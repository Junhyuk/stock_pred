#!/usr/bin/env python3
"""Collect extra unofficial daily numbers shown with the research forecast.

These prints are not exchange-official releases. They are stored with a receipt
time and are not given a manual probability weight.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

NAVER_TREND = {
    "KOSPI": "https://m.stock.naver.com/api/index/KOSPI/trend?pageSize=1",
    "KOSDAQ": "https://m.stock.naver.com/api/index/KOSDAQ/trend?pageSize=1",
}
NAVER_BASIC = (
    ("kospi200_futures", "KOSPI200 선물", "https://m.stock.naver.com/api/index/FUT/basic"),
    ("kospi200", "KOSPI200", "https://m.stock.naver.com/api/index/KPI200/basic"),
)
YAHOO = (
    ("usdkrw", "원/달러", "KRW=X", "원"),
    ("dxy", "달러 인덱스", "DX-Y.NYB", "index"),
    ("btc", "비트코인", "BTC-USD", "USD"),
    ("eth", "이더리움", "ETH-USD", "USD"),
    ("wti", "WTI (서부 텍사스 원유)", "CL=F", "USD"),
    ("brent", "브렌트유", "BZ=F", "USD"),
    ("vix", "VIX", "^VIX", "index"),
    ("sox", "SOX", "^SOX", "index"),
    ("gold", "금 선물", "GC=F", "USD"),
    ("copper", "구리 선물", "HG=F", "USD"),
    ("nq_futures", "나스닥 선물", "NQ=F", "index"),
    ("es_futures", "S&P 선물", "ES=F", "index"),
)
YIELD_TENORS = (1, 3, 5, 10)
KR_YIELD_CODES = {
    "kr_1y": ("한국 국고채 1년", "010190000"),
    "kr_3y": ("한국 국고채 3년", "010200000"),
    "kr_5y": ("한국 국고채 5년", "010200001"),
    "kr_10y": ("한국 국고채 10년", "010210000"),
}
JP_YIELD_URL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
FLOW_FIELDS = (
    ("foreignValue", "외국인 순매수"),
    ("institutionalValue", "기관 순매수"),
    ("personalValue", "개인 순매수"),
)


def _number(value):
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("%", "").strip()
    if cleaned in {"", "+", "-"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date(text: str) -> str:
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return str(text)[:10]


def parse_naver_trend(payload: dict, market: str) -> list[dict]:
    if not isinstance(payload, dict) or not payload.get("bizdate"):
        return []
    day = _date(payload["bizdate"])
    if len(day) != 10:
        return []
    rows = []
    for field, label in FLOW_FIELDS:
        raw = payload.get(field)
        value = _number(raw)
        if value is None:
            continue
        shown = raw if isinstance(raw, str) and raw.strip() else f"{value:,.0f}"
        rows.append({
            "id": f"{market.lower()}_{field}",
            "label": f"{market} {label}",
            "value": value,
            "display": f"{shown}억",
            "change_pct": None,
            "date": day,
            "source": "naver_index_trend_unofficial",
            "unit": "억원",
        })
    return rows


def parse_naver_basic(payload: dict, *, series_id: str, label: str) -> dict | None:
    if not isinstance(payload, dict):
        return None
    price = _number(payload.get("closePrice"))
    traded = payload.get("localTradedAt")
    if price is None or not isinstance(traded, str) or len(traded) < 10:
        return None
    day = traded[:10]
    if len(day) != 10 or day[4] != "-" or day[7] != "-":
        return None
    return {
        "id": series_id,
        "label": label,
        "value": price,
        "display": f"{price:,.2f}",
        "change_pct": _number(payload.get("fluctuationsRatio")),
        "date": day,
        "source": "naver_index_unofficial",
        "unit": "index",
    }


def derive_basis(future: dict | None, spot: dict | None) -> dict | None:
    if not future or not spot or future.get("date") != spot.get("date"):
        return None
    if not isinstance(future.get("value"), (int, float)) or not isinstance(spot.get("value"), (int, float)):
        return None
    gap = round(float(future["value"]) - float(spot["value"]), 2)
    return {
        "id": "kospi200_basis",
        "label": "KOSPI200 선물-현물",
        "value": gap,
        "display": f"{gap:+,.2f}",
        "change_pct": None,
        "date": future["date"],
        "source": "derived_naver_unofficial",
        "unit": "index_points",
    }


def parse_yahoo_chart(payload: dict, *, series_id: str, label: str, unit: str) -> dict | None:
    try:
        meta = payload["chart"]["result"][0]["meta"]
        price = float(meta["regularMarketPrice"])
        stamp = int(meta["regularMarketTime"])
        zone = ZoneInfo(meta.get("exchangeTimezoneName") or "UTC")
        day = datetime.fromtimestamp(stamp, zone).date().isoformat()
        change = meta.get("regularMarketChangePercent")
        change_pct = None if change is None else round(float(change), 4)
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if unit == "percent":
        display = f"{price:.3f}%"
    elif unit == "원":
        display = f"{price:,.2f}원"
    elif unit == "USD":
        display = f"{price:,.2f}달러"
    else:
        display = f"{price:,.2f}"
    return {
        "id": series_id,
        "label": label,
        "value": round(price, 4),
        "display": display,
        "change_pct": change_pct,
        "date": day,
        "source": "yahoo_chart_unofficial",
        "unit": unit,
    }


def _change_pct(latest: float, previous: float | None) -> float | None:
    if previous in (None, 0):
        return None
    return round((latest - previous) / abs(previous) * 100, 4)


def _level_row(series_id: str, label: str, day: str, value: float, previous: float | None, unit: str, source: str) -> dict:
    if unit == "percent":
        display = f"{value:.3f}%"
        delta = None if previous is None else round(value - previous, 4)
        change_pct = None
        change_display = None if delta is None else f"{delta:+.3f}%p"
    elif unit == "원":
        display = f"{value:,.2f}원"
        change_pct = _change_pct(value, previous)
        change_display = None
    elif unit == "USD":
        display = f"{value:,.2f}달러"
        change_pct = _change_pct(value, previous)
        change_display = None
    else:
        display = f"{value:,.2f}"
        change_pct = _change_pct(value, previous)
        change_display = None
    return {
        "id": series_id,
        "label": label,
        "value": round(value, 4),
        "display": display,
        "change_pct": change_pct,
        "change_display": change_display,
        "date": day,
        "source": source,
        "unit": unit,
    }


def _latest_pair(points: list[tuple[str, float]]) -> tuple[tuple[str, float], float | None] | None:
    ordered = sorted((day, value) for day, value in points if len(day) == 10)
    if not ordered:
        return None
    day, value = ordered[-1]
    previous = ordered[-2][1] if len(ordered) > 1 else None
    return (day, value), previous


def parse_treasury_latest(content: bytes) -> list[dict]:
    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
        "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
    }
    points = {tenor: [] for tenor in YIELD_TENORS}
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return []
    for entry in root.findall("a:entry", ns):
        props = entry.find(".//m:properties", ns)
        if props is None:
            continue
        raw_day = props.findtext("d:NEW_DATE", default="", namespaces=ns)
        day = raw_day[:10]
        if len(day) != 10:
            continue
        for tenor in YIELD_TENORS:
            text = props.findtext(f"d:BC_{tenor}YEAR", default="", namespaces=ns)
            try:
                points[tenor].append((day, float(text)))
            except (TypeError, ValueError):
                continue
    rows = []
    for tenor in YIELD_TENORS:
        pair = _latest_pair(points[tenor])
        if pair is None:
            continue
        (day, value), previous = pair
        rows.append(_level_row(
            f"us_{tenor}y", f"미국 국채 {tenor}년", day, value, previous, "percent",
            "us_treasury_daily_par_yield_curve",
        ))
    return rows


def parse_japan_latest(content: bytes) -> list[dict]:
    text = content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    start = next((index for index, line in enumerate(lines) if line.startswith("Date,")), None)
    if start is None:
        return []
    table = list(csv.DictReader(io.StringIO("\n".join(lines[start:]))))
    rows = []
    for tenor in YIELD_TENORS:
        points = []
        for row in table:
            day = str(row.get("Date") or "").strip().replace("/", "-")[:10]
            try:
                points.append((day, float(str(row.get(f"{tenor}Y") or "").strip())))
            except ValueError:
                continue
        pair = _latest_pair(points)
        if pair is None:
            continue
        (day, value), previous = pair
        rows.append(_level_row(
            f"jp_{tenor}y", f"일본 국채 {tenor}년", day, value, previous, "percent", "mof_jgb_historical",
        ))
    return rows


def parse_korea_page(payload: dict) -> tuple[int, list[tuple[str, float]]]:
    block = payload.get("StatisticSearch") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return 0, []
    try:
        total = int(block.get("list_total_count") or 0)
    except (TypeError, ValueError):
        total = 0
    batch = block.get("row") or []
    batch = batch if isinstance(batch, list) else [batch]
    points = []
    for row in batch:
        try:
            points.append((datetime.strptime(row["TIME"], "%Y%m%d").date().isoformat(), float(row["DATA_VALUE"])))
        except (KeyError, TypeError, ValueError):
            continue
    return total, points


def build_payload(series: list[dict], *, received_at: str) -> dict:
    return {
        "schema_version": "unofficial_numerics_v1",
        "received_at": received_at,
        "production_weight": 0.0,
        "series": series,
    }


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def _get_json(url: str) -> dict:
    return json.loads(fetch(url).decode("utf-8"))


def _latest_us_yields() -> list[dict]:
    year = datetime.now(UTC).year
    content = fetch(
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"
    )
    return parse_treasury_latest(content)


def _latest_japan_yields() -> list[dict]:
    return parse_japan_latest(fetch(JP_YIELD_URL))


def _latest_korea_yields() -> list[dict]:
    end = datetime.now(UTC).date()
    start = (end - timedelta(days=40)).strftime("%Y%m%d")
    end_text = end.strftime("%Y%m%d")
    rows = []
    for series_id, (label, code) in KR_YIELD_CODES.items():
        points, offset, total = [], 1, None
        while total is None or offset <= total:
            url = (
                "https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/"
                f"{offset}/{offset + 9}/817Y002/D/{start}/{end_text}/{code}"
            )
            total, batch = parse_korea_page(json.loads(fetch(url)))
            points.extend(batch)
            offset += 10
            if offset > 40:
                break
        pair = _latest_pair(points)
        if pair is None:
            continue
        (day, value), previous = pair
        rows.append(_level_row(series_id, label, day, value, previous, "percent", "bok_ecos_817Y002_sample"))
    return rows


def _append_optional(series: list[dict], loader) -> None:
    try:
        series.extend(loader())
    except (OSError, ValueError, TypeError, KeyError):
        return


def collect(received_at: str | None = None) -> dict:
    received = received_at or datetime.now(UTC).isoformat()
    series: list[dict] = []
    for market, url in NAVER_TREND.items():
        try:
            series.extend(parse_naver_trend(_get_json(url), market))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    basic: dict[str, dict] = {}
    for series_id, label, url in NAVER_BASIC:
        try:
            row = parse_naver_basic(_get_json(url), series_id=series_id, label=label)
        except (OSError, ValueError, TypeError, KeyError):
            row = None
        if row:
            basic[series_id] = row
            series.append(row)
    basis = derive_basis(basic.get("kospi200_futures"), basic.get("kospi200"))
    if basis:
        series.append(basis)
    for series_id, label, symbol, unit in YAHOO:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + quote(symbol, safe="") + "?interval=1d&range=5d"
        try:
            row = parse_yahoo_chart(_get_json(url), series_id=series_id, label=label, unit=unit)
        except (OSError, ValueError, TypeError, KeyError):
            row = None
        if row:
            series.append(row)
    _append_optional(series, _latest_us_yields)
    _append_optional(series, _latest_japan_yields)
    _append_optional(series, _latest_korea_yields)
    return build_payload(series, received_at=received)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/data/unofficial_numerics.json")
    args = parser.parse_args()
    payload = collect()
    if not payload["series"]:
        raise SystemExit("no unofficial numerics received")
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(path), "count": len(payload["series"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

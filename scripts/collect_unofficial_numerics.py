#!/usr/bin/env python3
"""Collect extra unofficial daily numbers shown with the research forecast.

These prints are not exchange-official releases. They are stored with a receipt
time and are not given a manual probability weight.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
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
    ("usdkrw", "USD/KRW", "KRW=X", "원"),
    ("wti", "WTI", "CL=F", "USD"),
    ("vix", "VIX", "^VIX", "index"),
    ("sox", "SOX", "^SOX", "index"),
    ("dxy", "달러인덱스", "DX-Y.NYB", "index"),
    ("gold", "금 선물", "GC=F", "USD"),
    ("copper", "구리 선물", "HG=F", "USD"),
    ("us10y", "미국 10년 금리", "^TNX", "percent"),
    ("nq_futures", "나스닥 선물", "NQ=F", "index"),
    ("es_futures", "S&P 선물", "ES=F", "index"),
)
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


def build_payload(series: list[dict], *, received_at: str) -> dict:
    return {
        "schema_version": "unofficial_numerics_v1",
        "received_at": received_at,
        "production_weight": 0.0,
        "series": series,
    }


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


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

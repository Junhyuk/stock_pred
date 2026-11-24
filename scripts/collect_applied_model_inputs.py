#!/usr/bin/env python3
"""Collect inputs used by the compared index models.

Kronos needs index OHLCV. The deep transformer and LightGBM comparison also
used dollar, crypto, oil, and 1/3/5/10-year yields. Existing rows are kept.
Unofficial prints store published_at as NULL.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
YAHOO = {
    "YAHOO_DXY": "DX-Y.NYB",
    "YAHOO_BTC": "BTC-USD",
    "YAHOO_ETH": "ETH-USD",
    "YAHOO_WTI": "CL=F",
    "YAHOO_USDKRW": "KRW=X",
}
KR_CODES = {
    "KR_KTB_1Y": "010190000",
    "KR_KTB_3Y": "010200000",
    "KR_KTB_5Y": "010200001",
    "KR_KTB_10Y": "010210000",
}
INDEXES = {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}


def fetch(url: str, timeout: int = 40) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_plan(latest: str | None, today: str, overlap_days: int = 3) -> tuple[str, str | None]:
    """Decide whether a series needs a request.

    ``skip`` means the stored date already reaches today. ``full`` means the table has
    no row. ``range`` starts a few days before the last stored date so only the missing
    tail is requested. A request that returns nothing new records nothing: stored rows
    keep the time they were first seen (``ingested_at``), so a stale source such as a
    stopped file is asked again on the next run instead of being marked as checked.
    """
    if latest and latest >= today:
        return "skip", None
    if not latest:
        return "full", None
    start = datetime.strptime(latest, "%Y-%m-%d").date() - timedelta(days=overlap_days)
    return "range", start.isoformat()


def yahoo_levels(symbol: str, span: str = "10y", start: str | None = None) -> list[tuple[str, float]]:
    query = f"?interval=1d&range={span}"
    if start:
        period1 = int(datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=UTC).timestamp())
        period2 = int(datetime.now(UTC).timestamp()) + 86400
        query = f"?interval=1d&period1={period1}&period2={period2}"
    payload = json.loads(fetch(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.request.quote(symbol, safe="") + query))
    result = payload["chart"]["result"][0]
    zone = ZoneInfo(result["meta"].get("exchangeTimezoneName") or "UTC")
    quote = result["indicators"]["quote"][0]
    rows = []
    for stamp, close in zip(result["timestamp"], quote["close"]):
        if close is None:
            continue
        rows.append((datetime.fromtimestamp(int(stamp), zone).date().isoformat(), float(close)))
    return rows


def yahoo_index_bars(symbol: str) -> dict[str, dict]:
    payload = json.loads(fetch(
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.request.quote(symbol, safe="") + "?interval=1d&range=3mo"))
    result = payload["chart"]["result"][0]
    zone = ZoneInfo(result["meta"].get("exchangeTimezoneName") or "Asia/Seoul")
    quote = result["indicators"]["quote"][0]
    bars = {}
    for stamp, open_, high, low, close, volume in zip(
            result["timestamp"], quote["open"], quote["high"], quote["low"], quote["close"], quote["volume"]):
        day = datetime.fromtimestamp(int(stamp), zone).date().isoformat()
        usable = None not in (open_, high, low, close) and min(open_, high, low, close) > 0 and low <= min(open_, close) <= max(open_, close) <= high
        bars[day] = {
            "open": float(open_) if usable else None,
            "high": float(high) if usable else None,
            "low": float(low) if usable else None,
            "close": float(close) if close not in (None, 0) else None,
            "volume": None if not volume else float(volume),
            "ohlc_ok": usable,
        }
    return bars


def naver_index_bars(market: str) -> dict[str, dict]:
    payload = json.loads(fetch(f"https://m.stock.naver.com/api/index/{market}/price?pageSize=30&page=1"))
    if not isinstance(payload, list):
        raise ValueError("invalid Naver index history")
    bars = {}
    for row in payload:
        day = row.get("localTradedAt")
        try:
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                continue
        except ValueError:
            continue
        values = {}
        for key, original in (("open", "openPrice"), ("high", "highPrice"), ("low", "lowPrice"), ("close", "closePrice")):
            try:
                values[key] = float(str(row.get(original)).replace(",", ""))
            except (TypeError, ValueError):
                values[key] = None
        if any(value is None or value <= 0 for value in values.values()):
            continue
        if not (values["low"] <= min(values["open"], values["close"]) <= max(values["open"], values["close"]) <= values["high"]):
            continue
        values["volume"] = None
        bars[day] = values
    return bars


def treasury_yields(start_year: int | None = None) -> dict[str, list[tuple[str, float]]]:
    ns = {"a": "http://www.w3.org/2005/Atom",
          "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata",
          "d": "http://schemas.microsoft.com/ado/2007/08/dataservices"}
    rows = {tenor: [] for tenor in (1, 3, 5, 10)}
    first_year = start_year or 2021
    for year in range(first_year, datetime.now(UTC).year + 1):
        root = ElementTree.fromstring(fetch(
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value={year}"))
        for entry in root.findall("a:entry", ns):
            props = entry.find(".//m:properties", ns)
            day = props.findtext("d:NEW_DATE", namespaces=ns)[:10]
            for tenor in rows:
                text = props.findtext(f"d:BC_{tenor}YEAR", namespaces=ns)
                if text not in (None, ""):
                    rows[tenor].append((day, float(text)))
    return {f"US_TREASURY_{tenor}Y": values for tenor, values in rows.items()}


def japan_yields(start: str | None = None) -> dict[str, list[tuple[str, float]]]:
    import pandas as pd
    raw = pd.read_csv(pd.io.common.BytesIO(fetch(
        "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv")), skiprows=1)
    raw.columns = [str(column).strip() for column in raw.columns]
    mapping = {"1Y": "JP_JGB_1Y", "3Y": "JP_JGB_3Y", "5Y": "JP_JGB_5Y", "10Y": "JP_JGB_10Y"}
    output = {code: [] for code in mapping.values()}
    days = pd.to_datetime(raw["Date"], errors="coerce")
    for source, code in mapping.items():
        values = pd.to_numeric(raw[source], errors="coerce")
        for day, value in zip(days, values):
            if pd.notna(day) and pd.notna(value):
                observed = day.date().isoformat()
                if start is None or observed >= start:
                    output[code].append((observed, float(value)))
    return output


def ecos_block(payload: dict) -> dict | None:
    """Return the StatisticSearch block, None for an empty range, or raise with ECOS's own code.

    ECOS answers an empty range (INFO-200) or any refusal (for example a rate limit or a
    key problem) with a RESULT object and no StatisticSearch block. Indexing the block
    blindly turned both cases into an unexplained KeyError.
    """
    block = payload.get("StatisticSearch")
    if block is not None:
        return block
    result = payload.get("RESULT") or {}
    if result.get("CODE") == "INFO-200":
        return None
    raise RuntimeError(f"ECOS returned {result.get('CODE') or 'no StatisticSearch block'}: "
                       f"{str(result.get('MESSAGE') or '')[:120]}")


def korea_yields(start: str | None = None, end: str | None = None) -> dict[str, list[tuple[str, float]]]:
    start = start or "20210104"
    end = end or datetime.now(UTC).strftime("%Y%m%d")

    def one(item):
        code, ecos = item
        rows, offset, total = [], 1, None
        while total is None or offset <= total:
            url = ("https://ecos.bok.or.kr/api/StatisticSearch/sample/json/kr/"
                   f"{offset}/{offset + 9}/817Y002/D/{start}/{end}/{ecos}")
            block = ecos_block(json.loads(fetch(url)))
            if block is None:
                break
            total = int(block["list_total_count"])
            batch = block["row"]
            batch = batch if isinstance(batch, list) else [batch]
            rows.extend((datetime.strptime(row["TIME"], "%Y%m%d").date().isoformat(), float(row["DATA_VALUE"])) for row in batch)
            offset += 10
        return code, rows

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, KR_CODES.items()))


def insert_macro(conn, series: dict[str, list[tuple[str, float]]], source: str, ingested_at: str) -> int:
    added = 0
    for code, rows in series.items():
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO macro_series
            (series_code, observation_date, value, published_at, revision_no, source, ingested_at)
            VALUES (?, ?, ?, NULL, 0, ?, ?)
            """,
            [(code, day, value, source, ingested_at) for day, value in rows],
        )
        added += conn.total_changes - before
    return added


def _latest_index(conn, market: str) -> str | None:
    try:
        row = conn.execute("SELECT MAX(session_date) FROM index_eod WHERE index_code = ?", (market,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def insert_index_bars(conn, ingested_at: str, today: str) -> int:
    added = 0
    for market, symbol in INDEXES.items():
        mode, start = fetch_plan(_latest_index(conn, market), today)
        if mode == "skip":
            continue
        bars = naver_index_bars(market)
        volumes = yahoo_index_bars(symbol)
        for day, bar in volumes.items():
            if day not in bars and bar.get("ohlc_ok"):
                bars[day] = bar
            elif day in bars and bar["volume"]:
                bars[day]["volume"] = bar["volume"]
            if day in bars and bars[day].get("volume"):
                conn.execute(
                    """
                    UPDATE index_eod SET volume = ?
                    WHERE index_code = ? AND session_date = ? AND volume IS NULL
                    """,
                    (bars[day]["volume"], market, day),
                )
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO index_eod
            (index_code, session_date, open, high, low, close, volume, turnover, source, published_at, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'naver_ohlc_yahoo_volume_unofficial', NULL, ?)
            """,
            [(market, day, bar["open"], bar["high"], bar["low"], bar["close"], bar.get("volume"), ingested_at)
             for day, bar in bars.items()],
        )
        added += conn.total_changes - before
    return added


def _latest(conn, code: str) -> str | None:
    try:
        row = conn.execute("SELECT MAX(observation_date) FROM macro_series WHERE series_code = ?", (code,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row and row[0] else None


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_series (
            series_code TEXT NOT NULL, observation_date TEXT NOT NULL, value REAL,
            published_at TEXT, revision_no INTEGER DEFAULT 0, source TEXT NOT NULL,
            ingested_at TEXT NOT NULL, PRIMARY KEY(series_code, observation_date, revision_no))
        """)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS index_eod (
            index_code TEXT NOT NULL, session_date TEXT NOT NULL, open REAL, high REAL,
            low REAL, close REAL, volume REAL, turnover REAL, source TEXT NOT NULL,
            published_at TEXT, ingested_at TEXT NOT NULL, PRIMARY KEY(index_code, session_date))
        """)


def _kst_today() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat()


def _yahoo_series(conn, today: str) -> dict[str, list[tuple[str, float]]]:
    series = {}
    for code, symbol in YAHOO.items():
        mode, start = fetch_plan(_latest(conn, code), today)
        if mode == "skip":
            continue
        series[code] = yahoo_levels(symbol, "10y", None if mode == "full" else start)
    return series


def _treasury_series(conn, today: str) -> dict[str, list[tuple[str, float]]]:
    mode, start = fetch_plan(_latest(conn, "US_TREASURY_10Y"), today)
    if mode == "skip":
        return {}
    first_year = 2021 if mode == "full" else int(start[:4])
    return treasury_yields(first_year)


def _japan_series(conn, today: str) -> dict[str, list[tuple[str, float]]]:
    mode, start = fetch_plan(_latest(conn, "JP_JGB_10Y"), today, overlap_days=0)
    if mode == "skip":
        return {}
    return japan_yields(None if mode == "full" else start)


def _korea_series(conn, today: str) -> dict[str, list[tuple[str, float]]]:
    mode, start = fetch_plan(_latest(conn, "KR_KTB_3Y"), today)
    if mode == "skip":
        return {}
    start_text = "20210104" if mode == "full" else start.replace("-", "")
    return korea_yields(start_text, today.replace("-", ""))


def collect(sqlite_path: Path) -> dict:
    ingested_at = datetime.now(UTC).isoformat()
    today = _kst_today()
    conn = sqlite3.connect(sqlite_path)
    sources = (
        ("index_bars", lambda: insert_index_bars(conn, ingested_at, today)),
        ("yahoo_levels", lambda: insert_macro(conn, _yahoo_series(conn, today), "yahoo_chart_unofficial", ingested_at)),
        ("us_yields", lambda: insert_macro(conn, _treasury_series(conn, today), "us_treasury_daily_par_yield_curve", ingested_at)),
        ("jp_yields", lambda: insert_macro(conn, _japan_series(conn, today), "mof_jgb_historical", ingested_at)),
        ("kr_yields", lambda: insert_macro(conn, _korea_series(conn, today), "bok_ecos_817Y002_sample", ingested_at)),
    )
    counts: dict = {}
    errors: dict[str, str] = {}
    try:
        _ensure_tables(conn)
        conn.commit()
        for name, run in sources:
            # One failing source must not discard what the others already fetched.
            try:
                counts[name] = run()
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - recorded per source and reported by main()
                conn.rollback()
                counts[name] = None
                errors[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        conn.close()
    counts["asof"] = today
    counts["errors"] = errors
    counts["published_at"] = None
    counts["production_weight"] = 0.0
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/processed/index_forecast.sqlite")
    args = parser.parse_args()
    result = collect(Path(args.sqlite))
    for name, message in result["errors"].items():
        # A workflow annotation, so a failed source is visible on the run page.
        print(f"::warning title=collect_applied_model_inputs::{name} failed: {message}")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

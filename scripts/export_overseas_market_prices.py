#!/usr/bin/env python3
"""Export completed USD price sessions for the public SKHY and KORU charts."""

from __future__ import annotations

import argparse
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.automation_state import atomic_json
from roboquant.index_forecast.market_calendar import exchange_schedule

NY = ZoneInfo("America/New_York")
CLOSE_BUFFER = timedelta(minutes=15)
MAX_POINTS = 252
INSTRUMENTS = {
    "SKHY": {
        "label": "SK하이닉스 ADR",
        "market": "NASDAQ",
        "exchanges": {"NMS", "NGM", "NCM"},
        "kind": "EQUITY",
        "listed_from": "2026-07-10",
    },
    "KORU": {
        "label": "Direxion 한국 3배 ETF",
        "market": "NYSE Arca",
        "exchanges": {"PCX"},
        "kind": "ETF",
        "listed_from": "2013-04-11",
    },
}


def _schedule(first, last):
    calendar, days = exchange_schedule("XNYS", first, last)
    return {day.date().isoformat(): calendar.session_close(day).to_pydatetime() for day in days}


def latest_session(as_of):
    if as_of.utcoffset() is None:
        raise ValueError("an aware price cutoff is required")
    day = as_of.astimezone(NY).date()
    sessions = _schedule(day - timedelta(days=14), day)
    return date.fromisoformat(
        max(day for day, close in sessions.items() if close + CLOSE_BUFFER <= as_of)
    )


def _market_date(timestamp):
    return datetime.fromtimestamp(timestamp, NY).date().isoformat()


def _validate_split_series(rows, events):
    close_by_date = dict(rows)
    positions = {day: i for i, (day, _) in enumerate(rows)}
    for event in events.values():
        day = _market_date(event["date"])
        index = positions.get(day)
        if index is None or index == 0:
            continue
        ratio = event["numerator"] / event["denominator"]
        if not math.isfinite(ratio) or ratio <= 0:
            raise ValueError("invalid provider split ratio")
        before = close_by_date[rows[index - 1][0]]
        after = close_by_date[day]
        # Yahoo's close has already been split adjusted. An apparent jump of at
        # least half the log split ratio is evidence against using this series.
        limit = math.sqrt(max(ratio, 1 / ratio))
        if limit >= 2 and max(after / before, before / after) >= limit:
            raise ValueError("price discontinuity at known split")


def market_points(symbol, payload, as_of):
    if as_of.utcoffset() is None:
        raise ValueError("an aware price cutoff is required")
    chart = payload["chart"]
    if chart["error"] or not chart["result"]:
        raise ValueError("provider has no price series")
    result = chart["result"][0]
    meta = result["meta"]
    spec = INSTRUMENTS[symbol]
    if (
        meta.get("symbol") != symbol
        or meta.get("currency") != "USD"
        or meta.get("exchangeTimezoneName") != NY.key
        or meta.get("exchangeName") not in spec["exchanges"]
        or meta.get("instrumentType") != spec["kind"]
    ):
        raise ValueError(f"wrong provider instrument identity for {symbol}")

    timestamps = result["timestamp"]
    values = result["indicators"]["quote"][0]["close"]
    if not timestamps:
        return []
    days = [_market_date(stamp) for stamp in timestamps]
    schedule = _schedule(min(days), min(max(days), as_of.astimezone(NY).date().isoformat()))
    observed = {}
    for day, price in zip(days, values, strict=True):
        close = schedule.get(day)
        if day < spec["listed_from"] or close is None or close + CLOSE_BUFFER > as_of:
            continue
        if isinstance(price, bool) or not isinstance(price, (int, float)):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        if day in observed and observed[day] != price:
            raise ValueError(f"conflicting close for {symbol} on {day}")
        observed[day] = price
    rows = sorted(observed.items())
    _validate_split_series(rows, result.get("events", {}).get("splits", {}))
    return [{"date": day, "close": round(price, 4)} for day, price in rows[-MAX_POINTS:]]


def fetch_yahoo(symbol):
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}?range=1y&interval=1d&events=div%2Csplits"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urlopen(req, timeout=6) as response:
                data = json.load(response)
            if data.get("chart", {}).get("result"):
                return data, datetime.now(UTC)
        except (OSError, ValueError):
            continue
    raise OSError(f"Yahoo Finance history unavailable for {symbol}")


def _retained(previous, expected):
    if not isinstance(previous, dict):
        return []
    rows = previous.get("points")
    if not isinstance(rows, list):
        return []
    validated = []
    for row in rows:
        try:
            day = datetime.fromisoformat(row["date"]).date().isoformat()
            price = row["close"]
        except (KeyError, TypeError, ValueError):
            return []
        if (
            day > expected
            or isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
            or price <= 0
        ):
            return []
        validated.append({"date": day, "close": price})
    if [row["date"] for row in validated] != sorted({row["date"] for row in validated}):
        return []
    return validated[-MAX_POINTS:]


def export_chart(output, *, as_of=None, fetch=fetch_yahoo):
    cutoff = as_of or datetime.now(UTC)
    expected = latest_session(cutoff).isoformat()
    try:
        previous = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    if not isinstance(previous, dict) or previous.get("schema_version") != 1:
        previous = {}
    old_instruments = previous.get("instruments")
    if not isinstance(old_instruments, dict):
        old_instruments = {}
    instruments = {}
    with ThreadPoolExecutor(max_workers=len(INSTRUMENTS)) as pool:
        downloads = {symbol: pool.submit(fetch, symbol) for symbol in INSTRUMENTS}
        for symbol, spec in INSTRUMENTS.items():
            saved = _retained(old_instruments.get(symbol), expected)
            rows = []
            try:
                raw, received_at = downloads[symbol].result()
                rows = market_points(symbol, raw, min(cutoff, received_at))
            except (
                KeyError,
                IndexError,
                TypeError,
                ValueError,
                OSError,
                OverflowError,
                ZeroDivisionError,
            ):
                pass
            if rows and rows[-1]["date"] == expected:
                status = "ready"
            else:
                status = "stale" if rows or saved else "unavailable"
                if saved and (not rows or rows[-1]["date"] <= saved[-1]["date"]):
                    rows = saved
            last_session = rows[-1]["date"] if rows else None
            previous_close = None
            if len(rows) >= 2:
                nearby_sessions = _schedule(
                    date.fromisoformat(last_session) - timedelta(days=10), last_session
                )
                if list(nearby_sessions)[-2:] == [row["date"] for row in rows[-2:]]:
                    previous_close = rows[-2]["close"]
            instruments[symbol] = {
                "symbol": symbol,
                "label": spec["label"],
                "market": spec["market"],
                "currency": "USD",
                "status": status,
                "source": {
                    "label": "Yahoo Finance",
                    "url": f"https://finance.yahoo.com/quote/{symbol}/history/",
                },
                "last_session": last_session,
                "previous_close": previous_close,
                "points": rows,
            }
    states = [entry["status"] for entry in instruments.values()]
    overall = (
        "ready"
        if all(value == "ready" for value in states)
        else ("unavailable" if all(value == "unavailable" for value in states) else "partial")
    )
    result = {
        "schema_version": 1,
        "status": overall,
        "expected_session": expected,
        "timezone": NY.key,
        "price_basis": "split_adjusted_close_excluding_dividends",
        "instruments": instruments,
    }
    if {key: value for key, value in previous.items() if key != "generated_at"} == result:
        return previous
    result["generated_at"] = cutoff.astimezone(UTC).isoformat(timespec="seconds")
    atomic_json(output, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT / "docs/data/overseas-market-prices.json"
    )
    args = parser.parse_args()
    result = export_chart(args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "expected_session": result["expected_session"],
                "instruments": {
                    symbol: {"last_session": series["last_session"], "rows": len(series["points"])}
                    for symbol, series in result["instruments"].items()
                },
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

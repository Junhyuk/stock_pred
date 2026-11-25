#!/usr/bin/env python3
"""Export two years of completed KRX investor net buying for SK hynix or Samsung.

Naver Pay Securities identifies the default mobile investor table as KRX and
serves NXT through a separate endpoint. The published fields are net share
counts (buys minus sells) for each investor class. Seoul's current date is
excluded because the provider can still be updating the session.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import TypedDict
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.market_calendar import exchange_schedule

SYMBOL = "000660"
INSTRUMENTS = {
    "000660": ("SK하이닉스", "sk-hynix-investor-flow.json"),
    "005930": ("삼성전자", "samsung-electronics-investor-flow.json"),
}
SEOUL = ZoneInfo("Asia/Seoul")
PAGE_SIZE = 60
MAX_PAGES = 12
OVERLAP_SESSIONS = 10
PROVIDER_FIELDS = {
    "foreign": "foreignerPureBuyQuant",
    "institution": "organPureBuyQuant",
    "individual": "individualPureBuyQuant",
}


class FlowRow(TypedDict):
    date: str
    foreign: int | None
    institution: int | None
    individual: int | None


def identity(symbol: str) -> dict:
    return {
        "schema_version": 1,
        "issuer": INSTRUMENTS[symbol][0],
        "symbol": symbol,
        "market": "KRX",
        "unit": "shares",
        "source": {
            "label": "네이버페이 증권",
            "url": f"https://m.stock.naver.com/domestic/stock/{symbol}/total",
        },
    }


def window_start(today: date) -> date:
    try:
        return today.replace(year=today.year - 2)
    except ValueError:
        return today.replace(year=today.year - 2, day=28)


def session_dates(start: date, end: date) -> list[str]:
    _, sessions = exchange_schedule("XKRX", start, end)
    return [day.date().isoformat() for day in sessions]


def fetch_naver_page(before: date, *, symbol: str = SYMBOL) -> object:
    """Fetch a page whose dates strictly precede the provider's bizdate cursor."""
    query = urlencode({"pageSize": PAGE_SIZE, "bizdate": before.strftime("%Y%m%d")})
    request = Request(
        f"https://m.stock.naver.com/api/stock/{symbol}/trend?{query}",
        headers={"Accept": "application/json", "User-Agent": "stock-pred-research-chart/1.0"},
    )
    for attempt in range(3):
        try:
            with urlopen(request, timeout=12) as response:
                return json.load(response)
        except OSError as exc:
            if attempt == 2 or (isinstance(exc, HTTPError) and exc.code < 500 and exc.code != 429):
                raise
            sleep(attempt + 1)


def net_shares(value: object) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(
        r"[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)", value.strip()
    ):
        return int(value.replace(",", ""))
    return None


def collect_rows(
    today: date, fetch_page: Callable[[date], object], *, symbol: str = SYMBOL,
    through: date | None = None,
) -> list[FlowRow]:
    first = window_start(today)
    through = through or first
    before = today
    rows: dict[str, FlowRow] = {}
    for _ in range(MAX_PAGES):
        page = fetch_page(before)
        if not isinstance(page, list):
            raise ValueError("unexpected response from the KRX investor trend endpoint")
        dates = []
        for item in page:
            if not isinstance(item, dict) or item.get("itemCode") != symbol:
                continue
            raw_date = item.get("bizdate")
            if not isinstance(raw_date, str) or not re.fullmatch(r"[0-9]{8}", raw_date):
                continue
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                continue
            if day >= before or day.weekday() >= 5:
                continue
            dates.append(day)
            if day < first:
                continue
            key = day.isoformat()
            row: FlowRow = {
                "date": key,
                "foreign": net_shares(item.get(PROVIDER_FIELDS["foreign"])),
                "institution": net_shares(item.get(PROVIDER_FIELDS["institution"])),
                "individual": net_shares(item.get(PROVIDER_FIELDS["individual"])),
            }
            if key in rows and rows[key] != row:
                raise ValueError("the KRX investor trend returned conflicting records for one date")
            rows[key] = row
        if len(page) < PAGE_SIZE or (dates and min(dates) <= through):
            break
        if not dates:
            raise ValueError("the KRX investor trend cursor stopped advancing")
        before = min(dates)
    return [rows[key] for key in sorted(rows)]


def previous_rows(path: Path, sessions: set[str], *, symbol: str = SYMBOL) -> dict[str, FlowRow]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(snapshot, dict)
        or any(snapshot.get(key) != value for key, value in identity(symbol).items())
        or not isinstance(snapshot.get("series"), list)
    ):
        return {}
    rows = {}
    for row in snapshot["series"]:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str) or row["date"] not in sessions:
            continue
        if all(key in row and (row[key] is None or type(row[key]) is int) for key in PROVIDER_FIELDS):
            rows[row["date"]] = row
    return rows


def last_observation(rows: list[FlowRow]) -> str:
    return max(
        (row["date"] for row in rows if any(row[key] is not None for key in PROVIDER_FIELDS)),
        default="",
    )


def write_snapshot(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def refresh(
    output: Path, now: datetime, fetch_page: Callable[[date], object], *, symbol: str = SYMBOL,
) -> str:
    today = now.astimezone(SEOUL).date()
    start = window_start(today)
    sessions = session_dates(start, today - timedelta(days=1))
    expected = set(sessions)
    previous = previous_rows(output, expected, symbol=symbol)
    # Refresh the recent overlap to pick up revisions. Repair older session gaps
    # when needed, and stop the initial or recovery backfill at the window start.
    overlap_start = sessions[-min(len(sessions), OVERLAP_SESSIONS)]
    first_uncovered = min(expected - previous.keys(), default=overlap_start)
    through = date.fromisoformat(min(overlap_start, first_uncovered)) if previous else start
    try:
        fresh = [
            row for row in collect_rows(today, fetch_page, symbol=symbol, through=through)
            if row["date"] in expected
        ]
    except (OSError, ValueError) as exc:
        print(f"{symbol} investor flow fetch failed: {exc}", file=sys.stderr)
        fresh = []

    latest_fresh = last_observation(fresh)
    latest_previous = last_observation(list(previous.values()))
    if latest_previous and latest_fresh < latest_previous:
        return "preserved"

    if latest_fresh:
        for row in fresh:
            existing = previous.get(row["date"], {})
            previous[row["date"]] = {
                key: existing.get(key) if value is None else value for key, value in row.items()
            }
        series = [previous[key] for key in sorted(previous)]
    else:
        series = []
    missing_sessions = sorted(expected - {row["date"] for row in series})
    missing_values = {key: sum(row[key] is None for row in series) for key in PROVIDER_FIELDS}
    if not series:
        status = "unavailable"
    else:
        status = "partial" if missing_sessions or any(missing_values.values()) else "ready"

    write_snapshot(
        output,
        {
            **identity(symbol),
            "status": status,
            "generated_at": now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "window_start": start.isoformat(),
            "window_end": sessions[-1],
            "coverage": {
                "expected_sessions": len(sessions),
                "observed_sessions": len(series),
                "missing_sessions": missing_sessions,
                "missing_values": missing_values,
            },
            "series": series,
        },
    )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", choices=INSTRUMENTS, default=SYMBOL)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    issuer, filename = INSTRUMENTS[args.symbol]
    output = args.output or ROOT / "docs/data" / filename
    status = refresh(output, datetime.now(UTC), partial(fetch_naver_page, symbol=args.symbol), symbol=args.symbol)
    print(f"{issuer} investor flow: {status} ({output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export the latest completed SK hynix KRX investor net-buy observations.

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
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import sleep
from typing import TypedDict
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SYMBOL = "000660"
SOURCE = {
    "label": "네이버페이 증권",
    "url": f"https://m.stock.naver.com/domestic/stock/{SYMBOL}/total",
}
ENDPOINT = f"https://m.stock.naver.com/api/stock/{SYMBOL}/trend"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "docs/data/sk-hynix-investor-flow.json"
SEOUL = ZoneInfo("Asia/Seoul")
PAGE_SIZE = 60
HISTORY_ROWS = 250
PROVIDER_FIELDS = {
    "foreign": "foreignerPureBuyQuant",
    "institution": "organPureBuyQuant",
    "individual": "individualPureBuyQuant",
}
IDENTITY = {
    "schema_version": 1,
    "issuer": "SK하이닉스",
    "symbol": SYMBOL,
    "market": "KRX",
    "unit": "shares",
    "source": SOURCE,
}


class FlowRow(TypedDict):
    date: str
    foreign: int | None
    institution: int | None
    individual: int | None


def fetch_naver_page(before: date) -> object:
    """Fetch a page whose dates strictly precede the provider's bizdate cursor."""
    query = urlencode({"pageSize": PAGE_SIZE, "bizdate": before.strftime("%Y%m%d")})
    request = Request(
        f"{ENDPOINT}?{query}",
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


def collect_rows(today: date, fetch_page: Callable[[date], object]) -> list[FlowRow]:
    before = today
    rows: dict[str, FlowRow] = {}
    for _ in range((HISTORY_ROWS + PAGE_SIZE - 1) // PAGE_SIZE):
        page = fetch_page(before)
        if not isinstance(page, list):
            raise ValueError("unexpected response from the KRX investor trend endpoint")
        dates = []
        for item in page:
            if not isinstance(item, dict) or item.get("itemCode") != SYMBOL:
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
            key = day.isoformat()
            rows[key] = {
                "date": key,
                "foreign": net_shares(item.get(PROVIDER_FIELDS["foreign"])),
                "institution": net_shares(item.get(PROVIDER_FIELDS["institution"])),
                "individual": net_shares(item.get(PROVIDER_FIELDS["individual"])),
            }
        if len(page) < PAGE_SIZE:
            break
        if not dates:
            raise ValueError("the KRX investor trend cursor stopped advancing")
        before = min(dates)
    return [rows[key] for key in sorted(rows)[-HISTORY_ROWS:]]


def previous_rows(path: Path, today: date) -> dict[str, FlowRow]:
    try:
        snapshot = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if (
        not isinstance(snapshot, dict)
        or any(snapshot.get(key) != value for key, value in IDENTITY.items())
        or not isinstance(snapshot.get("series"), list)
    ):
        return {}
    rows = {}
    for row in snapshot["series"]:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            continue
        try:
            day = date.fromisoformat(row["date"])
        except ValueError:
            continue
        if day >= today or day.weekday() >= 5 or day.isoformat() != row["date"]:
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


def refresh(output: Path, now: datetime, fetch_page: Callable[[date], object]) -> str:
    today = now.astimezone(SEOUL).date()
    try:
        fresh = collect_rows(today, fetch_page)
    except (OSError, ValueError) as exc:
        print(f"SK hynix investor flow fetch failed: {exc}", file=sys.stderr)
        fresh = []

    previous = previous_rows(output, today)
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
        series = [previous[key] for key in sorted(previous)[-HISTORY_ROWS:]]
        status = (
            "partial" if any(row[key] is None for row in series for key in PROVIDER_FIELDS)
            else "ready"
        )
    else:
        series = []
        status = "unavailable"

    write_snapshot(
        output,
        {
            **IDENTITY,
            "status": status,
            "generated_at": now.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "series": series,
        },
    )
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    status = refresh(args.output, datetime.now(UTC), fetch_naver_page)
    print(f"SK hynix investor flow: {status} ({args.output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

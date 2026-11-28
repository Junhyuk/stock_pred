#!/usr/bin/env python3
"""Collect FreeSIS marketwide CFD balances into a private research snapshot."""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.automation_state import atomic_json

SOURCE_URL = "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
METRIC_OBJECTS = {
    "with_margin": "STATDRV0100000320BO",
    "without_margin": "STATDRV0100000310BO",
}
TIMEOUT_SECONDS = 7
MAX_RESPONSE_BYTES = 512_000
DEFAULT_PRIVATE_OUTPUT = ROOT / "reports/cfd_20261125/data/frees_is_market.json"


def _iso_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD for the CFD query dates")
    return parsed


def _fetch_metric(object_name: str, first: date, last: date) -> list[dict]:
    request_data = {
        "dmSearch": {
            "tmpV40": "1",  # Query in whole KRW.
            "tmpV41": "1",
            "tmpV1": "D",
            "tmpV45": first.isoformat().replace("-", ""),
            "tmpV46": last.isoformat().replace("-", ""),
            "OBJ_NM": object_name,
        }
    }
    request = Request(
        SOURCE_URL,
        data=json.dumps(request_data).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Referer": "https://freesis.kofia.or.kr/",
            "User-Agent": "roboquant-cfd-research/1.0",
        },
    )
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        if response.geturl() != SOURCE_URL or response.headers.get_content_type() != "application/json":
            raise ValueError(f"Unexpected FreeSIS response for {object_name}")
        raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError(f"FreeSIS response exceeds the size limit for {object_name}")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("ds1"), list):
        raise ValueError(f"Invalid FreeSIS table for {object_name}")

    balances = {}
    for row in payload["ds1"]:
        if not isinstance(row, dict):
            raise ValueError(f"Invalid FreeSIS row for {object_name}")
        value = row.get("TMPV1")
        if not isinstance(value, str) or re.fullmatch(r"[0-9]{8}", value) is None:
            raise ValueError(f"Invalid FreeSIS date for {object_name}")
        day = date.fromisoformat(value)
        if not first <= day <= last or day in balances:
            raise ValueError(f"FreeSIS date {day} is outside the interval or duplicated for {object_name}")

        amounts = [row.get(f"TMPV{column}") for column in range(2, 13)]
        if any(type(amount) is not int or amount < 0 for amount in amounts):
            raise ValueError(f"Invalid FreeSIS KRW amount on {day} for {object_name}")
        total = amounts[-1]
        if total != sum(amounts[:-1]):
            raise ValueError(f"FreeSIS KRW total does not reconcile on {day} for {object_name}")
        balances[day] = {
            "date": day.isoformat(),
            "total_krw": total,
            "kospi_krw": row["TMPV2"] + row["TMPV7"],
            "kosdaq_krw": row["TMPV3"] + row["TMPV8"],
            "buy_krw": sum(amounts[:5]),
            "sell_krw": sum(amounts[5:10]),
            "kospi_buy_krw": row["TMPV2"],
            "kospi_sell_krw": row["TMPV7"],
            "kosdaq_buy_krw": row["TMPV3"],
            "kosdaq_sell_krw": row["TMPV8"],
        }
    return [balances[day] for day in sorted(balances)]


def fetch_total(definition: str, first: str, last: str) -> list[dict]:
    """Fetch one FreeSIS definition so a failure cannot mask the other one."""
    first_day, last_day = _iso_date(first), _iso_date(last)
    if first_day > last_day:
        raise ValueError("First CFD query date must not be after the last date")
    return _fetch_metric(METRIC_OBJECTS[definition], first_day, last_day)


def fetch_totals(first: str, last: str) -> dict[str, list[dict]]:
    """Fetch inclusive ISO dates, with one request per margin treatment.

    The unprefixed fields cover all reported markets, including overseas and
    other markets. The prefixed fields cover the two KRX markets separately.
    Buy and sell are outstanding notional balances. Gross totals are retained
    for callers that use the sum of both sides. Observations are ordered by date.
    """
    series = {metric: fetch_total(metric, first, last) for metric in METRIC_OBJECTS}
    if [row["date"] for row in series["with_margin"]] != [row["date"] for row in series["without_margin"]]:
        raise ValueError("FreeSIS CFD metrics do not cover the same dates")
    return series


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--first", required=True, help="First observation date, YYYY-MM-DD")
    parser.add_argument("--last", required=True, help="Last observation date, YYYY-MM-DD")
    parser.add_argument("--output", type=Path, default=DEFAULT_PRIVATE_OUTPUT, help="Private JSON snapshot path")
    args = parser.parse_args(argv)
    if args.output.resolve().is_relative_to((ROOT / "docs").resolve()):
        parser.error("The CFD snapshot must be written outside the public docs directory")
    try:
        series = fetch_totals(args.first, args.last)
        atomic_json(args.output, {"generated_at": datetime.now(UTC).isoformat(), "series": series})
    except (OSError, ValueError) as exc:
        parser.exit(1, f"CFD collection failed: {exc}\n")
    print(f"Saved {len(series['with_margin'])} observation dates per metric to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

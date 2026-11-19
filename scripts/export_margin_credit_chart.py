#!/usr/bin/env python3
"""Export public market credit balances from the research SQLite for Pages."""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

MARKETS = ("KOSPI", "KOSDAQ")
SOURCE = "금융투자협회 FreeSIS"
SOURCE_URL = ("https://freesis.kofia.or.kr/stat/FreeSIS.do"
              "?parentDivId=MSIS10000000000000&serviceId=STATSCU0100000140")
POINTS_PER_MARKET = 90
QUERY = """
WITH observed AS (
    SELECT market, observation_date, value, utc_stamp(received_at) AS received_utc, published_at
    FROM session_input_observations
    WHERE series = 'MARGIN_CREDIT_BALANCE'
      AND source = 'kofia_freesis_margin_credit' AND unit = 'KRW'
      AND market IN ('KOSPI', 'KOSDAQ')
      AND (is_final IS NULL OR is_final = 1)
      AND observation_date <= ?
      AND typeof(value) IN ('integer', 'real') AND value > 0
), versions AS (
    SELECT market, observation_date, value, received_utc,
           DENSE_RANK() OVER (
               PARTITION BY market, observation_date ORDER BY received_utc DESC
           ) AS recency
    FROM observed
    WHERE received_utc <= ? AND (published_at IS NULL OR utc_stamp(published_at) <= ?)
)
SELECT market, observation_date, MIN(value), received_utc
FROM versions
WHERE recency = 1
GROUP BY market, observation_date, received_utc
HAVING COUNT(DISTINCT value) = 1
ORDER BY market, observation_date DESC
"""


def utc_stamp(value) -> str | None:
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is not None:
            return stamp.astimezone(UTC).isoformat(timespec="microseconds")
    except (TypeError, ValueError, OverflowError):
        return None
    return None


def build_chart(sqlite: Path, *, as_of: datetime) -> dict:
    if as_of.utcoffset() is None:
        raise ValueError("As-of time requires an explicit timezone")
    cutoff = as_of.astimezone(UTC).isoformat(timespec="microseconds")
    day = as_of.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat()
    markets = {market: [] for market in MARKETS}
    receipts = []
    with closing(sqlite3.connect(sqlite.resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.create_function("utc_stamp", 1, utc_stamp, deterministic=True)
        for market, observed, balance, received in connection.execute(QUERY, (day, cutoff, cutoff)):
            points = markets[market]
            if len(points) == POINTS_PER_MARKET:
                continue
            if date.fromisoformat(observed).isoformat() != observed or not math.isfinite(balance):
                raise ValueError(f"Invalid {market} credit observation")
            points.append({"date": observed, "balance_krw": int(balance)})
            receipts.append(received)
    for market, points in markets.items():
        if not points:
            raise ValueError(f"No published credit balance is available for {market}")
        points.reverse()
    return {
        "schema_version": "margin_credit_chart_v1",
        "unit": "KRW",
        "source": SOURCE,
        "source_url": SOURCE_URL,
        "snapshot_received_at": max(receipts),
        "markets": markets,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True, help="Research session-input database (read only)")
    parser.add_argument("--output", type=Path, default=Path("docs/data/margin_credit_chart.json"))
    parser.add_argument("--as-of", type=datetime.fromisoformat, default=datetime.now(UTC))
    args = parser.parse_args()
    if args.output.resolve() == args.sqlite.resolve():
        parser.error("Chart output and SQLite input must be different paths")
    try:
        chart = build_chart(args.sqlite, as_of=args.as_of)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=args.output.parent) as stage:
            temporary = Path(stage) / args.output.name
            temporary.write_text(json.dumps(chart, ensure_ascii=False, separators=(",", ":")) + "\n",
                                 encoding="utf-8")
            temporary.replace(args.output)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

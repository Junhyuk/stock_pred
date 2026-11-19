#!/usr/bin/env python3
"""Append canonical rate, investor flow and margin credit snapshots to SQLite."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roboquant.index_forecast.session_input_store import KOFIA_CREDIT_SOURCE, MARGIN, append_inputs


def kofia_credit_rows(rows, received_at: str) -> pd.DataFrame:
    title, units, categories, markets = (next(rows, ()) for _ in range(4))
    if (title[:1] != ("신용공여 잔고 추이",) or units[:1] != ("[단위 :백만원]",)
            or categories[1:2] != ("신용거래융자",) or markets[1:4] != ("전체", "유가증권", "코스닥")):
        raise ValueError("Expected the KOFIA credit balance export with amounts in KRW millions")
    result = []
    dates = set()
    for line in rows:
        if not any(value is not None for value in line):
            continue
        try:
            day = datetime.strptime(line[0], "%Y/%m/%d").date().isoformat()
            total, kospi, kosdaq = (int(str(amount).replace(",", "")) for amount in line[1:4])
        except (ValueError, TypeError, IndexError) as exc:
            raise ValueError("Invalid date or amount in KOFIA credit export") from exc
        if day in dates or min(total, kospi, kosdaq) < 0 or abs(total - kospi - kosdaq) > 1:
            raise ValueError(f"Duplicate date or inconsistent KOFIA balances for {day}")
        dates.add(day)
        for market, amount in (("GLOBAL", total), ("KOSPI", kospi), ("KOSDAQ", kosdaq)):
            result.append({
                "market": market, "series": MARGIN, "observation_date": day,
                "value": amount * 1_000_000, "unit": "KRW", "source": KOFIA_CREDIT_SOURCE,
                "received_at": received_at, "published_at": "", "is_final": None,
                "availability_policy": "historical_dated_credit",
            })
    if not result:
        raise ValueError("KOFIA credit export contains no balance observations")
    return pd.DataFrame(result)


def read_kofia_credit(path: Path, received_at: str) -> pd.DataFrame:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise ValueError("Reading official KOFIA XLSX exports requires openpyxl") from exc
    with path.open("rb") as stream:
        workbook = load_workbook(stream, read_only=True, data_only=True)
        try:
            sheet = workbook["신용공여 잔고 추이"]
            # FreeSIS exports can declare A1:A1 even when they contain many rows.
            sheet.reset_dimensions()
            return kofia_credit_rows(iter(sheet.values), received_at)
        finally:
            workbook.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Dedicated research observations SQLite")
    parser.add_argument("snapshots", type=Path, nargs="*", help="Canonical CSV observations")
    parser.add_argument("--kofia-credit-xlsx", type=Path, help="Official daily FreeSIS credit balance export")
    parser.add_argument("--received-at", help="KOFIA export receipt time with an explicit timezone")
    args = parser.parse_args()
    if bool(args.kofia_credit_xlsx) != bool(args.received_at):
        parser.error("--kofia-credit-xlsx and --received-at must be supplied together")
    if not args.snapshots and not args.kofia_credit_xlsx:
        parser.error("Provide at least one canonical CSV or a KOFIA credit XLSX")
    frames = [pd.read_csv(path, dtype=str, keep_default_na=False) for path in args.snapshots]
    if args.kofia_credit_xlsx:
        frames.append(read_kofia_credit(args.kofia_credit_xlsx, args.received_at))
    observations = pd.concat(frames, ignore_index=True)
    inserted = append_inputs(args.db, observations)
    print(json.dumps({"db": str(args.db), "input_rows": len(observations), "inserted": inserted}))


if __name__ == "__main__":
    main()

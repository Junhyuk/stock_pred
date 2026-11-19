#!/usr/bin/env python3
"""Audit frozen next-session publications and run offline research candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from roboquant.index_forecast.reliable_inputs import session_schedule

CALENDAR_OVERRIDES = {
    "2024-11-14": {
        "open_kst": "10:00",
        "close_kst": "16:30",
        "sources": {
            "KOSPI": "https://kind.krx.co.kr/external/2024/10/31/000086/20241031000185/99303.htm",
            "KOSDAQ": "https://www.hanwhawm.com/main/bbs/indexView.cmd?cc_gubun=1&mode=center&nn_id=66702&num=1975&pageVal=1&vc_bid=notice",
        },
    },
    "2025-11-13": {
        "open_kst": "10:00",
        "close_kst": "16:30",
        "sources": {
            "KOSPI": "https://kind.krx.co.kr/external/2025/10/30/000102/20251030000137/99303.htm",
            "KOSDAQ": "https://money2.daishin.com/e5/mboard/ptype_basic/basic_001/DW_Basic_Read.aspx?boardseq=114&m=1108&p=1385&page=1&searchString=&seq=17899&v=2247",
        },
    },
    "2026-11-19": {
        "open_kst": "10:00",
        "close_kst": "16:30",
        "sources": {
            "KOSPI": "https://kind.krx.co.kr/external/2026/11/05/000141/20261105000216/99303.htm",
            "KOSDAQ": "https://kind.krx.co.kr/external/2026/11/05/000145/20261104001218/70780.htm",
        },
    },
}
MARKETS = ("KOSPI", "KOSDAQ")
PRICE_KEY = ["index_code", "session_date"]


def aware_stamp(value: str | pd.Timestamp) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("An explicit timezone is required")
    return stamp.tz_convert("UTC")


def audit_sessions(as_of: pd.Timestamp) -> pd.DataFrame:
    end = aware_stamp(as_of).tz_convert("Asia/Seoul").date() + timedelta(days=14)
    schedule, _ = session_schedule("2019-01-02", end)
    for date, hours in CALENDAR_OVERRIDES.items():
        mask = schedule["date"].eq(date)
        for field, column in (("open_kst", "open_at"), ("close_kst", "close_at")):
            schedule.loc[mask, column] = aware_stamp(f"{date}T{hours[field]}:00+09:00")
    return schedule


def load_verified_archive(path: Path, receipt_path: Path, as_of: pd.Timestamp) -> dict:
    receipt = json.loads(receipt_path.read_bytes())
    if (receipt.get("schema_version") != "reliable_publication_v1"
            or receipt.get("status") != "research_only" or receipt.get("replay") is not False):
        raise ValueError("Expected a verified research publication receipt")
    if aware_stamp(receipt["publication_checked_at"]) > aware_stamp(as_of):
        raise ValueError("Archive publication is after the audit cutoff")
    raw = path.read_bytes()
    run_id = receipt["run_id"]
    if hashlib.sha256(raw).hexdigest() != receipt["payload_checksums"].get(f"archive/{run_id}.json"):
        raise ValueError("Forecast archive checksum differs from the receipt")
    archive = json.loads(raw)
    if archive.get("run_id") != run_id or archive.get("replay") is not False:
        raise ValueError("Forecast archive identity differs from the receipt")
    return archive


def read_prices(path: Path) -> pd.DataFrame:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        return pd.read_sql_query(
            """SELECT index_code, session_date, close, published_at, ingested_at, source
               FROM index_eod WHERE index_code IN (?, ?) ORDER BY index_code, session_date""",
            conn,
            params=MARKETS,
        )


def _checked_prices(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    known = data["index_code"].isin(MARKETS)
    date = pd.to_datetime(data["session_date"], format="%Y-%m-%d", errors="coerce")
    data["session_date"] = date.dt.strftime("%Y-%m-%d")
    data["close"] = pd.to_numeric(data["close"], errors="coerce")
    if (not known.all() or date.isna().any() or data.duplicated(PRICE_KEY).any()
            or not (np.isfinite(data["close"]) & data["close"].gt(0)).all()):
        raise ValueError("Close inputs must contain unique market/date rows with positive finite prices")
    return data


def _available_closes(frame: pd.DataFrame, cutoff: pd.Timestamp) -> pd.DataFrame:
    data = frame.copy()
    available = pd.Series(True, index=data.index)
    for column in ("published_at", "observed_at"):
        if column not in data:
            continue
        text = data[column].astype("string").str.strip().replace("", pd.NA)
        if column == "published_at":
            # Imported transaction dates are not publication instants.
            text = text.mask(text.str.fullmatch(r"\d{4}-\d{2}-\d{2}", na=False))
        zoned = text.str.contains(r"(?:Z|[+-]\d{2}:?\d{2})$", regex=True, na=False)
        stamp = pd.to_datetime(text.where(zoned), utc=True, format="mixed", errors="coerce")
        available &= text.isna() | stamp.le(cutoff)
        data[column] = stamp
    return data.loc[available]


def merge_closes(historical: pd.DataFrame, crosscheck: pd.DataFrame, *, as_of: pd.Timestamp) -> pd.DataFrame:
    """Join retrospective close files, marking disagreements explicitly."""
    historical = _checked_prices(historical)
    stamps = ["published_at", "observed_at"]
    crosscheck = _checked_prices(crosscheck[PRICE_KEY + ["close"] + [c for c in stamps if c in crosscheck]])
    keys = pd.concat([historical[PRICE_KEY], crosscheck[PRICE_KEY]]).drop_duplicates()
    cutoff = aware_stamp(as_of)
    historic_ready = _available_closes(historical, cutoff)
    crosscheck_ready = _available_closes(crosscheck, cutoff).rename(
        columns={c: f"crosscheck_{c}" for c in ["close", *stamps] if c in crosscheck},
    )
    data = historic_ready.merge(crosscheck_ready, on=PRICE_KEY, how="outer", validate="one_to_one")
    both = data["close"].notna() & data["crosscheck_close"].notna()
    agrees = both & np.isclose(data["close"], data["crosscheck_close"], rtol=0, atol=0.005)
    conflict = both & ~agrees
    data["status"] = np.select([conflict, agrees], ["conflict", "crosschecked"], default="single_source")
    data["close"] = data["crosscheck_close"].combine_first(data["close"]).mask(conflict)
    for column in stamps:
        other = f"crosscheck_{column}"
        if other in data:
            known = [c for c in (column, other) if c in data]
            data[column] = data[known].max(axis=1)
    result = keys.merge(data.drop(columns="crosscheck_close"), on=PRICE_KEY, how="left", validate="one_to_one")
    result["status"] = result["status"].fillna("price_not_observed_as_of")
    return result.sort_values(PRICE_KEY).reset_index(drop=True)


def read_legacy_rows(path: Path) -> pd.DataFrame:
    import duckdb

    with duckdb.connect(str(path), read_only=True) as conn:
        return conn.execute(
            """SELECT market, target_date, asof_date, created_at, model_version
               FROM market_outlook_forecasts
               WHERE market IN ('KOSPI', 'KOSDAQ')
                 AND horizon IN ('TODAY', 'NEXT_TRADING_DAY', '1D')"""
        ).df()


def _flat_audit_rows(audit):
    for market, breakdown in audit["markets"].items():
        for row in breakdown["daily"]:
            yield {"market": market, "track": "target_session", **row}
        for category in ("historical", "cohorts"):
            for entry in breakdown[category]:
                for row in entry["daily"]:
                    yield {
                        "market": market,
                        "track": category,
                        "model": entry.get("model"),
                        "role": entry.get("role"),
                        "protocol_hash": entry.get("protocol_hash"),
                        **row,
                    }


def write_report(output: Path, report: dict) -> None:
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    table = pd.DataFrame(_flat_audit_rows(report["audit"]))
    if {"expected_index", "actual_close"} <= set(table):
        table["error_points"] = table["expected_index"] - table["actual_close"]
    csv = table.to_csv(index=False)
    output.mkdir(parents=True, exist_ok=False)
    (output / "accuracy.json").write_text(serialized + "\n", encoding="utf-8")
    (output / "accuracy_daily.csv").write_text(csv, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, required=True, help="Existing index_eod database (read only)")
    parser.add_argument("--closes-csv", type=Path, required=True, help="Price cross-check snapshot")
    parser.add_argument("--closes-observed-at", required=True, help="When the price snapshot was obtained (with timezone)")
    parser.add_argument("--archive", type=Path, required=True, help="Full, frozen forecast archive JSON")
    parser.add_argument("--publication", type=Path, required=True, help="Corresponding publication receipt JSON")
    parser.add_argument("--ledger", type=Path, required=True, help="Forward history JSON")
    parser.add_argument("--legacy-db", type=Path, help="Optional market_outlook forecasts DuckDB (read only)")
    parser.add_argument("--session-input-db", type=Path, help="Optional rate, flow and margin credit observations (read only)")
    parser.add_argument("--as-of", required=True, help="Audit cutoff with timezone")
    parser.add_argument("--output-dir", type=Path, required=True, help="New directory for audit results")
    parser.add_argument("--audit-only", action="store_true", help="Compute price comparisons without model fits")
    args = parser.parse_args()

    from roboquant.index_forecast.accuracy_audit import audit_recent_accuracy

    cutoff = aware_stamp(args.as_of)
    closes_observed_at = aware_stamp(args.closes_observed_at)
    if closes_observed_at > cutoff:
        raise ValueError("Close snapshot arrived after the audit cutoff")
    archive = load_verified_archive(args.archive, args.publication, cutoff)
    ledger = json.loads(args.ledger.read_bytes())
    sessions = audit_sessions(cutoff)
    prices = merge_closes(read_prices(args.sqlite), pd.read_csv(args.closes_csv), as_of=cutoff)
    legacy = read_legacy_rows(args.legacy_db) if args.legacy_db else None
    audit = audit_recent_accuracy(prices, archive, ledger, sessions=sessions, as_of=cutoff, legacy_rows=legacy)
    report = {
        "schema_version": "robo_stock_accuracy_v1",
        "as_of": cutoff.isoformat(),
        "archive_run_id": archive["run_id"],
        "archive_sha256": hashlib.sha256(args.archive.read_bytes()).hexdigest(),
        "close_csv_sha256": hashlib.sha256(args.closes_csv.read_bytes()).hexdigest(),
        "retrospective_close_snapshot_observed_at": closes_observed_at.isoformat(),
        "history_hash": ledger["history_hash"],
        "calendar_overrides": CALENDAR_OVERRIDES,
        "production_weight": 0.0,
        "audit": audit,
    }
    if not args.audit_only:
        from roboquant.index_forecast.accuracy_research import evaluate_accuracy_research

        report["model_research"] = evaluate_accuracy_research(prices, archive, sessions=sessions, as_of=cutoff)
        if args.session_input_db:
            from roboquant.index_forecast.fundamental_research import evaluate_fundamental_research
            from roboquant.index_forecast.session_input_store import read_inputs

            observations = read_inputs(args.session_input_db, as_of=cutoff)
            report["additional_inputs"] = evaluate_fundamental_research(
                prices, archive, observations, sessions=sessions, as_of=cutoff,
            )
    write_report(args.output_dir, report)
    print(f"Accuracy results written to {args.output_dir}")


if __name__ == "__main__":
    main()

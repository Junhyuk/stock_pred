#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.krx import fetch_benchmark
from roboquant.db import append_dedup_table, connect_database

MARKET_INDICES = (
    ("1001", "KOSPI"),
    ("2001", "KOSDAQ"),
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect KOSPI/KOSDAQ benchmark index OHLCV.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    market_cfg = config.get("market", {})
    start_date = args.start or str(market_cfg.get("start_date", "2019-01-01"))
    end_date = args.end or market_cfg.get("end_date") or datetime.now().date().isoformat()

    conn = connect_database(get_database_path(config))
    total = 0
    try:
        for code, name in MARKET_INDICES:
            try:
                frame = fetch_benchmark(code, name, start_date, end_date)
            except Exception as exc:
                _record_failure(conn, name, exc, end_date)
                print(f"{name}: failed: {exc}")
                continue
            if frame.empty:
                print(f"{name}: empty")
                continue
            append_dedup_table(conn, "benchmark_daily", frame, ["date", "benchmark"])
            total += len(frame)
            print(f"{name}: {len(frame)} rows")
    finally:
        conn.close()
    print(f"benchmark_daily rows upserted: {total}")


def _record_failure(conn, benchmark: str, error: Exception, target_date: str) -> None:
    failure = collection_failure_row(
        step="collect_market_indices",
        source="pykrx_or_fdr",
        error=error,
        symbol=benchmark,
        target_date=target_date,
    )
    append_dedup_table(
        conn,
        "collection_failures",
        failure,
        ["collected_at", "step", "source", "symbol", "error_message"],
    )


if __name__ == "__main__":
    main()

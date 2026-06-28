#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.collector import snapshots_to_frame
from roboquant.global_market.providers.yfinance_poc import YFinancePocProvider
from roboquant.global_market.regime import resolve_cutoff


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect premarket global market snapshots.")
    parser.add_argument("--config", default="configs/global_market.yaml")
    parser.add_argument("--cutoff", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    cutoff = resolve_cutoff(args.cutoff, config.get("regime", {}).get("cutoff_time_kst", "08:00"))
    conn = connect_database(get_database_path(config))
    try:
        symbols = config.get("symbols", {}).get("yfinance_intraday", [])
        try:
            snapshots = YFinancePocProvider().get_intraday_snapshots(symbols, cutoff)
        except Exception as exc:
            append_dedup_table(
                conn,
                "collection_failures",
                collection_failure_row(
                    step="collect_premarket_snapshot",
                    source="yfinance_poc",
                    error=exc,
                    target_date=cutoff.date().isoformat(),
                ),
                ["collected_at", "step", "source", "error_message"],
            )
            raise
        frame = snapshots_to_frame(snapshots)
        if frame.empty:
            print("No premarket snapshots collected.")
            return
        append_dedup_table(
            conn,
            "global_market_intraday_snapshot",
            frame,
            ["snapshot_at", "symbol", "source_name"],
        )
        print(f"global_market_intraday_snapshot rows upserted: {len(frame)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

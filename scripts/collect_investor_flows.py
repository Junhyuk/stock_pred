#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.investor_flows import fetch_investor_flows
from roboquant.db import append_dedup_table, connect_database
from roboquant.utils import today_string


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect KRX investor flow data.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    market_cfg = config.get("market", {})
    end_date = args.end or market_cfg.get("end_date") or today_string()
    start_date = args.start or end_date

    try:
        flows = fetch_investor_flows(start_date, end_date, market_cfg.get("markets"))
        append_dedup_table(conn, "investor_flows_daily", flows, ["date", "symbol"])
        print(f"investor_flows_daily rows: {len(flows)}")
    except Exception as exc:
        failure = collection_failure_row(
            step="collect_investor_flows",
            source="pykrx",
            error=exc,
            target_date=end_date,
        )
        append_dedup_table(
            conn,
            "collection_failures",
            failure,
            ["collected_at", "step", "source", "error_message"],
        )
        raise


if __name__ == "__main__":
    main()


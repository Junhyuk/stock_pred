#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.loaders import load_analyst_reports, load_benchmark, load_prices
from roboquant.db import append_dedup_table, connect_database
from roboquant.features.analyst_features import compute_analyst_report_outcomes


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update realized outcomes for analyst reports.")
    parser.add_argument("--config", default="configs/poc.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))

    reports = load_analyst_reports(conn)
    prices = load_prices(conn)
    benchmark = load_benchmark(conn)
    outcomes = compute_analyst_report_outcomes(reports, prices, benchmark)
    append_dedup_table(conn, "analyst_report_outcomes", outcomes, ["report_id"])
    print(f"analyst_report_outcomes rows updated: {len(outcomes)}")


if __name__ == "__main__":
    main()

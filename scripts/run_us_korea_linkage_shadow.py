#!/usr/bin/env python3
"""Run the Nasdaq/SOX market linkage shadow backtest and latest candidate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_us_linkage import run_market_us_linkage_shadow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Nasdaq/SOX close-loop shadow.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--skip-live", action="store_true", help="Backtest only; do not create a latest candidate row.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        result = run_market_us_linkage_shadow(
            conn,
            config,
            asof_date=args.date,
            include_live=not args.skip_live,
        )
        print(json.dumps(result, ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

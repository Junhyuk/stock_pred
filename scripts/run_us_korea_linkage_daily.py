#!/usr/bin/env python3
"""Run the close or premarket Nasdaq/SOX linkage stage without touching production."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_us_linkage import (
    run_market_us_linkage_shadow,
    update_market_us_linkage_feedback,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Nasdaq/SOX linkage by daily stage.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--stage", choices=("close", "premarket"), required=True)
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        if args.stage == "close":
            result = update_market_us_linkage_feedback(conn, config)
        else:
            result = run_market_us_linkage_shadow(conn, config, asof_date=args.date, include_live=True)
        print(json.dumps({"stage": args.stage, **result}, ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

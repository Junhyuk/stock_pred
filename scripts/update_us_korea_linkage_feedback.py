#!/usr/bin/env python3
"""Attach matured KRX closes to live Nasdaq/SOX shadow forecasts."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_us_linkage import update_market_us_linkage_feedback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update Nasdaq/SOX shadow close-loop feedback.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        print(json.dumps(update_market_us_linkage_feedback(conn, config), ensure_ascii=False, default=str))
    finally:
        conn.close()


if __name__ == "__main__":
    main()

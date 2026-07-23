#!/usr/bin/env python3
"""Run the purged KOSPI next-session direction diagnostic (shadow only)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_outlook_direction_shadow import run_market_outlook_direction_shadow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate KOSPI direction candidates with a purged expanding walk-forward."
    )
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        result = run_market_outlook_direction_shadow(conn, config, asof_date=args.date)
    finally:
        conn.close()
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.signals.market_move_explanations import refresh_market_move_explanations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build market and Top50 2% move explanations.")
    parser.add_argument("--config", default="configs/today_update.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = refresh_market_move_explanations(conn, config, asof_date=args.date)
        triggered = int(frame["triggered"].sum()) if not frame.empty else 0
        asof = None if frame.empty else frame["asof_date"].max()
        print(f"market_move_explanations rows: {len(frame)} triggered: {triggered} asof: {asof}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

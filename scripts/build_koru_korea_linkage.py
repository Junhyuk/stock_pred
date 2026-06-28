#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.koru import refresh_koru_korea_linkage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KORU/EWY/KOSPI/KOSDAQ linkage features.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = refresh_koru_korea_linkage(conn, config=config, asof_date=args.date)
    finally:
        conn.close()
    latest = None if frame.empty else frame["trade_date"].max()
    shock_count = 0 if frame.empty else int(frame["koru_market_shock_flag"].fillna(False).astype(bool).sum())
    print(f"koru_korea_linkage rows: {len(frame)}")
    print(f"latest trade_date: {latest}")
    print(f"market shock rows: {shock_count}")


if __name__ == "__main__":
    main()

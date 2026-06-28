#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.us_sector_linkage import refresh_us_sector_linkage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KOSPI/KOSDAQ to US similar-sector linkage features.")
    parser.add_argument("--config", default="configs/global_market.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = refresh_us_sector_linkage(conn, config=config, asof_date=args.date)
    finally:
        conn.close()

    if frame.empty:
        print("us_sector_linkage_daily rows: 0")
        return
    latest = frame["trade_date"].max()
    print(f"us_sector_linkage_daily rows: {len(frame)} asof: {latest}")
    latest_frame = frame[frame["trade_date"].astype(str).eq(str(latest))]
    for _, row in latest_frame.sort_values("domestic_sector").iterrows():
        print(
            f"{row['domestic_sector']}: proxy={row['primary_proxy']} "
            f"1d={row['us_sector_return_1d']} impact={row['us_sector_impact_score']}"
        )


if __name__ == "__main__":
    main()

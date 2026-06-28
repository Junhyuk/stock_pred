#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_outlook import build_market_outlook_dataset, resolve_market_outlook_asof


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KOSPI/KOSDAQ short-horizon market outlook features.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config), read_only=True, initialize_schema=False)
    try:
        asof = resolve_market_outlook_asof(conn, args.date)
        frame = build_market_outlook_dataset(conn, config=config, asof_date=args.date)
    finally:
        conn.close()

    latest = frame[frame["asof_date"].astype(str) == asof.isoformat()] if not frame.empty else frame
    targets = (
        latest[["horizon", "market", "target_date"]].to_dict(orient="records")
        if not latest.empty
        else []
    )
    print(f"market_outlook_features rows: {len(frame)}")
    print(f"market_outlook_features asof: {asof.isoformat()}")
    print(f"market_outlook_features latest_targets: {targets}")


if __name__ == "__main__":
    main()

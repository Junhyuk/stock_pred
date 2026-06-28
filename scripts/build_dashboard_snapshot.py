#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.dashboard.dashboard_service import build_dashboard_snapshot
from roboquant.db import connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build v7 dashboard snapshot JSON cache.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--horizon", default="3M")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    conn = connect_database(get_database_path(config))
    snapshot = build_dashboard_snapshot(conn, horizon=args.horizon)
    print(f"dashboard_snapshot date: {snapshot['snapshot_date']}")


if __name__ == "__main__":
    main()

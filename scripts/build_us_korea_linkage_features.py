#!/usr/bin/env python3
"""Build cutoff-safe Nasdaq/SOX linkage features for KOSPI/KOSDAQ."""

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
    build_market_us_linkage_features,
    refresh_market_us_linkage_features,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build Nasdaq/SOX-to-KRX linkage features.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest", help="Latest or maximum KRX as-of date.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = build_market_us_linkage_features(conn, config, asof_date=args.date)
        refresh_market_us_linkage_features(conn, frame)
        print(
            json.dumps(
                {
                    "status": "ready" if not frame.empty else "not_ready",
                    "rows": int(len(frame)),
                    "markets": sorted(frame["market"].dropna().astype(str).unique().tolist()) if not frame.empty else [],
                    "prediction_date": str(frame["prediction_date"].max()) if not frame.empty else None,
                },
                ensure_ascii=False,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

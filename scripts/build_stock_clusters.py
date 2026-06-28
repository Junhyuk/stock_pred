#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.clustering.stock_clusters import build_stock_clusters, persist_stock_clusters
from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build latest KOSPI stock clusters.")
    parser.add_argument("--config", default="configs/samsung_demo.yaml")
    parser.add_argument("--horizon", default="3M")
    parser.add_argument("--clusters", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    conn = connect_database(get_database_path(config))
    features = conn.execute("SELECT * FROM features_daily WHERE horizon = ?", [args.horizon]).fetchdf()
    assignments, summaries = build_stock_clusters(
        features,
        horizon=args.horizon,
        n_clusters=args.clusters,
    )
    persist_stock_clusters(conn, assignments, summaries)
    print(f"stock_clusters rows: {len(assignments)}")
    print(f"cluster_summary rows: {len(summaries)}")


if __name__ == "__main__":
    main()

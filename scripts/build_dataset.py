#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, get_horizons, load_config
from roboquant.data.loaders import (
    load_benchmark,
    load_investor_flows,
    load_market_metrics,
    load_prices,
)
from roboquant.data.validators.quality import validate_prices
from roboquant.db import connect_database, replace_table
from roboquant.features.build_feature_matrix import build_feature_matrix
from roboquant.labels.make_labels import compute_labels


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build feature and label tables.")
    parser.add_argument("--config", default="configs/poc.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    horizons = get_horizons(config)

    prices = load_prices(conn)
    quality = validate_prices(prices)
    for warning in quality.warnings:
        print(f"warning: {warning}")
    quality.raise_for_errors()

    benchmark = load_benchmark(conn)
    market_metrics = load_market_metrics(conn)
    investor_flows = load_investor_flows(conn)
    features = build_feature_matrix(
        prices,
        horizons,
        investor_flows=investor_flows,
        market_metrics=market_metrics,
        missing_factor_default=float(config.get("recommendation", {}).get("missing_factor_default", 0.5)),
    )
    labels = compute_labels(prices, benchmark, horizons)

    replace_table(conn, "features_daily", features)
    replace_table(conn, "labels", labels)
    print(f"features_daily rows: {len(features)}")
    print(f"labels rows: {len(labels)}")


if __name__ == "__main__":
    main()

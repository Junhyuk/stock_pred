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
    load_collection_failures,
    load_consensus_history,
    load_current_prediction_universe_symbols,
    load_investor_flows,
    load_koru_linkage,
    load_market_metrics,
    load_news_signals,
    load_prices,
    load_symbols,
    load_telegram_market_signals,
    load_us_sector_linkage,
)
from roboquant.data.validators.quality import validate_prices
from roboquant.db import connect_database, replace_table
from roboquant.features.build_feature_matrix import build_feature_matrix
from roboquant.labels.make_labels import compute_labels
from roboquant.reports.generate_report import render_data_quality_markdown, write_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build v2 feature matrix and labels.")
    parser.add_argument("--config", default="configs/poc.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    horizons = get_horizons(config)

    prices = _load_prices_for_config(conn, config)
    quality = validate_prices(prices)
    quality.raise_for_errors()
    market_metrics = load_market_metrics(conn)
    investor_flows = load_investor_flows(conn)
    consensus_history = load_consensus_history(conn)
    koru_linkage = load_koru_linkage(conn)
    telegram_market = load_telegram_market_signals(conn)
    news_signals = load_news_signals(conn)
    us_sector_linkage = load_us_sector_linkage(conn)
    symbols = load_symbols(conn)
    benchmark = load_benchmark(conn)

    features = build_feature_matrix(
        prices,
        horizons,
        investor_flows=investor_flows,
        market_metrics=market_metrics,
        consensus_history=consensus_history,
        koru_linkage=koru_linkage,
        telegram_market=telegram_market,
        news_signals=news_signals,
        us_sector_linkage=us_sector_linkage,
        symbols=symbols,
        config=config,
        missing_factor_default=float(config.get("recommendation", {}).get("missing_factor_default", 0.5)),
    )
    labels = compute_labels(prices, benchmark, horizons)
    replace_table(conn, "features_daily", features)
    replace_table(conn, "labels", labels)

    failures = load_collection_failures(conn)
    quality_report = render_data_quality_markdown(
        prices=prices,
        features=features,
        labels=labels,
        market_metrics=market_metrics,
        investor_flows=investor_flows,
        failures=failures,
        warnings=quality.warnings,
    )
    latest_date = features["date"].max() if not features.empty else "latest"
    report_path = Path(config["paths"]["report_dir"]) / f"data_quality_{latest_date}.md"
    write_text(report_path, quality_report)
    print(f"features_daily rows: {len(features)}")
    print(f"labels rows: {len(labels)}")
    print(f"data quality report: {report_path}")


def _load_prices_for_config(conn, config: dict) -> object:
    universe_rule = config.get("universe", {}).get("rule")
    if not universe_rule:
        return load_prices(conn)

    symbols = load_current_prediction_universe_symbols(conn, str(universe_rule))
    if not symbols:
        raise RuntimeError(f"No current prediction universe symbols found for rule={universe_rule}")

    prices = load_prices(conn, symbols=symbols)
    covered_symbols = set(prices["symbol"].astype(str).str.zfill(6)) if not prices.empty else set()
    missing = sorted(set(symbols).difference(covered_symbols))
    if missing:
        raise RuntimeError(f"Missing prices for prediction universe symbols: {missing}")
    return prices


if __name__ == "__main__":
    main()

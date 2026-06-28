#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, get_horizons, load_config
from roboquant.data.loaders import load_latest_features, load_symbols
from roboquant.db import append_dedup_table, connect_database
from roboquant.long_short import build_long_short_recommendations, render_long_short_report
from roboquant.signals.market_news_context import active_flow_themes, apply_flow_news_risk_flags
from roboquant.universe.symbols import load_prediction_universe_symbols, sync_prediction_universe_symbols
from roboquant.models.predict import predict_from_model_path
from roboquant.models.train import baseline_feature_predictions
from roboquant.reports.generate_report import write_text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate simulated long-short Top50 portfolios.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--horizon", default=None, help="2M or 1Y. Omit to generate configured long-short horizons.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    horizons = get_horizons(config)
    selected_horizons = _selected_horizons(config, horizons, args.horizon)
    if args.dry_run:
        print(
            "dry-run: long-short horizons="
            f"{selected_horizons}, config={args.config}, date={args.date}"
        )
        return

    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    report_dir = Path(config["paths"]["report_dir"])
    model_dir = Path(config["paths"]["model_dir"])
    universe_rule = str(config.get("universe", {}).get("rule", "prediction_top_market_cap"))
    synced = sync_prediction_universe_symbols(conn, universe_rule=universe_rule)
    if synced:
        print(f"synced {synced} prediction-universe symbols")
    symbols = load_symbols(conn)
    universe = load_prediction_universe_symbols(conn, universe_rule=universe_rule)
    flow_themes = active_flow_themes(conn)

    for horizon in selected_horizons:
        features = load_latest_features(conn, horizon, args.date)
        if features.empty:
            print(f"{horizon}: no features found")
            continue
        model_path = model_dir / horizon / "model.pkl"
        if model_path.exists():
            predictions = predict_from_model_path(model_path, features)
        else:
            print(f"{horizon}: model not found, using factor baseline")
            predictions = baseline_feature_predictions(features, horizon)
        if predictions.empty:
            print(f"{horizon}: no predictions generated")
            continue

        append_dedup_table(
            conn,
            "predictions",
            predictions,
            ["asof_date", "symbol", "horizon", "model_version"],
        )
        recommendations = build_long_short_recommendations(
            predictions,
            features=features,
            symbols=symbols,
            universe=universe,
            horizon=horizon,
            config=config,
            asof_date=args.date,
        )
        if recommendations.empty:
            print(f"{horizon}: no eligible long-short recommendations")
            continue

        recommendations = apply_flow_news_risk_flags(recommendations, flow_themes)
        conn.execute("DELETE FROM long_short_recommendations WHERE horizon = ?", [horizon])
        append_dedup_table(
            conn,
            "long_short_recommendations",
            recommendations,
            ["asof_date", "horizon", "market", "symbol", "side", "model_version"],
        )
        report = render_long_short_report(recommendations, horizon)
        write_text(report_dir / f"long_short_{horizon}.md", report)
        print(f"{horizon}: wrote {len(recommendations)} long-short rows")


def _selected_horizons(
    config: dict,
    horizons: dict[str, int],
    requested: str | None,
) -> list[str]:
    if requested:
        if requested not in horizons:
            raise ValueError(f"Unknown horizon {requested}. Config horizons: {sorted(horizons)}")
        return [requested]
    ls_cfg = config.get("long_short", {})
    configured = ls_cfg.get("horizons", {})
    if isinstance(configured, dict):
        selected = [
            str(configured.get("short")),
            str(configured.get("long")),
        ]
        selected = [horizon for horizon in selected if horizon and horizon in horizons]
        if selected:
            return selected
    pipeline = config.get("pipeline", {})
    pipeline_horizons = pipeline.get("long_short_horizons")
    if pipeline_horizons:
        return [horizon for horizon in pipeline_horizons if horizon in horizons]
    rebalance = ls_cfg.get("rebalance_frequency", {})
    if isinstance(rebalance, dict) and rebalance:
        return [horizon for horizon in rebalance if horizon in horizons]
    return [horizon for horizon in ("2M", "6M") if horizon in horizons]


if __name__ == "__main__":
    main()

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
from roboquant.koru import latest_koru_overlay_weights
from roboquant.models.predict import predict_from_model_path
from roboquant.models.train import baseline_feature_predictions
from roboquant.recommend.scorer import build_recommendations, score_predictions
from roboquant.reports.generate_report import (
    render_recommendations_html,
    render_recommendations_markdown,
    write_report_contexts,
    write_text,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate latest recommendation reports.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--horizon", default=None, help="Omit to generate all configured horizons.")
    parser.add_argument("--top-k", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    horizons = get_horizons(config)
    selected_horizons = [args.horizon] if args.horizon else _configured_horizons(config, horizons)
    report_dir = Path(config["paths"]["report_dir"])
    model_dir = Path(config["paths"]["model_dir"])
    rec_cfg = config.get("recommendation", {})
    market_cfg = config.get("market", {})
    top_k = args.top_k or int(rec_cfg.get("default_top_k", 20))
    symbols = load_symbols(conn)
    koru_overlay_weights = latest_koru_overlay_weights(conn)

    all_recommendations = []
    markdown_parts = []
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
        if not predictions.empty:
            append_dedup_table(
                conn,
                "predictions",
                predictions,
                ["asof_date", "symbol", "horizon", "model_version"],
            )

        scored = score_predictions(
            predictions,
            features,
            _weights_for_horizon(rec_cfg.get("weights"), koru_overlay_weights, horizon),
            missing_factor_default=float(rec_cfg.get("missing_factor_default", 0.5)),
        )
        recommendations = build_recommendations(
            scored,
            horizon=horizon,
            top_k=top_k,
            min_trading_value_20d=float(market_cfg.get("min_trading_value_20d", 1_000_000_000)),
            symbols=symbols,
            exclusion_thresholds=config.get("features", {}).get("exclusions"),
        )
        if recommendations.empty:
            print(f"{horizon}: no eligible recommendations")
            continue
        append_dedup_table(
            conn,
            "recommendations",
            recommendations[
                [
                    "asof_date",
                    "horizon",
                    "symbol",
                    "final_score",
                    "rank",
                    "reason_json",
                    "risk_flags_json",
                    "model_version",
                ]
            ],
            ["asof_date", "horizon", "symbol", "model_version"],
        )
        recommendations.to_csv(report_dir / f"recommendations_{horizon}.csv", index=False)
        write_report_contexts(recommendations, report_dir)
        all_recommendations.append(recommendations)
        markdown_parts.append(render_recommendations_markdown(recommendations))
        print(f"{horizon}: wrote {len(recommendations)} recommendations")

    if all_recommendations:
        combined = "\n\n---\n\n".join(markdown_parts)
        write_text(report_dir / "recommendations_latest.md", combined)
        write_text(report_dir / "recommendations_latest.html", render_recommendations_html(all_recommendations[0]))
    else:
        write_text(report_dir / "recommendations_latest.md", "# AI Robo Stock Recommendations\n\n추천 결과가 없습니다.\n")


def _configured_horizons(config: dict, horizons: dict[str, int]) -> list[str]:
    configured = config.get("pipeline", {}).get("report_horizons")
    if configured:
        return [str(horizon) for horizon in configured if str(horizon) in horizons]
    return list(horizons)


def _weights_for_horizon(base_weights: dict | None, koru_weights: dict[str, float], horizon: str) -> dict:
    weights = dict(base_weights or {})
    weights["koru_impact_score"] = float(koru_weights.get(str(horizon), 0.0) or 0.0)
    return weights


if __name__ == "__main__":
    main()

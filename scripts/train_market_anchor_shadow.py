#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.market_anchor_shadow import (
    default_market_anchor_model_path,
    fit_market_anchor_shadow_model,
    load_market_anchor_config,
    load_market_anchor_features,
    market_anchor_settings,
    save_market_anchor_shadow_model,
)
from roboquant.registry.model_registry import register_feature_set, register_model
from roboquant.utils import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train experimental KOSPI/KOSDAQ anchor quantile shadow model.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def run_train(config_path: str | Path, *, model_path: str | Path | None = None) -> dict:
    config = load_market_anchor_config(config_path)
    ensure_project_dirs(config)
    settings = market_anchor_settings(config)
    target = Path(model_path) if model_path else default_market_anchor_model_path(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = load_market_anchor_features(
            conn,
            feature_version=str(settings["feature_version"]),
            stage=str(settings["forecast_stage"]),
        )
        bundle = fit_market_anchor_shadow_model(frame, config)
        save_market_anchor_shadow_model(bundle, target)
        metrics_path = target.with_suffix(".metrics.json")
        write_json(
            metrics_path,
            {
                "model_name": bundle["model_name"],
                "model_version": bundle["model_version"],
                "production_weight": 0.0,
                "metrics": bundle["metrics"],
            },
        )
        register_feature_set(
            conn,
            f"{settings['feature_version']}_shadow",
            ["anchor_bundle", "cutoff_safe_global_optional", "final_anchor_flow_optional"],
            status="experimental",
            description="Point-in-time KOSPI/KOSDAQ anchor basket range features.",
        )
        dated = frame.dropna(subset=["asof_date"]).sort_values("asof_date")
        register_model(
            conn,
            model_name=str(bundle["model_name"]),
            model_type="lightgbm_quantile_conformal",
            feature_set_name=f"{settings['feature_version']}_shadow",
            label_name="next_trading_day_index_return",
            horizons=["TODAY"],
            artifact_path=str(target),
            metrics=dict(bundle["metrics"]),
            split={
                "train_start": None if dated.empty else dated["asof_date"].min(),
                "train_end": None if dated.empty else dated["asof_date"].max(),
            },
            status="experimental",
            production_weight=0.0,
            shadow_mode=True,
        )
        return {
            "status": "experimental",
            "model_name": bundle["model_name"],
            "model_version": bundle["model_version"],
            "model_path": str(target),
            "metrics": bundle["metrics"],
            "production_weight": 0.0,
        }
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_train(args.config, model_path=args.model_path), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

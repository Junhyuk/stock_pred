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
    build_market_anchor_shadow_forecasts,
    default_market_anchor_model_path,
    load_market_anchor_config,
    load_market_anchor_features,
    load_market_anchor_shadow_model,
    market_anchor_settings,
    refresh_market_anchor_shadow_forecasts,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate latest experimental KOSPI/KOSDAQ anchor range forecast.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def run_predict(config_path: str | Path, *, model_path: str | Path | None = None) -> dict:
    config = load_market_anchor_config(config_path)
    ensure_project_dirs(config)
    settings = market_anchor_settings(config)
    target = Path(model_path) if model_path else default_market_anchor_model_path(config)
    if not target.exists():
        raise FileNotFoundError(f"Anchor shadow artifact is missing: {target}")
    conn = connect_database(get_database_path(config))
    try:
        bundle = load_market_anchor_shadow_model(target)
        frame = load_market_anchor_features(
            conn,
            feature_version=str(settings["feature_version"]),
            stage=str(settings["forecast_stage"]),
        )
        history = conn.execute(
            """
            SELECT *
            FROM market_anchor_shadow_forecasts
            WHERE is_backtest = TRUE AND model_version = ?
            ORDER BY prediction_date
            """,
            [bundle["model_version"]],
        ).fetchdf()
        forecasts = build_market_anchor_shadow_forecasts(frame, bundle, calibration_history=history)
        refresh_market_anchor_shadow_forecasts(conn, forecasts)
        return {
            "status": "experimental" if not forecasts.empty else "not_ready",
            "forecast_rows": len(forecasts),
            "model_version": bundle["model_version"],
            "production_weight": 0.0,
        }
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_predict(args.config, model_path=args.model_path), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

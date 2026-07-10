#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.premarket_shadow import (
    FINAL_STAGE,
    FORECAST_STAGES,
    build_premarket_feature_history,
    build_premarket_shadow_forecasts,
    default_premarket_shadow_model_path,
    fit_premarket_shadow_model,
    refresh_premarket_feature_store,
    refresh_premarket_shadow_forecasts,
    save_premarket_shadow_model,
)
from roboquant.utils import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build cutoff-aware premarket shadow forecasts.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--stage", choices=sorted(FORECAST_STAGES), required=True)
    parser.add_argument("--date", default="latest", help="Latest completed KRX as-of date.")
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_shadow(
        args.config,
        stage=args.stage,
        asof_date=args.date,
        model_path=args.model_path,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


def run_shadow(
    config_path: str | Path,
    *,
    stage: str,
    asof_date: str = "latest",
    model_path: str | Path | None = None,
    dry_run: bool = False,
) -> dict:
    config = load_config(config_path)
    ensure_project_dirs(config)
    target_model_path = (
        Path(model_path)
        if model_path
        else default_premarket_shadow_model_path(config, stage)
    )
    if dry_run:
        return {
            "status": "dry_run",
            "stage": stage,
            "asof_date": asof_date,
            "database": str(get_database_path(config)),
            "model_path": str(target_model_path),
            "production_weight": 0.0,
        }

    conn = connect_database(get_database_path(config))
    try:
        features = build_premarket_feature_history(
            conn,
            config,
            stage=stage,
            asof_date=asof_date,
        )
        refresh_premarket_feature_store(conn, features)
        bundle = fit_premarket_shadow_model(features, stage=stage)
        save_premarket_shadow_model(bundle, target_model_path)
        metrics_path = target_model_path.with_suffix(".metrics.json")
        write_json(
            metrics_path,
            {
                "model_version": bundle["model_version"],
                "forecast_stage": stage,
                "production_weight": 0.0,
                "metrics": bundle["metrics"],
            },
        )
        forecasts = build_premarket_shadow_forecasts(features, bundle)
        refresh_premarket_shadow_forecasts(conn, forecasts)
        latest = features.sort_values("asof_date").tail(2)
        return {
            "status": "ready" if not forecasts.empty else "not_ready",
            "reason": (
                None
                if not forecasts.empty
                else "final US close is incomplete" if stage == FINAL_STAGE else "no latest feature rows"
            ),
            "stage": stage,
            "feature_rows": len(features),
            "forecast_rows": len(forecasts),
            "prediction_dates": sorted(
                {str(value) for value in latest.get("prediction_date", [])}
            ),
            "is_final_close": bool(latest["is_final_close"].all()) if not latest.empty else False,
            "model_path": str(target_model_path),
            "model_version": bundle["model_version"],
            "production_weight": 0.0,
        }
    finally:
        conn.close()


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import (
    ensure_project_dirs,
    get_database_path,
    get_feature_columns,
    get_horizons,
    load_config,
)
from roboquant.data.loaders import load_modeling_dataset
from roboquant.db import append_dedup_table, connect_database
from roboquant.koru import KORU_TRAINING_FEATURE_COLUMNS
from roboquant.models.train import evaluate_predictions, save_model_bundle, train_horizon_model
from roboquant.signals.news_signals import NEWS_TRAINING_FEATURE_COLUMNS
from roboquant.signals.telegram_signals import TELEGRAM_TRAINING_FEATURE_COLUMNS
from roboquant.us_sector_linkage import US_SECTOR_TRAINING_FEATURE_COLUMNS
from roboquant.utils import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train horizon-specific models.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--horizon", default=None, help="2M, 3M, 6M, 9M, 1Y. Omit to train all.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    horizons = get_horizons(config)
    feature_columns = get_feature_columns(config)
    selected_horizons = [args.horizon] if args.horizon else _configured_horizons(config, horizons)
    model_root = Path(config["paths"]["model_dir"])

    for horizon in selected_horizons:
        dataset = load_modeling_dataset(conn, horizon)
        horizon_features = _feature_columns_for_horizon(config, feature_columns, horizon)
        result = train_horizon_model(
            dataset,
            horizon,
            horizon_features,
            config.get("model", {}),
            horizons[horizon],
        )
        horizon_dir = model_root / horizon
        model_path = horizon_dir / "model.pkl"
        metrics_path = horizon_dir / "metrics.json"
        save_model_bundle(result.bundle, model_path)

        metrics = dict(result.metrics)
        prediction_rows = result.walk_forward_predictions
        if prediction_rows.empty:
            prediction_rows = result.validation_predictions
        if not prediction_rows.empty:
            conn.execute("DELETE FROM predictions WHERE horizon = ?", [horizon])
            append_dedup_table(
                conn,
                "predictions",
                prediction_rows,
                ["asof_date", "symbol", "horizon", "model_version"],
            )
            walk_metrics = evaluate_predictions(dataset, prediction_rows, top_k=20)
            metrics.update({f"stored_{key}": value for key, value in walk_metrics.items()})

        write_json(metrics_path, metrics)
        print(f"{horizon}: saved {model_path}")
        print(f"{horizon}: metrics {metrics}")


def _configured_horizons(config: dict, horizons: dict[str, int]) -> list[str]:
    configured = config.get("pipeline", {}).get("train_horizons")
    if configured:
        return [str(horizon) for horizon in configured if str(horizon) in horizons]
    return list(horizons)


def _feature_columns_for_horizon(config: dict, feature_columns: list[str], horizon: str) -> list[str]:
    if not _is_long_horizon(config, horizon):
        return feature_columns
    blocked = set()
    if not bool(config.get("koru", {}).get("include_6m_features", False)):
        blocked.update(KORU_TRAINING_FEATURE_COLUMNS)
    if not bool(config.get("telegram", {}).get("include_6m_features", False)):
        blocked.update(TELEGRAM_TRAINING_FEATURE_COLUMNS)
    if not bool(config.get("news_signals", {}).get("include_6m_features", False)):
        blocked.update(NEWS_TRAINING_FEATURE_COLUMNS)
    if not bool(config.get("us_sector_linkage", {}).get("include_6m_features", False)):
        blocked.update(US_SECTOR_TRAINING_FEATURE_COLUMNS)
    return [column for column in feature_columns if column not in blocked]


def _is_long_horizon(config: dict, horizon: str) -> bool:
    days = get_horizons(config).get(str(horizon))
    if days is not None:
        return int(days) >= 126
    return str(horizon) in {"6M", "9M", "1Y", "2Y"}


if __name__ == "__main__":
    main()

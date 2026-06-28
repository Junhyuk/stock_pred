#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import (
    build_market_regime_row,
    regime_row_to_frame,
    resolve_cutoff,
    resolve_prediction_date,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build daily global market regime.")
    parser.add_argument("--config", default="configs/global_market.yaml")
    parser.add_argument("--prediction-date", default="latest")
    parser.add_argument("--cutoff", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    cutoff = resolve_cutoff(args.cutoff, config.get("regime", {}).get("cutoff_time_kst", "08:00"))
    prediction_date = resolve_prediction_date(args.prediction_date, cutoff)
    conn = connect_database(get_database_path(config))
    try:
        row = build_market_regime_row(
            conn,
            prediction_date=prediction_date,
            prediction_cutoff=cutoff,
            config=config,
        )
        append_dedup_table(
            conn,
            "market_regime_daily",
            regime_row_to_frame(row),
            ["prediction_date", "prediction_cutoff", "feature_version"],
        )
        print(
            "market_regime_daily upserted: "
            f"{row['prediction_date']} {row['regime']} risk={row['global_risk_score']}"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

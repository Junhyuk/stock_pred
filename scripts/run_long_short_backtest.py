#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, get_horizons, load_config
from roboquant.data.loaders import load_prediction_dataset
from roboquant.db import append_dedup_table, connect_database
from roboquant.long_short import run_long_short_backtest


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run simulated long-short Top50 backtest.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--horizon", default="2M", help="2M or 6M")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    horizons = get_horizons(config)
    if args.horizon not in horizons:
        raise ValueError(f"Unknown horizon {args.horizon}. Config horizons: {sorted(horizons)}")

    if args.dry_run:
        ls_cfg = config.get("long_short", {})
        frequency = ls_cfg.get("rebalance_frequency", {})
        if isinstance(frequency, dict):
            frequency = frequency.get(args.horizon, "M")
        print(
            "dry-run: long-short backtest "
            f"horizon={args.horizon}, horizon_days={horizons[args.horizon]}, "
            f"rebalance={frequency}, long_count={ls_cfg.get('long_count', 10)}, "
            f"short_count={ls_cfg.get('short_count', 10)}"
        )
        return

    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    dataset = load_prediction_dataset(conn, args.horizon)
    if dataset.empty:
        print(f"{args.horizon}: no stored predictions with forward returns")
        return

    curve, summary = run_long_short_backtest(dataset, args.horizon, config=config)
    if curve.empty:
        print(f"{args.horizon}: no backtest rows")
        return

    append_dedup_table(
        conn,
        "long_short_backtest_results",
        curve,
        ["asof_date", "horizon", "market", "model_version"],
    )
    print(f"{args.horizon}: stored {len(curve)} long-short backtest rows")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

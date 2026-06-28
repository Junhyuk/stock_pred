#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.dashboard.backtest_service import run_backtest_job
from roboquant.db import connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build v7 prediction-level backtest results.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--horizon", type=int, default=60)
    parser.add_argument("--model", default=None)
    parser.add_argument("--version", default=None)
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    results, performance = run_backtest_job(
        conn,
        horizon_days=args.horizon,
        model=args.model,
        version=args.version,
        from_date=args.from_date,
        to_date=args.to_date,
    )
    print(f"backtest_results rows: {len(results)}")
    print(f"model_performance_daily rows: {len(performance)}")


if __name__ == "__main__":
    main()

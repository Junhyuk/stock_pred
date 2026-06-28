#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.dashboard.gatekeeper_service import run_model_gatekeeper
from roboquant.db import connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run v7 model performance gatekeeper.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--baseline", default="lightgbm")
    parser.add_argument("--min-sample-count", type=int, default=200)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    conn = connect_database(get_database_path(config))
    decisions = run_model_gatekeeper(
        conn,
        baseline_model=args.baseline,
        min_sample_count=args.min_sample_count,
    )
    print(decisions.to_string(index=False) if not decisions.empty else "no model performance rows")


if __name__ == "__main__":
    main()

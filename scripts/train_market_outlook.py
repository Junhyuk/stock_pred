#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_outlook import default_market_outlook_model_path, train_market_outlook_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train KOSPI/KOSDAQ TODAY/WEEK market outlook model.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    model_path = Path(args.model_path) if args.model_path else default_market_outlook_model_path(config)
    conn = connect_database(get_database_path(config), read_only=True, initialize_schema=False)
    try:
        model = train_market_outlook_model(conn, config=config, model_path=model_path, asof_date=args.date)
    finally:
        conn.close()

    specs = model.get("models", {})
    print(f"market_outlook_model saved: {model_path}")
    for key, spec in sorted(specs.items()):
        print(
            f"{key}: samples={spec.get('sample_count', 0)} "
            f"sigma={spec.get('residual_std')} direction_accuracy={spec.get('direction_accuracy')}"
        )


if __name__ == "__main__":
    main()

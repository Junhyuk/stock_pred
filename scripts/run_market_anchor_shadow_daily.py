#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backfill_market_anchor_history import run_backfill
from build_market_anchor_features import run_build
from predict_market_anchor_shadow import run_predict
from run_market_anchor_shadow_backtest import run_backtest
from train_market_anchor_shadow import run_train


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the bounded daily anchor shadow collection and inference steps.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--retrain", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dry_run:
        print(
            json.dumps(
                {
                    "status": "dry_run",
                    "steps": [
                        "backfill latest anchor snapshot",
                        "build anchor features",
                        "optional train/backtest",
                        "latest shadow inference",
                    ],
                    "production_weight": 0.0,
                },
                ensure_ascii=False,
            )
        )
        return
    collection = run_backfill(args.config, from_date=args.date, to_date=args.date)
    if collection.get("failed_dates"):
        print(json.dumps({"status": "partial_ready", "collection": collection}, ensure_ascii=False, default=str))
        return
    features = run_build(args.config, asof_date=args.date)
    result: dict[str, object] = {"status": features["status"], "collection": collection, "features": features}
    if args.retrain:
        training = run_train(args.config)
        result["training"] = training
        result["backtest"] = run_backtest(args.config)
    result["prediction"] = run_predict(args.config)
    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()

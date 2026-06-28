#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.universe.price_collection import (
    PredictionUniversePriceCollectionError,
    collect_prediction_universe_prices,
    summary_as_dict,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect prices for the current v8 Top50 universe.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--snapshot-date", default="latest", help="'latest' or YYYY-MM-DD")
    parser.add_argument("--to-date", default=None, help="'latest', YYYY-MM-DD, or omitted for today")
    parser.add_argument("--allow-missing", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)

    universe_cfg = config.get("universe", {})
    market_cfg = config.get("market", {})
    collection_cfg = config.get("collection", {})
    universe_rule = str(universe_cfg.get("rule", "prediction_top_market_cap"))

    conn = connect_database(get_database_path(config))
    try:
        summary = collect_prediction_universe_prices(
            conn,
            universe_rule=universe_rule,
            snapshot_date=args.snapshot_date,
            start_date=str(market_cfg.get("start_date", "2019-01-01")),
            end_date=args.to_date,
            sleep_seconds=float(collection_cfg.get("sleep_seconds", 0.05)),
            strict_missing=not args.allow_missing,
        )
    except PredictionUniversePriceCollectionError as exc:
        print(f"Top50 price collection failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        conn.close()

    print(f"Top50 price collection complete: {summary_as_dict(summary)}")


if __name__ == "__main__":
    main()

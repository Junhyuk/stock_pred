#!/usr/bin/env python3
"""Generate independent SPX/Nasdaq next-US-session research receipts."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.index_forecast.us_market_inputs import _utc
from roboquant.index_forecast.us_market_pipeline import run_us_reliable_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--us-observations", default="data/processed/us_market_observations_v2.csv")
    parser.add_argument("--output-dir", default="docs/data/us")
    parser.add_argument("--config", default="configs/reliable_index_forecast.yaml")
    parser.add_argument("--now", help="Explicit replay timestamp, not a live observation")
    parser.add_argument("--news-context")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text()) or {}
    now = _utc(args.now).to_pydatetime() if args.now else None
    result = run_us_reliable_pipeline(args.us_observations, args.output_dir, config=config, now=now, news_context=args.news_context)
    print(json.dumps({"status": result["status"], "run_id": result.get("run_id"), "issued_at": result.get("issued_at"),
                      "markets": [{"market": row["market"], "forecast_id": row["forecast_id"], "target_date": row["context"]["target_date"],
                                   "target_close_at": row["context"]["target_close_at"], "status": row["forecast"]["status"]}
                                  for row in result["markets"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()

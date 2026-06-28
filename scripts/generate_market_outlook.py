#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_outlook import default_market_outlook_model_path, refresh_market_outlook_forecasts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate KOSPI/KOSDAQ TODAY/WEEK market outlook forecasts.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    model_path = Path(args.model_path) if args.model_path else default_market_outlook_model_path(config)
    conn = connect_database(get_database_path(config))
    try:
        frame = refresh_market_outlook_forecasts(
            conn,
            config=config,
            asof_date=args.date,
            model_path=model_path,
        )
    finally:
        conn.close()

    if frame.empty:
        print("market_outlook_forecasts rows: 0")
        return
    asof = frame["asof_date"].iloc[0]
    print(f"market_outlook_forecasts rows: {len(frame)} asof: {asof}")
    for _, row in frame.sort_values(["horizon", "market"]).iterrows():
        print(
            f"{row['horizon']} {row['market']}: expected={row['expected_return']:.4f} "
            f"up={row['up_probability']:.3f} shock={row['shock_probability']:.3f}"
        )


if __name__ == "__main__":
    main()

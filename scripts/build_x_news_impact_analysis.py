#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.signals.x_news_impact import (
    refresh_x_market_outlook_impact,
    refresh_x_news_prediction_impact,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build X MarketNews prediction impact analysis.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--horizons", default="2M,3M")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    horizons = [item.strip() for item in args.horizons.split(",") if item.strip()]
    conn = connect_database(get_database_path(config))
    try:
        stock = refresh_x_news_prediction_impact(conn, config, asof_date=args.date, horizons=horizons)
        market = refresh_x_market_outlook_impact(conn, config, asof_date=args.date)
    finally:
        conn.close()
    print(f"x_news_prediction_impact_daily rows: {len(stock)}")
    print(f"x_market_outlook_impact_daily rows: {len(market)}")
    if stock.empty and market.empty:
        print("X news impact not generated: no x_marketnews_feed signal available for the latest feature date")


if __name__ == "__main__":
    main()

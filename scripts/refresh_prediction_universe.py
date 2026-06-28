#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database
from roboquant.universe.providers.factory import get_market_data_provider
from roboquant.universe.refresh import (
    RefreshSettings,
    UniverseRefreshError,
    refresh_prediction_universe,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh the v8 Top50 prediction universe.")
    parser.add_argument("--config", default="configs/universe_top50.yaml")
    parser.add_argument("--date", default="latest", help="'latest' or YYYY-MM-DD")
    parser.add_argument("--provider", default=None, help="Override MARKET_DATA_PROVIDER/config")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    universe = config["universe"]
    market_data = config.get("market_data", {})
    snapshot_date = _parse_snapshot_date(args.date)
    provider_name = args.provider or market_data.get("local_provider")
    provider = get_market_data_provider(str(provider_name) if provider_name else None)
    settings = RefreshSettings(
        fetch_limit_per_market=int(market_data.get("fetch_limit_per_market", 100)),
        validation_price_days=int(market_data.get("validation_price_days", 90)),
        min_listing_trading_days=int(market_data.get("min_listing_trading_days", 120)),
        max_missing_ratio_60d=float(market_data.get("max_missing_ratio_60d", 0.10)),
        max_latest_price_gap_days=int(market_data.get("max_latest_price_gap_days", 14)),
        kospi_target=int(universe.get("kospi_target", 30)),
        kosdaq_target=int(universe.get("kosdaq_target", 20)),
    )

    conn = connect_database(get_database_path(config))
    try:
        result = refresh_prediction_universe(
            conn,
            provider,
            snapshot_date=snapshot_date,
            universe_rule=str(universe["rule"]),
            settings=settings,
        )
    except UniverseRefreshError as exc:
        print(f"Universe refresh failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        conn.close()

    print(
        "Refreshed prediction universe: "
        f"date={result['snapshot_date']}, provider={result['provider']}, "
        f"raw={result['raw_count']}, prediction={result['prediction_count']}, "
        f"KOSPI={result['kospi_count']}, KOSDAQ={result['kosdaq_count']}"
    )


def _parse_snapshot_date(value: str) -> date:
    if value == "latest":
        return date.today()
    return date.fromisoformat(value)


if __name__ == "__main__":
    main()

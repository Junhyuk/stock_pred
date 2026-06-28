#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.yahoo_unofficial import YahooUnofficialProvider, symbols_from_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST

DISCLAIMER = (
    "Unofficial Yahoo/yfinance provider is for local PoC/research use only. "
    "Do not use it as an approved production or redistribution data source."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect local-only unofficial Yahoo/yfinance data.")
    parser.add_argument("--config", default="configs/yahoo_unofficial.yaml")
    parser.add_argument("--from-date", default="2024-01-01")
    parser.add_argument("--to-date", default="latest")
    parser.add_argument("--symbols", default=None, help="Comma-separated Yahoo symbols overriding config.")
    parser.add_argument("--prices-only", action="store_true")
    parser.add_argument("--fundamentals-only", action="store_true")
    parser.add_argument("--sleep-seconds", type=float, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.prices_only and args.fundamentals_only:
        raise ValueError("Choose at most one of --prices-only or --fundamentals-only")
    config = load_config(args.config)
    ensure_project_dirs(config)
    settings = config.get("yahoo_unofficial", {})
    specs = _resolve_symbols(config, args.symbols)
    max_symbols = int(settings.get("max_symbols", 100))
    if len(specs) > max_symbols:
        raise ValueError(f"Too many Yahoo symbols: {len(specs)} > max_symbols={max_symbols}")
    sleep_seconds = float(args.sleep_seconds if args.sleep_seconds is not None else settings.get("sleep_seconds", 1.0))
    start_date = _parse_date(args.from_date)
    end_date = _parse_date(args.to_date)
    prices_enabled = bool(settings.get("prices_enabled", True)) and not args.fundamentals_only
    fundamentals_enabled = bool(settings.get("fundamentals_enabled", True)) and not args.prices_only
    print(DISCLAIMER)
    provider = YahooUnofficialProvider()
    conn = connect_database(get_database_path(config))
    try:
        price_rows = 0
        fundamentals_rows = 0
        for spec in specs:
            try:
                if prices_enabled:
                    prices = provider.get_price_history([spec], start_date, end_date)
                    append_dedup_table(conn, "yahoo_prices_daily", prices, ["date", "yahoo_symbol"])
                    price_rows += len(prices)
                if fundamentals_enabled and spec.asset_type == "stock":
                    fundamentals = provider.get_fundamentals([spec], end_date)
                    append_dedup_table(
                        conn,
                        "yahoo_fundamentals_snapshot",
                        fundamentals,
                        ["asof_date", "yahoo_symbol"],
                    )
                    fundamentals_rows += len(fundamentals)
            except Exception as exc:
                append_dedup_table(
                    conn,
                    "collection_failures",
                    collection_failure_row(
                        step="collect_yahoo_unofficial",
                        source="yahoo_unofficial",
                        symbol=spec.yahoo_symbol,
                        target_date=end_date.isoformat(),
                        error=exc,
                    ),
                    ["collected_at", "step", "source", "error_message"],
                )
                print(f"yahoo_unofficial failed for {spec.yahoo_symbol}: {exc}")
            time.sleep(max(0.0, sleep_seconds))
        print(f"yahoo_prices_daily rows upserted: {price_rows}")
        print(f"yahoo_fundamentals_snapshot rows upserted: {fundamentals_rows}")
    finally:
        conn.close()


def _resolve_symbols(config: dict, override: str | None):
    if override:
        return symbols_from_config([item.strip() for item in override.split(",") if item.strip()])
    return symbols_from_config(config.get("symbols", []))


def _parse_date(value: str) -> date:
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return date.fromisoformat(str(value))


if __name__ == "__main__":
    main()

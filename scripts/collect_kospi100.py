#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.krx import (
    fetch_benchmark,
    fetch_kospi_top_symbols,
    fetch_prices,
)
from roboquant.data.validators.quality import validate_prices
from roboquant.db import append_dedup_table, connect_database, replace_table


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect the KOSPI top-100 Samsung demo universe.")
    parser.add_argument("--config", default="configs/samsung_demo.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    market = config["market"]
    universe = config.get("universe", {})
    collection = config.get("collection", {})

    symbols = fetch_kospi_top_symbols(
        limit=int(universe.get("limit", 100)),
        focus_symbol=str(universe.get("focus_symbol", "005930")),
        extra_symbols=universe.get("extra_symbols", []),
    )
    replace_table(conn, "symbols", symbols)
    selected = symbols["symbol"].tolist()
    success = 0
    for index, symbol in enumerate(selected, start=1):
        try:
            prices = fetch_prices(symbol, market["start_date"], market.get("end_date"))
            validate_prices(prices).raise_for_errors()
            append_dedup_table(conn, "prices_daily", prices, ["date", "symbol"])
            success += 1
            print(f"[{index}/{len(selected)}] {symbol}: {len(prices)} rows")
        except Exception as exc:
            print(f"[{index}/{len(selected)}] {symbol}: failed: {exc}")
            append_dedup_table(
                conn,
                "collection_failures",
                collection_failure_row(
                    step="collect_kospi100",
                    source="pykrx_or_fdr",
                    error=exc,
                    symbol=symbol,
                ),
                ["collected_at", "step", "source", "symbol", "error_message"],
            )
        sleep(float(collection.get("sleep_seconds", 0.05)))

    benchmark = fetch_benchmark(
        market["benchmark_code"],
        market["benchmark_name"],
        market["start_date"],
        market.get("end_date"),
    )
    append_dedup_table(conn, "benchmark_daily", benchmark, ["date", "benchmark"])
    print(f"symbols: {len(symbols)}, successful prices: {success}, benchmark rows: {len(benchmark)}")


if __name__ == "__main__":
    main()

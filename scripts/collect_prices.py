#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.krx import fetch_benchmark, fetch_prices, fetch_symbols
from roboquant.data.validators.quality import validate_prices
from roboquant.db import append_dedup_table, connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect KRX symbol and OHLCV data.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--limit", type=int, default=None, help="Limit symbols for a quick smoke run.")
    parser.add_argument("--symbols", nargs="*", default=None, help="Specific symbols to collect.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))

    market_cfg = config["market"]
    collection_cfg = config.get("collection", {})
    symbols = fetch_symbols(market_cfg["markets"])
    append_dedup_table(conn, "symbols", symbols, ["symbol"])

    selected = symbols["symbol"].astype(str).str.zfill(6).tolist()
    if args.symbols:
        selected = [symbol.zfill(6) for symbol in args.symbols]
    max_symbols = args.limit or collection_cfg.get("max_symbols")
    if max_symbols:
        selected = selected[: int(max_symbols)]

    print(f"Collecting {len(selected)} symbols from {market_cfg['start_date']}...")
    success = 0
    for idx, symbol in enumerate(selected, start=1):
        try:
            prices = fetch_prices(symbol, market_cfg["start_date"], market_cfg.get("end_date"))
            if prices.empty:
                print(f"[{idx}/{len(selected)}] {symbol}: empty")
                continue
            report = validate_prices(prices)
            report.raise_for_errors()
            append_dedup_table(conn, "prices_daily", prices, ["date", "symbol"])
            success += 1
            print(f"[{idx}/{len(selected)}] {symbol}: {len(prices)} rows")
        except Exception as exc:
            print(f"[{idx}/{len(selected)}] {symbol}: failed: {exc}")
            failure = collection_failure_row(
                step="collect_prices",
                source="pykrx_or_fdr",
                error=exc,
                symbol=symbol,
                target_date=market_cfg.get("end_date"),
            )
            append_dedup_table(
                conn,
                "collection_failures",
                failure,
                ["collected_at", "step", "source", "symbol", "error_message"],
            )

    benchmark = fetch_benchmark(
        market_cfg["benchmark_code"],
        market_cfg["benchmark_name"],
        market_cfg["start_date"],
        market_cfg.get("end_date"),
    )
    if not benchmark.empty:
        append_dedup_table(conn, "benchmark_daily", benchmark, ["date", "benchmark"])
        print(f"Benchmark rows: {len(benchmark)}")
    else:
        print("Benchmark fetch returned empty; build_dataset will use equal-weight universe fallback.")
    print(f"Done. Successful symbols: {success}/{len(selected)}")


if __name__ == "__main__":
    main()

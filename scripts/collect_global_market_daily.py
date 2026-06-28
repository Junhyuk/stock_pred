#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.collector import daily_bars_to_frame
from roboquant.global_market.providers.errors import GlobalProviderConfigurationError
from roboquant.global_market.providers.fred import FredProvider
from roboquant.global_market.providers.registry import FRED_DAILY_SERIES
from roboquant.global_market.providers.yfinance_poc import YFinancePocProvider
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect global daily market indicators.")
    parser.add_argument("--config", default="configs/global_market.yaml")
    parser.add_argument("--from-date", default="2022-01-01")
    parser.add_argument("--to-date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    start_date = _parse_date(args.from_date)
    end_date = _parse_date(args.to_date)
    conn = connect_database(get_database_path(config))
    try:
        bars = []
        yfinance_symbols = config.get("symbols", {}).get("yfinance_daily", [])
        try:
            bars.extend(YFinancePocProvider().get_daily_bars(yfinance_symbols, start_date, end_date))
        except Exception as exc:
            _record_failure(conn, "collect_global_market_daily", "yfinance_poc", exc, end_date)
            raise

        fred_symbols = config.get("symbols", {}).get("fred_daily", list(FRED_DAILY_SERIES))
        try:
            bars.extend(FredProvider().get_daily_bars(fred_symbols, start_date, end_date))
        except GlobalProviderConfigurationError:
            print("FRED_API_KEY is not set; skipping FRED daily series without fake values.")
        except Exception as exc:
            _record_failure(conn, "collect_global_market_daily", "fred", exc, end_date)
            print(f"FRED collection failed and was skipped: {exc}")

        frame = daily_bars_to_frame(bars)
        if frame.empty:
            raise RuntimeError("No global daily market rows collected")
        append_dedup_table(conn, "global_market_daily", frame, ["trade_date", "symbol", "source_name"])
        print(f"global_market_daily rows upserted: {len(frame)}")
    finally:
        conn.close()


def _record_failure(conn, step: str, source: str, error: Exception, target_date: date) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(step=step, source=source, error=error, target_date=target_date.isoformat()),
        ["collected_at", "step", "source", "error_message"],
    )


def _parse_date(value: str) -> date:
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return date.fromisoformat(str(value))


if __name__ == "__main__":
    main()

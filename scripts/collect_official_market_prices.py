#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.official_sources import (
    OfficialDataConfigurationError,
    collect_official_market_prices,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect configured official KRX/Data.go.kr market prices.")
    parser.add_argument("--config", default="configs/official_data_sources.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--source", choices=["all", "krx", "data_go_kr_prices"], default="all")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    target_date = _parse_date(args.date)
    conn = connect_database(get_database_path(config))
    sources = ["krx", "data_go_kr_prices"] if args.source == "all" else [args.source]
    total = 0
    try:
        for source in sources:
            try:
                frame = collect_official_market_prices(config=config.get("official_news", {}), target_date=target_date, source=source)
                append_dedup_table(conn, "official_market_prices", frame, ["date", "symbol", "source"])
                total += len(frame)
                print(f"official_market_prices {source} rows: {len(frame)}")
            except OfficialDataConfigurationError as exc:
                _record_failure(conn, source, exc, target_date)
                if not args.allow_missing_key:
                    raise
                print(f"{source} skipped without fake data: {exc}")
        print(f"official_market_prices total rows: {total}")
    except Exception as exc:
        if not isinstance(exc, OfficialDataConfigurationError):
            _record_failure(conn, args.source, exc, target_date)
        raise
    finally:
        conn.close()


def _record_failure(conn, source: str, error: Exception, target_date) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row("collect_official_market_prices", source, error, target_date=target_date.isoformat()),
        ["collected_at", "step", "source", "error_message"],
    )


def _parse_date(value: str):
    if str(value).strip().lower() == "latest":
        return datetime.now(KST).date()
    return datetime.fromisoformat(str(value)).date()


if __name__ == "__main__":
    main()

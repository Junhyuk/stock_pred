#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.market_credit_balance import (
    MissingCreditBalanceConfig,
    fetch_market_credit_balance,
)
from roboquant.data.freshness import expected_latest_trading_day
from roboquant.db import append_dedup_table, connect_database
from roboquant.utils import today_string


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect official KOFIA/data.go.kr market credit balance data.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default=None, help="'latest', YYYY-MM-DD, or omitted for today")
    parser.add_argument("--allow-missing-key", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    target_date = _target_date(args.date)
    try:
        frame = fetch_market_credit_balance(target_date, config)
        append_dedup_table(conn, "market_credit_balance_daily", frame, ["date", "market", "source"])
        print(f"market_credit_balance_daily rows: {len(frame)}")
    except MissingCreditBalanceConfig as exc:
        _record_failure(conn, exc, target_date)
        if args.allow_missing_key:
            print(f"{exc}; skipped market credit balance without fake data.")
            return
        raise
    except Exception as exc:
        _record_failure(conn, exc, target_date)
        raise
    finally:
        conn.close()


def _target_date(value: str | None) -> str:
    if not value:
        return today_string()
    if value == "latest":
        return expected_latest_trading_day().isoformat()
    return value


def _record_failure(conn, error: Exception, target_date: str) -> None:
    append_dedup_table(
        conn,
        "collection_failures",
        collection_failure_row(
            step="collect_market_credit_balance",
            source="data_go_kr_kofia",
            error=error,
            target_date=target_date,
        ),
        ["collected_at", "step", "source", "error_message"],
    )


if __name__ == "__main__":
    main()

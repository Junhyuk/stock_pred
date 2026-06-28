#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.market_metrics import (
    fetch_market_metrics_by_date,
    fetch_market_metrics_from_universe,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.utils import today_string


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect KRX market cap and valuation metrics.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--date", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    market_cfg = config.get("market", {})
    target_date = args.date or market_cfg.get("end_date") or today_string()

    errors: list[str] = []
    try:
        metrics = fetch_market_metrics_by_date(target_date, market_cfg.get("markets"), errors=errors)
    except Exception as exc:
        errors.append(str(exc))
        metrics = fetch_market_metrics_from_universe(conn, target_date, market_cfg.get("markets"))
    if metrics.empty:
        metrics = fetch_market_metrics_from_universe(conn, target_date, market_cfg.get("markets"))
    if metrics.empty and errors:
        exc = RuntimeError("; ".join(errors))
        failure = collection_failure_row(
            step="collect_market_metrics",
            source="pykrx",
            error=exc,
            target_date=target_date,
        )
        append_dedup_table(
            conn,
            "collection_failures",
            failure,
            ["collected_at", "step", "source", "error_message"],
        )
        raise
    append_dedup_table(conn, "market_metrics_daily", metrics, ["date", "symbol"])
    if errors:
        failure = collection_failure_row(
            step="collect_market_metrics",
            source="pykrx",
            error=RuntimeError("; ".join(errors)),
            target_date=target_date,
        )
        append_dedup_table(
            conn,
            "collection_failures",
            failure,
            ["collected_at", "step", "source", "error_message"],
        )
    print(f"market_metrics_daily rows: {len(metrics)}")


if __name__ == "__main__":
    main()

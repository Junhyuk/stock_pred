#!/usr/bin/env python3
"""Run the sequential-7 KOSPI foreign cash/futures history review.

This is a research/shadow review. It never raises production_weight above 0.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.market_investor_flows import (
    fetch_market_trading_value_by_date_panels,
    panel_has_complete_investors,
)
from roboquant.db import append_dedup_table, connect_database
from roboquant.market_foreign_flow_history import (
    attach_actual_returns_to_forecasts,
    load_benchmark_returns,
    load_cash_flows,
    load_review_inventory,
    load_shadow_forecasts,
    run_sequential7_foreign_flow_review,
)

KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Review KOSPI foreign cash/futures history vs next-session forecasts.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--collect", action="store_true", help="Attempt pykrx cash-flow backfill before the review.")
    parser.add_argument("--persist", action="store_true", help="Persist a complete 12-category cash panel only.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    db_path = get_database_path(config)
    writable = bool(args.collect or args.persist)
    conn = connect_database(db_path, read_only=not writable)
    try:
        inventory = load_review_inventory(conn)
        returns = load_benchmark_returns(conn)
        forecasts = attach_actual_returns_to_forecasts(load_shadow_forecasts(conn), returns)
        cash_flows = load_cash_flows(conn)
        collect_status = None
        if args.collect:
            cash_flows, collect_status = _collect_cash_flows(
                conn,
                config,
                inventory,
                start=args.start,
                end=args.end,
                persist=args.persist,
            )
            if writable:
                inventory = load_review_inventory(conn)
        report = run_sequential7_foreign_flow_review(
            cash_flows=cash_flows,
            futures_flows=None,
            returns=returns,
            forecasts=forecasts,
            inventory=inventory,
        )
        report["collect"] = collect_status
        print(json.dumps(report, ensure_ascii=False, default=str))
    finally:
        conn.close()


def _collect_cash_flows(conn, config, inventory, *, start, end, persist):
    start_date = _to_date(start) or _to_date(inventory.get("benchmark_min_date"))
    end_date = _to_date(end) or _to_date(inventory.get("benchmark_max_date"))
    backfill_days = int((config.get("market_investor_flows") or {}).get("historical_backfill_days") or 600)
    if start is None and end_date is not None:
        trading_days = conn.execute(
            """
            SELECT date FROM benchmark_daily
            WHERE benchmark = 'KOSPI' AND date <= ?
            ORDER BY date DESC
            LIMIT ?
            """,
            [end_date, backfill_days],
        ).fetchall()
        if trading_days:
            start_date = min(_to_date(row[0]) for row in trading_days)
    try:
        flows = fetch_market_trading_value_by_date_panels(
            start_date,
            end_date,
            market="KOSPI",
            now=datetime.now(KST),
        )
    except Exception as exc:
        append_dedup_table(
            conn,
            "collection_failures",
            collection_failure_row(
                step="collect_kospi_foreign_flow_history",
                source="pykrx",
                error=exc,
                target_date=None if end_date is None else end_date.isoformat(),
            ),
            ["collected_at", "step", "source", "error_message"],
        )
        return pd.DataFrame(), {
            "status": "blocked_collection_failed",
            "error": str(exc),
            "start": None if start_date is None else start_date.isoformat(),
            "end": None if end_date is None else end_date.isoformat(),
        }
    persisted = False
    if persist and panel_has_complete_investors(flows):
        append_dedup_table(
            conn,
            "market_investor_flows_daily",
            flows,
            ["trade_date", "market", "investor", "source", "is_final"],
        )
        persisted = True
    return flows, {
        "status": "ready",
        "rows": int(len(flows)),
        "complete_12_category": bool(panel_has_complete_investors(flows)),
        "persisted": persisted,
        "start": None if start_date is None else start_date.isoformat(),
        "end": None if end_date is None else end_date.isoformat(),
    }


def _to_date(value):
    if value in (None, "", "latest"):
        return None
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


if __name__ == "__main__":
    main()

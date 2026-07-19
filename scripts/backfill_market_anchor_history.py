#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path
from time import sleep

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.collectors.market_anchor import fetch_market_anchor_constituents_by_date
from roboquant.db import append_dedup_table, connect_database
from roboquant.market_anchor_shadow import load_market_anchor_config, market_anchor_settings


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill point-in-time KOSPI/KOSDAQ anchor market-cap snapshots.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--from-date", default=None)
    parser.add_argument("--to-date", default="latest")
    parser.add_argument("--force", action="store_true", help="Replace already stored snapshot dates.")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_backfill(
        args.config,
        from_date=args.from_date,
        to_date=args.to_date,
        force=args.force,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, ensure_ascii=False, default=str))


def run_backfill(
    config_path: str | Path,
    *,
    from_date: str | None = None,
    to_date: str = "latest",
    force: bool = False,
    dry_run: bool = False,
) -> dict:
    _load_dotenv(ROOT / ".env")
    config = load_market_anchor_config(config_path)
    ensure_project_dirs(config)
    settings = market_anchor_settings(config)
    conn = connect_database(get_database_path(config))
    try:
        dates = _benchmark_dates(
            conn,
            start=from_date or str(settings["start_date"]),
            end=to_date,
        )
        pending = dates if force else [item for item in dates if not _snapshot_complete(conn, item)]
        if dry_run:
            return {
                "status": "dry_run",
                "available_dates": len(dates),
                "pending_dates": len(pending),
                "from_date": None if not dates else dates[0],
                "to_date": None if not dates else dates[-1],
                "source": settings["source"],
            }
        names = _symbol_names(conn)
        collected = 0
        failures = 0
        for index, trade_date in enumerate(pending):
            try:
                frame = fetch_market_anchor_constituents_by_date(
                    trade_date,
                    settings,
                    name_lookup=names,
                )
                append_dedup_table(
                    conn,
                    "market_anchor_constituents_daily",
                    frame,
                    ["asof_date", "market", "symbol", "selection_rule"],
                )
                collected += int(len(frame))
            except Exception as exc:
                failures += 1
                append_dedup_table(
                    conn,
                    "collection_failures",
                    collection_failure_row(
                        step="backfill_market_anchor_history",
                        source=str(settings["source"]),
                        error=exc,
                        target_date=trade_date.isoformat(),
                    ),
                    ["collected_at", "step", "source", "error_message"],
                )
                if _is_authentication_failure(exc):
                    break
            if index + 1 < len(pending) and float(settings["sleep_seconds"]) > 0.0:
                sleep(float(settings["sleep_seconds"]))
        return {
            "status": "ready" if failures == 0 else "partial_ready",
            "available_dates": len(dates),
            "pending_dates": len(pending),
            "collected_rows": collected,
            "failed_dates": failures,
            "source": settings["source"],
        }
    finally:
        conn.close()


def _benchmark_dates(conn, *, start: str, end: str) -> list[date]:
    max_date = conn.execute("SELECT MAX(date) FROM benchmark_daily").fetchone()[0]
    end_date = max_date if end == "latest" else pd.to_datetime(end).date()
    if end_date is None:
        return []
    frame = conn.execute(
        """
        SELECT date,
               CASE WHEN benchmark = '1001' THEN 'KOSPI'
                    WHEN benchmark = '2001' THEN 'KOSDAQ'
                    ELSE benchmark END AS market
        FROM benchmark_daily
        WHERE date BETWEEN ? AND ?
          AND benchmark IN ('KOSPI', 'KOSDAQ', '1001', '2001')
        GROUP BY date, market
        """,
        [pd.to_datetime(start).date(), end_date],
    ).fetchdf()
    if frame.empty:
        return []
    counts = frame.groupby("date")["market"].nunique()
    return sorted(pd.to_datetime(counts[counts >= 2].index).date.tolist())


def _snapshot_complete(conn, trade_date: date) -> bool:
    rows = conn.execute(
        """
        SELECT market, COUNT(*)
        FROM market_anchor_constituents_daily
        WHERE asof_date = ?
          AND selection_rule IN ('fixed_user_anchor_v1', 'point_in_time_market_cap_top3_v1')
        GROUP BY market
        """,
        [trade_date],
    ).fetchall()
    counts = {str(market): int(count) for market, count in rows}
    return counts.get("KOSPI") == 3 and counts.get("KOSDAQ") == 3


def _symbol_names(conn) -> dict[str, str]:
    frame = conn.execute("SELECT symbol, name FROM symbols WHERE name IS NOT NULL").fetchdf()
    return {
        str(item.symbol).zfill(6): str(item.name)
        for item in frame.itertuples(index=False)
        if item.name is not None
    }


def _is_authentication_failure(error: Exception) -> bool:
    text = str(error).lower()
    return any(token in text for token in ("krx_id", "krx_pw", "로그인", "authentication", "unauthorized"))


def _load_dotenv(path: Path) -> None:
    """Load local credentials without logging or overwriting process values."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            os.environ.setdefault(key, value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()

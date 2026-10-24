#!/usr/bin/env python3
"""3-year KOSPI/KOSDAQ/KOSPI100/KOSDAQ100 walk-forward and GitHub Pages report.

Does not modify production DuckDB forecasts or market-outlook-v2 weights.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.index_forecast.indexes import FORECAST_HORIZONS, RESEARCH_INDEXES
from roboquant.index_forecast.pipeline import run_index_forecast_research
from roboquant.index_forecast.sqlite_store import DEFAULT_SQLITE_PATH, connect_index_forecast


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="3-year index accuracy backtest and 1D/5D research forecast.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE_PATH))
    parser.add_argument("--index", action="append", dest="indexes", help="Index code. Repeatable.")
    parser.add_argument("--horizons", default="1,5", help="Comma-separated horizon days, e.g. 1,5")
    parser.add_argument("--eval-years", type=int, default=3)
    parser.add_argument("--ingest-official", action="store_true", help="Fetch official pykrx index OHLCV into SQLite only.")
    parser.add_argument("--ingest-kospi100", action="store_true", help="Deprecated alias for --ingest-official.")
    parser.add_argument("--skip-export", action="store_true")
    parser.add_argument("--initial-train-days", type=int, default=252)
    parser.add_argument("--retrain-step-days", type=int, default=21)
    parser.add_argument("--min-oos-rows", type=int, default=60)
    parser.add_argument("--write-pages", default="docs", help="GitHub Pages directory. Empty string to skip.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    sqlite_path = Path(args.sqlite)
    if not sqlite_path.is_absolute():
        sqlite_path = Path(config["_root"]) / sqlite_path
    sqlite_conn = connect_index_forecast(sqlite_path)
    duckdb_conn = connect_database(get_database_path(config), read_only=True)
    indexes = tuple(args.indexes) if args.indexes else RESEARCH_INDEXES
    horizons = tuple(int(item.strip()) for item in str(args.horizons).split(",") if item.strip()) or FORECAST_HORIZONS
    pages_dir = None if str(args.write_pages).strip() in {"", "none"} else Path(args.write_pages)
    if pages_dir is not None and not pages_dir.is_absolute():
        pages_dir = Path(config["_root"]) / pages_dir
    try:
        result = run_index_forecast_research(
            sqlite_conn,
            duckdb_conn=duckdb_conn,
            indexes=indexes,
            horizon_days=horizons,
            eval_years=args.eval_years,
            ingest_official=bool(args.ingest_official or args.ingest_kospi100),
            skip_export=args.skip_export,
            persist=True,
            initial_train_days=args.initial_train_days,
            retrain_step_days=args.retrain_step_days,
            min_oos_rows=args.min_oos_rows,
            pages_dir=pages_dir,
        )
        summary = {
            "sqlite": str(sqlite_path),
            "pages": result.get("pages"),
            "status": result.get("status"),
            "eval_years": result.get("eval_years"),
            "production_weight": result.get("production_weight"),
            "ingest": result.get("ingest"),
            "export": result.get("export"),
            "leaderboard": (result.get("report") or {}).get("leaderboard"),
            "forecasts": _compact_forecasts((result.get("report") or {}).get("indexes") or []),
        }
        print(json.dumps(summary, ensure_ascii=False, default=str))
    finally:
        sqlite_conn.close()
        duckdb_conn.close()


def _compact_forecasts(cards: list) -> list[dict]:
    rows: list[dict] = []
    for card in cards:
        for horizon, block in (card.get("horizons") or {}).items():
            forecast = block.get("forecast") or {}
            champion = block.get("champion") or {}
            rows.append(
                {
                    "index_code": card.get("index_code"),
                    "status": card.get("status"),
                    "horizon": horizon,
                    "champion": champion.get("model_type"),
                    "group": champion.get("group"),
                    "direction_hit": (champion.get("metrics") or {}).get("direction_hit"),
                    "mae": (champion.get("metrics") or {}).get("mae"),
                    "expected_return": forecast.get("expected_return"),
                    "range_low": forecast.get("range_low"),
                    "range_high": forecast.get("range_high"),
                    "signal": forecast.get("signal"),
                    "asof_date": forecast.get("asof_date") or card.get("asof_date"),
                }
            )
    return rows


if __name__ == "__main__":
    main()

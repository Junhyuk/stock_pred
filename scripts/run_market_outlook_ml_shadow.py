#!/usr/bin/env python3
"""Run the isolated LGBM + local-news next-session market shadow."""

from __future__ import annotations

import argparse
import json
import sys
import warnings
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.market_outlook import (
    market_outlook_input_status,
    resolve_market_outlook_asof,
    resolve_market_outlook_target,
)
from roboquant.market_outlook_ml_shadow import run_market_outlook_ml_shadow
from roboquant.signals.local_llm_news import load_local_llm_news_config

KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run shadow-only LightGBM quantile market outlook.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--target-date", default="latest")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_local_llm_news_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    try:
        asof = resolve_market_outlook_asof(conn, args.date)
        target = resolve_market_outlook_target(
            asof,
            config,
            target_date=None if str(args.target_date).strip().lower() == "latest" else args.target_date,
        )
        context = market_outlook_input_status(
            conn,
            asof_date=asof,
            target_date=target["target_date"],
            config=config,
            calendar_source=target["calendar_source"],
            calendar_messages=target["calendar_messages"],
        )
        if context["forecast_status"] != "confirmed":
            print(json.dumps({"status": "skipped_stale_input", "input_status": context}, ensure_ascii=False, default=str))
            return
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning, message="Mean of empty slice")
            result = run_market_outlook_ml_shadow(
                conn,
                config,
                asof_date=args.date,
                now=datetime.combine(target["target_date"], time(8, 0), tzinfo=KST),
            )
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                default=str,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

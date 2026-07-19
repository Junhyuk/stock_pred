#!/usr/bin/env python3
"""Refresh the production Ridge outlook and its isolated LGBM-news shadow."""

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
    default_market_outlook_model_path,
    market_outlook_input_status,
    refresh_market_outlook_forecasts,
    resolve_market_outlook_asof,
    resolve_market_outlook_target,
    train_market_outlook_model,
)
from roboquant.market_outlook_ml_shadow import run_market_outlook_ml_shadow
from roboquant.signals.local_llm_news import (
    generate_local_llm_market_brief,
    load_local_llm_news_config,
)

KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh tomorrow market ML and experimental LLM-news shadow.")
    parser.add_argument("--config", default="configs/local_llm_news.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--target-date", default="latest", help="Upcoming KRX session date or latest.")
    parser.add_argument("--skip-shadow", action="store_true")
    parser.add_argument("--skip-brief", action="store_true")
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
            print(
                json.dumps(
                    {
                        "status": "blocked_stale_input",
                        "production_forecasts": 0,
                        "target_date": context["target_date"],
                        "domestic_asof_date": context["domestic_asof_date"],
                        "input_status": context,
                        "shadow": {"status": "skipped_stale_input", "production_weight": 0.0},
                        "brief": {"status": "skipped_stale_input"},
                    },
                    ensure_ascii=False,
                    default=str,
                )
            )
            return

        prediction_now = datetime.combine(target["target_date"], time(8, 0), tzinfo=KST)
        model_path = default_market_outlook_model_path(config)
        # Sparse optional sources are represented as missing, not synthetic
        # values. Their all-NaN medians are expected during Ridge preparation.
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=RuntimeWarning, message="Mean of empty slice")
            production_model = train_market_outlook_model(
                conn,
                config=config,
                model_path=model_path,
                asof_date=args.date,
            )
            production = refresh_market_outlook_forecasts(
                conn,
                config=config,
                asof_date=args.date,
                model_path=model_path,
                now=prediction_now,
                forecast_context=context,
            )
        shadow = (
            {"status": "skipped"}
            if args.skip_shadow
            else run_market_outlook_ml_shadow(conn, config, asof_date=args.date, now=prediction_now)
        )
        brief = (
            {"status": "skipped"}
            if args.skip_brief
            else generate_local_llm_market_brief(
                conn,
                config,
                stage="premarket",
                market_context={
                    "production_market_count": int(len(production)),
                    "shadow_status": shadow.get("status"),
                    "shadow_markets": [
                        {"market": item.get("market"), "gate_status": item.get("gate_status")}
                        for item in shadow.get("markets", [])
                    ],
                },
            )
        )
        print(
            json.dumps(
                {
                    "status": "ready",
                    "forecast_status": context["forecast_status"],
                    "target_date": context["target_date"],
                    "domestic_asof_date": context["domestic_asof_date"],
                    "production_model_version": production_model.get("model_version"),
                    "production_forecasts": int(len(production)),
                    "shadow": shadow,
                    "brief": brief,
                },
                ensure_ascii=False,
                default=str,
            )
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()

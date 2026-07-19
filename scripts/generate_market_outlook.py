#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.db import connect_database
from roboquant.market_outlook import (
    default_market_outlook_model_path,
    market_outlook_input_status,
    refresh_market_outlook_forecasts,
    resolve_market_outlook_asof,
    resolve_market_outlook_target,
)

KST = ZoneInfo("Asia/Seoul")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate KOSPI/KOSDAQ TODAY/WEEK market outlook forecasts.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--target-date", default="latest")
    parser.add_argument("--model-path", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    model_path = Path(args.model_path) if args.model_path else default_market_outlook_model_path(config)
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
                f"market_outlook_forecasts blocked_stale_input: target={target['target_date']} "
                f"asof={asof}; {' · '.join(context['messages'])}"
            )
            return
        frame = refresh_market_outlook_forecasts(
            conn,
            config=config,
            asof_date=args.date,
            model_path=model_path,
            now=datetime.combine(target["target_date"], time(8, 0), tzinfo=KST),
            forecast_context=context,
        )
    finally:
        conn.close()

    if frame.empty:
        print("market_outlook_forecasts rows: 0")
        return
    asof = frame["asof_date"].iloc[0]
    print(f"market_outlook_forecasts rows: {len(frame)} asof: {asof}")
    for _, row in frame.sort_values(["horizon", "market"]).iterrows():
        print(
            f"{row['horizon']} {row['market']}: expected={row['expected_return']:.4f} "
            f"up={row['up_probability']:.3f} shock={row['shock_probability']:.3f}"
        )


if __name__ == "__main__":
    main()

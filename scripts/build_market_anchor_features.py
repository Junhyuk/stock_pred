#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path
from roboquant.db import connect_database
from roboquant.market_anchor_shadow import (
    build_market_anchor_feature_history,
    load_market_anchor_config,
    market_anchor_settings,
    refresh_market_anchor_feature_store,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build leakage-safe KOSPI/KOSDAQ anchor shadow features.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--date", default="latest")
    parser.add_argument("--stage", default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def run_build(
    config_path: str | Path,
    *,
    asof_date: str = "latest",
    stage: str | None = None,
    dry_run: bool = False,
) -> dict:
    config = load_market_anchor_config(config_path)
    ensure_project_dirs(config)
    settings = market_anchor_settings(config)
    if dry_run:
        return {"status": "dry_run", "stage": stage or settings["forecast_stage"]}
    conn = connect_database(get_database_path(config))
    try:
        frame = build_market_anchor_feature_history(
            conn,
            config,
            asof_date=asof_date,
            stage=stage,
        )
        refresh_market_anchor_feature_store(conn, frame)
        return {
            "status": "ready" if not frame.empty else "not_ready",
            "feature_rows": len(frame),
            "markets": sorted(set(frame.get("market", []))),
            "feature_version": settings["feature_version"],
        }
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            run_build(
                args.config,
                asof_date=args.date,
                stage=args.stage,
                dry_run=args.dry_run,
            ),
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

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
    default_market_anchor_model_path,
    load_market_anchor_config,
    load_market_anchor_features,
    load_market_anchor_shadow_model,
    market_anchor_settings,
    refresh_market_anchor_backtest_runs,
    refresh_market_anchor_shadow_forecasts,
    run_market_anchor_shadow_backtest,
)
from roboquant.registry.model_registry import update_model_status
from roboquant.utils import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run expanding KOSPI/KOSDAQ anchor range shadow backtest.")
    parser.add_argument("--config", default="configs/market_anchor_shadow.yaml")
    parser.add_argument("--model-path", default=None)
    parser.add_argument("--report-path", default=None)
    return parser.parse_args()


def run_backtest(
    config_path: str | Path,
    *,
    model_path: str | Path | None = None,
    report_path: str | Path | None = None,
) -> dict:
    config = load_market_anchor_config(config_path)
    ensure_project_dirs(config)
    settings = market_anchor_settings(config)
    target = Path(model_path) if model_path else default_market_anchor_model_path(config)
    conn = connect_database(get_database_path(config))
    try:
        model_version = None
        if target.exists():
            model_version = str(load_market_anchor_shadow_model(target)["model_version"])
        frame = load_market_anchor_features(
            conn,
            feature_version=str(settings["feature_version"]),
            stage=str(settings["forecast_stage"]),
        )
        forecasts, summaries = run_market_anchor_shadow_backtest(
            frame,
            config,
            model_version=model_version,
        )
        refresh_market_anchor_shadow_forecasts(conn, forecasts)
        refresh_market_anchor_backtest_runs(conn, summaries)
        _update_registry_status(conn, str(settings["model_name"]), summaries)
        report = {
            "model_name": settings["model_name"],
            "model_version": model_version or f"{settings['model_name']}-backtest",
            "summaries": summaries,
            "forecast_rows": len(forecasts),
        }
        target_report = Path(report_path) if report_path else Path(config["paths"]["report_dir"]) / "market_anchor_shadow_backtest.json"
        write_json(target_report, report)
        return report
    finally:
        conn.close()


def _update_registry_status(conn, model_name: str, summaries: list[dict]) -> None:
    existing = conn.execute(
        "SELECT COUNT(*) FROM model_registry WHERE model_name = ?",
        [model_name],
    ).fetchone()
    if not existing or int(existing[0]) == 0:
        return
    statuses = {str(summary.get("gate_status") or "not_ready") for summary in summaries}
    if not summaries or "not_ready" in statuses:
        status = "experimental"
    elif statuses == {"accepted_shadow"}:
        status = "accepted_shadow"
    else:
        status = "rejected"
    reasons = sorted({str(summary["fail_reason"]) for summary in summaries if summary.get("fail_reason")})
    update_model_status(
        conn,
        model_name=model_name,
        status=status,
        production_weight=0.0,
        fail_reason="; ".join(reasons) if reasons else None,
        metrics={"markets": summaries},
    )


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            run_backtest(args.config, model_path=args.model_path, report_path=args.report_path),
            ensure_ascii=False,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

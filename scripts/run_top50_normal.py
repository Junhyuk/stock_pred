#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database

EXECUTION_MODEL = "5.5"
EXECUTION_QUALITY = "high"
EXECUTION_SPEED = "default"

LEGACY_TABLES = [
    "symbols",
    "features_daily",
    "labels",
    "predictions",
    "recommendations",
    "backtest_results",
    "model_performance_daily",
    "stock_clusters",
    "cluster_summary",
    "dashboard_snapshot",
]


class Step:
    def __init__(self, name: str, command: list[str], *, optional: bool = False) -> None:
        self.name = name
        self.command = command
        self.optional = optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the v8 Top50 normal retraining pipeline.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--universe-config", default="configs/universe_top50.yaml")
    parser.add_argument("--provider", default="fdr_poc")
    parser.add_argument("--flow-lookback-days", type=int, default=90)
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--skip-legacy-backup", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        f"execution: model={EXECUTION_MODEL}, quality={EXECUTION_QUALITY}, speed={EXECUTION_SPEED}",
        flush=True,
    )
    if args.dry_run:
        for step in build_steps(args):
            print(_format_step(step))
        return

    if not args.skip_legacy_backup:
        _backup_legacy_kospi100(args.config)

    for step in build_steps(args):
        if step.optional:
            _run_optional(step.command, step.name)
        else:
            _run_required(step.command)


def build_steps(args: argparse.Namespace) -> list[Step]:
    steps: list[Step] = []
    if not args.skip_refresh:
        steps.append(
            Step(
                "refresh_universe",
                [
                    "scripts/refresh_prediction_universe.py",
                    "--config",
                    args.universe_config,
                    "--date",
                    "latest",
                    "--provider",
                    args.provider,
                ],
            )
        )
    if not args.skip_collect:
        steps.append(
            Step(
                "collect_prediction_universe_prices",
                [
                    "scripts/collect_prediction_universe_prices.py",
                    "--config",
                    args.config,
                    "--snapshot-date",
                    "latest",
                ],
            )
        )
    if not args.skip_enrichment:
        today = date.today().isoformat()
        flow_start = (date.today() - timedelta(days=max(1, int(args.flow_lookback_days)))).isoformat()
        steps.extend(
            [
                Step(
                    "collect_market_news",
                    ["scripts/collect_market_news.py", "--config", "configs/market_news.yaml"],
                    optional=True,
                ),
                Step(
                    "collect_market_metrics",
                    ["scripts/collect_market_metrics.py", "--config", args.config, "--date", today],
                    optional=True,
                ),
                Step(
                    "collect_investor_flows",
                    [
                        "scripts/collect_investor_flows.py",
                        "--config",
                        args.config,
                        "--start",
                        flow_start,
                        "--end",
                        today,
                    ],
                    optional=True,
                ),
            ]
        )
    steps.append(Step("build_feature_matrix", ["scripts/build_feature_matrix.py", "--config", args.config]))
    config = load_config(args.config)
    train_horizons = config.get("pipeline", {}).get("train_horizons", ["3M", "6M"])
    for horizon in train_horizons:
        steps.append(
            Step(f"train_{horizon}", ["scripts/train_models.py", "--config", args.config, "--horizon", horizon])
        )
    steps.extend(
        [
            Step(
                "generate_recommendations",
                ["scripts/generate_recommendations.py", "--config", args.config, "--date", "latest"],
            ),
            Step(
                "generate_long_short_predictions",
                ["scripts/generate_long_short_predictions.py", "--config", args.config, "--date", "latest"],
            ),
            Step(
                "generate_market_up_down",
                ["scripts/generate_market_up_down.py", "--config", args.config, "--date", "latest"],
            ),
        ]
    )
    for horizon in config.get("pipeline", {}).get("long_short_horizons", ["2M", "6M"]):
        steps.append(
            Step(
                f"long_short_backtest_{horizon}",
                ["scripts/run_long_short_backtest.py", "--config", args.config, "--horizon", horizon],
            )
        )
    steps.append(Step("build_stock_clusters", ["scripts/build_stock_clusters.py", "--config", args.config, "--horizon", "3M"]))
    steps.extend(_prediction_backtest_steps(config, args.config))
    steps.extend(
        [
            Step("model_gatekeeper", ["scripts/run_model_gatekeeper.py", "--config", args.config]),
            Step("dashboard_snapshot", ["scripts/build_dashboard_snapshot.py", "--config", args.config, "--horizon", "3M"]),
            Step("market_move_explanations", ["scripts/build_market_move_explanations.py", "--config", args.config, "--date", "latest"]),
        ]
    )
    return steps


def _prediction_backtest_steps(config: dict, config_path: str) -> list[Step]:
    horizons = config.get("horizons", {})
    report_horizons = config.get("pipeline", {}).get("report_horizons", ["3M", "6M"])
    steps: list[Step] = []
    seen: set[int] = set()
    for horizon in report_horizons:
        days = horizons.get(str(horizon))
        if days is None:
            continue
        horizon_days = int(days)
        if horizon_days in seen:
            continue
        seen.add(horizon_days)
        steps.append(
            Step(
                f"prediction_backtest_{horizon_days}",
                ["scripts/run_prediction_backtest.py", "--config", config_path, "--horizon", str(horizon_days)],
            )
        )
    return steps


def _run_required(args: list[str]) -> None:
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _run_optional(args: list[str], name: str) -> None:
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode != 0:
        print(f"{name}: optional step failed with code {result.returncode}; continuing with available data")


def _backup_legacy_kospi100(config_path: str) -> None:
    config = load_config(config_path)
    conn = connect_database(get_database_path(config))
    try:
        tables = {row[0] for row in conn.execute("SHOW TABLES").fetchall()}
        for table in LEGACY_TABLES:
            legacy_table = f"legacy_kospi100_{table}"
            if table not in tables or legacy_table in tables:
                continue
            conn.execute(f"CREATE TABLE {legacy_table} AS SELECT * FROM {table}")
            print(f"legacy backup created: {legacy_table}")
    finally:
        conn.close()


def _format_step(step: Step) -> str:
    marker = "optional" if step.optional else "required"
    return f"{step.name} [{marker}]: {sys.executable} {' '.join(step.command)}"


if __name__ == "__main__":
    main()

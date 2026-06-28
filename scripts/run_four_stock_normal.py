#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

EXECUTION_MODEL = "5.5"
EXECUTION_QUALITY = "high"
EXECUTION_SPEED = "default"


class Step:
    def __init__(self, name: str, command: list[str], *, optional: bool = False) -> None:
        self.name = name
        self.command = command
        self.optional = optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the normal four-stock prediction demo pipeline.")
    parser.add_argument("--config", default="configs/two_stock_demo.yaml")
    parser.add_argument("--flow-lookback-days", type=int, default=90)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--reset-demo-data", action="store_true")
    parser.add_argument("--no-restart-web", action="store_true")
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

    if not args.no_restart_web:
        _stop_port(8000)
        _stop_port(8501)
    if args.reset_demo_data:
        _reset_demo_tables(args.config)

    for step in build_steps(args):
        if step.name == "collect_kospi100":
            _run_collect_or_reuse(step.command, args.config)
        elif step.optional:
            _run_optional(step.command, step.name)
        else:
            _run_required(step.command)

    if not args.no_restart_web:
        _start_web()


def build_steps(args: argparse.Namespace) -> list[Step]:
    steps: list[Step] = []
    if not args.skip_collect:
        steps.append(Step("collect_kospi100", ["scripts/collect_kospi100.py", "--config", args.config]))
    if not args.skip_enrichment:
        today = date.today().isoformat()
        flow_start = (date.today() - timedelta(days=max(1, int(args.flow_lookback_days)))).isoformat()
        steps.extend(
            [
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
    for horizon in ("3M", "6M"):
        steps.append(Step(f"train_{horizon}", ["scripts/train_models.py", "--config", args.config, "--horizon", horizon]))
    steps.extend(
        [
            Step(
                "generate_recommendations",
                ["scripts/generate_recommendations.py", "--config", args.config, "--date", "latest"],
            ),
            Step("build_stock_clusters", ["scripts/build_stock_clusters.py", "--config", args.config, "--horizon", "3M"]),
            Step("prediction_backtest_63", ["scripts/run_prediction_backtest.py", "--config", args.config, "--horizon", "63"]),
            Step("prediction_backtest_126", ["scripts/run_prediction_backtest.py", "--config", args.config, "--horizon", "126"]),
            Step("model_gatekeeper", ["scripts/run_model_gatekeeper.py", "--config", args.config]),
            Step("dashboard_snapshot", ["scripts/build_dashboard_snapshot.py", "--config", args.config, "--horizon", "3M"]),
            Step(
                "price_gap_backtest",
                ["scripts/run_prediction_price_gap_backtest.py", "--config", args.config, "--horizon", "3M"],
                optional=True,
            ),
        ]
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


def _run_collect_or_reuse(args: list[str], config_path: str) -> None:
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode == 0:
        return
    if _has_existing_prices(config_path):
        print("collect_kospi100: failed; reusing existing prices_daily rows for normal pipeline")
        return
    raise subprocess.CalledProcessError(result.returncode, command)


def _has_existing_prices(config_path: str) -> bool:
    code = f"""
from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database
conn = connect_database(get_database_path(load_config({config_path!r})))
try:
    count = conn.execute('SELECT COUNT(*) FROM prices_daily').fetchone()[0]
finally:
    conn.close()
raise SystemExit(0 if count > 0 else 1)
"""
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=False).returncode == 0


def _format_step(step: Step) -> str:
    marker = "optional" if step.optional else "required"
    return f"{step.name} [{marker}]: {sys.executable} {' '.join(step.command)}"


def _stop_port(port: int) -> None:
    result = subprocess.run(
        ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    for value in result.stdout.split():
        try:
            os.kill(int(value), signal.SIGTERM)
        except ProcessLookupError:
            pass


def _reset_demo_tables(config_path: str) -> None:
    code = f"""
from roboquant.config import get_database_path, load_config
from roboquant.db import connect_database
conn = connect_database(get_database_path(load_config({config_path!r})))
for table in [
    'symbols', 'prices_daily', 'benchmark_daily', 'market_metrics_daily',
    'investor_flows_daily', 'features_daily', 'labels', 'predictions',
    'recommendations', 'backtest_results', 'model_performance_daily',
    'stock_clusters', 'cluster_summary', 'dashboard_snapshot'
]:
    conn.execute(f'DELETE FROM {{table}}')
conn.close()
"""
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


def _start_web() -> None:
    log_dir = ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    api_log = (log_dir / "fastapi.log").open("ab")
    streamlit_log = (log_dir / "streamlit.log").open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=ROOT,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            "app_streamlit.py",
            "--server.address",
            "0.0.0.0",
            "--server.port",
            "8501",
            "--server.headless",
            "true",
        ],
        cwd=ROOT,
        stdout=streamlit_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print("Four-stock demo: http://localhost:8000/demo/four-stocks")
    print("Dashboard: http://localhost:8000/dashboard")
    print("Streamlit: http://localhost:8501")


if __name__ == "__main__":
    main()

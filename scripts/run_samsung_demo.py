#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build and serve the Samsung KOSPI-100 demo.")
    parser.add_argument("--config", default="configs/samsung_demo.yaml")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--reset-demo-data", action="store_true")
    parser.add_argument("--no-restart-web", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _stop_port(8000)
    _stop_port(8501)
    if args.reset_demo_data:
        _reset_demo_tables(args.config)

    if not args.skip_collect:
        _run("scripts/collect_kospi100.py", "--config", args.config)
    _run("scripts/build_feature_matrix.py", "--config", args.config)

    for horizon in ("3M", "6M"):
        trained = _run_optional("scripts/train_models.py", "--config", args.config, "--horizon", horizon)
        if not trained:
            print(f"{horizon}: model training failed; latest recommendations will use factor baseline")

    _run("scripts/generate_recommendations.py", "--config", args.config, "--date", "latest")
    _run("scripts/build_stock_clusters.py", "--config", args.config, "--horizon", "3M")
    for horizon_days in ("63", "126"):
        _run("scripts/run_prediction_backtest.py", "--config", args.config, "--horizon", horizon_days)
    _run("scripts/run_model_gatekeeper.py", "--config", args.config)
    _run("scripts/build_dashboard_snapshot.py", "--config", args.config, "--horizon", "3M")

    if not args.no_restart_web:
        _start_web()


def _run(*args: str) -> None:
    command = [sys.executable, *args]
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def _run_optional(*args: str) -> bool:
    try:
        _run(*args)
        return True
    except subprocess.CalledProcessError as exc:
        print(f"optional step failed ({exc.returncode}): {' '.join(args)}")
        return False


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
    'symbols', 'prices_daily', 'benchmark_daily', 'features_daily', 'labels',
    'predictions', 'recommendations', 'backtest_results', 'model_performance_daily',
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
        [str(ROOT / ".venv/bin/uvicorn"), "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=ROOT,
        stdout=api_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    subprocess.Popen(
        [
            str(ROOT / ".venv/bin/streamlit"),
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
    print("FastAPI: http://localhost:8000/dashboard")
    print("Streamlit: http://localhost:8501")


if __name__ == "__main__":
    main()

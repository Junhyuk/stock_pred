#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.freshness import expected_latest_trading_day, price_freshness_report
from roboquant.db import connect_database

EXECUTION_MODEL = "5.5"
EXECUTION_QUALITY = "high"
EXECUTION_SPEED = "default"


@dataclass(frozen=True)
class Step:
    name: str
    command: list[str]
    optional: bool = False
    internal: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run latest Top50 retraining with market impact explanations.")
    parser.add_argument("--config", default="configs/top50_normal.yaml")
    parser.add_argument("--universe-config", default="configs/universe_top50.yaml")
    parser.add_argument("--provider", default="fdr_poc")
    parser.add_argument("--target-date", default="latest")
    parser.add_argument("--flow-lookback-days", type=int, default=90)
    parser.add_argument("--restart-web", action="store_true")
    parser.add_argument("--skip-refresh", action="store_true")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--skip-enrichment", action="store_true")
    parser.add_argument("--skip-global", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(ROOT / ".env")
    target_date = _resolve_target_date(args.target_date)
    config = load_config(args.config)
    ensure_project_dirs(config)

    print(
        f"execution: model={EXECUTION_MODEL}, quality={EXECUTION_QUALITY}, "
        f"speed={EXECUTION_SPEED}, target_date={target_date.isoformat()}",
        flush=True,
    )

    if args.dry_run:
        for step in build_steps(args, include_training=True):
            print(_format_step(step))
        return

    if args.restart_web:
        _stop_web()

    status = "failed"
    freshness: dict | None = None
    executed: list[dict] = []
    try:
        for step in build_collection_steps(args, target_date):
            executed.append(_run_step(step))

        freshness = check_price_freshness(args.config, target_date).to_dict()
        executed.append({"name": "freshness_check", "status": freshness["status"], "freshness": freshness})
        if freshness["stale"]:
            if args.restart_web:
                for step in build_partial_finalize_steps(args):
                    executed.append(_run_step(step))
            else:
                executed.append(
                    {
                        "name": "market_move_explanations_stale_snapshot",
                        "status": "skipped_requires_restart_web",
                        "message": "DB write skipped while web may hold DuckDB lock. Re-run with --restart-web to refresh stale snapshot.",
                    }
                )
            status = "partial_ready"
            print(json.dumps({"status": status, "freshness": freshness, "steps": executed}, ensure_ascii=False))
            return

        for step in build_training_steps(args):
            executed.append(_run_step(step))
        for step in build_finalize_steps(args):
            executed.append(_run_step(step))
        status = "ready"
        print(json.dumps({"status": status, "freshness": freshness, "steps": executed}, ensure_ascii=False))
    finally:
        if args.restart_web:
            _start_web()


def build_steps(args: argparse.Namespace, *, include_training: bool = True) -> list[Step]:
    target_date = _resolve_target_date(args.target_date)
    steps: list[Step] = []
    if getattr(args, "restart_web", False):
        steps.append(Step("stop_web", [], internal=True))
    steps.extend(build_collection_steps(args, target_date))
    steps.append(Step("freshness_check", [], internal=True))
    if include_training:
        steps.extend(build_training_steps(args))
        steps.extend(build_finalize_steps(args))
    else:
        steps.extend(build_partial_finalize_steps(args))
    if getattr(args, "restart_web", False):
        steps.append(Step("start_web", [], internal=True))
    return steps


def build_collection_steps(args: argparse.Namespace, target_date: date) -> list[Step]:
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
                    target_date.isoformat(),
                    "--provider",
                    args.provider,
                ],
            )
        )
    if not args.skip_collect:
        steps.extend(
            [
                Step(
                    "collect_prediction_universe_prices",
                    [
                        "scripts/collect_prediction_universe_prices.py",
                        "--config",
                        args.config,
                        "--snapshot-date",
                        "latest",
                        "--to-date",
                        target_date.isoformat(),
                    ],
                ),
                Step(
                    "collect_market_indices",
                    [
                        "scripts/collect_market_indices.py",
                        "--config",
                        args.config,
                        "--end",
                        target_date.isoformat(),
                    ],
                    optional=True,
                ),
            ]
        )
    if not args.skip_enrichment:
        flow_start = (target_date - timedelta(days=max(1, int(args.flow_lookback_days)))).isoformat()
        steps.extend(
            [
                Step(
                    "collect_market_metrics",
                    ["scripts/collect_market_metrics.py", "--config", args.config, "--date", target_date.isoformat()],
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
                        target_date.isoformat(),
                    ],
                    optional=True,
                ),
            ]
        )
    if not args.skip_global:
        steps.append(
            Step(
                "global_market_regime",
                [
                    "scripts/run_premarket_global_pipeline.py",
                    "--config",
                    "configs/global_market.yaml",
                    "--from-date",
                    "2022-01-01",
                    "--to-date",
                    target_date.isoformat(),
                    "--cutoff",
                    "now",
                    "--prediction-date",
                    target_date.isoformat(),
                ],
                optional=True,
            )
        )
    if not args.skip_news:
        steps.extend(
            [
                Step(
                    "collect_market_news",
                    ["scripts/collect_market_news.py", "--config", "configs/market_news.yaml"],
                    optional=True,
                ),
                Step(
                    "collect_naver_news_top50",
                    [
                        "scripts/collect_naver_news.py",
                        "--config",
                        args.config,
                        "--date",
                        target_date.isoformat(),
                        "--universe-rule",
                        "prediction_top_market_cap",
                        "--allow-missing-key",
                    ],
                    optional=True,
                ),
                Step(
                    "collect_telegram_signals",
                    ["scripts/collect_telegram_signals.py", "--config", "configs/telegram_signals.yaml"],
                    optional=True,
                ),
            ]
        )
    if not args.skip_enrichment:
        steps.append(
            Step(
                "collect_market_credit_balance",
                [
                    "scripts/collect_market_credit_balance.py",
                    "--config",
                    args.config,
                    "--date",
                    target_date.isoformat(),
                    "--allow-missing-key",
                ],
                optional=True,
            )
        )
    return steps


def build_training_steps(args: argparse.Namespace) -> list[Step]:
    config = load_config(args.config)
    steps = [
        Step("build_us_sector_linkage", ["scripts/build_us_sector_linkage.py", "--config", "configs/global_market.yaml", "--date", "latest"]),
        Step("build_koru_korea_linkage", ["scripts/build_koru_korea_linkage.py", "--config", args.config, "--date", "latest"]),
        Step("build_feature_matrix", ["scripts/build_feature_matrix.py", "--config", args.config]),
    ]
    for horizon in config.get("pipeline", {}).get("train_horizons", ["2M", "3M", "6M"]):
        steps.append(Step(f"train_{horizon}", ["scripts/train_models.py", "--config", args.config, "--horizon", horizon]))
    steps.extend(
        [
            Step("koru_weight_gate", ["scripts/run_koru_weight_gate.py", "--config", args.config]),
            Step("generate_recommendations", ["scripts/generate_recommendations.py", "--config", args.config, "--date", "latest"]),
            Step("generate_long_short_predictions", ["scripts/generate_long_short_predictions.py", "--config", args.config, "--date", "latest"]),
            Step("generate_market_up_down", ["scripts/generate_market_up_down.py", "--config", args.config, "--date", "latest"]),
            Step("build_market_outlook_features", ["scripts/build_market_outlook_features.py", "--config", args.config, "--date", "latest"]),
            Step("train_market_outlook", ["scripts/train_market_outlook.py", "--config", args.config, "--date", "latest"]),
            Step("generate_market_outlook", ["scripts/generate_market_outlook.py", "--config", args.config, "--date", "latest"]),
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
    steps.append(Step("model_gatekeeper", ["scripts/run_model_gatekeeper.py", "--config", args.config]))
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


def build_finalize_steps(args: argparse.Namespace) -> list[Step]:
    return [
        Step("dashboard_snapshot", ["scripts/build_dashboard_snapshot.py", "--config", args.config, "--horizon", "3M"]),
        Step("market_move_explanations", ["scripts/build_market_move_explanations.py", "--config", args.config, "--date", "latest"]),
        Step("today_context_refresh", _today_context_refresh_command(), optional=True),
    ]


def build_partial_finalize_steps(args: argparse.Namespace) -> list[Step]:
    return [
        Step(
            "market_move_explanations_stale_snapshot",
            ["scripts/build_market_move_explanations.py", "--config", args.config, "--date", "latest"],
            optional=True,
        ),
        Step("today_context_refresh", _today_context_refresh_command(), optional=True),
    ]


def _today_context_refresh_command() -> list[str]:
    return [
        "scripts/run_today_market_update.py",
        "--config",
        "configs/today_update.yaml",
        "--skip-global",
        "--skip-news",
        "--skip-market-news",
        "--skip-move-explanations",
    ]


def check_price_freshness(config_path: str, target_date: date):
    config = load_config(config_path)
    conn = connect_database(get_database_path(config), read_only=True, initialize_schema=False)
    try:
        return price_freshness_report(conn, expected_date=target_date)
    finally:
        conn.close()


def _run_step(step: Step) -> dict:
    if step.internal:
        return {"name": step.name, "status": "internal"}
    command = [sys.executable, *step.command]
    print("+", " ".join(command), flush=True)
    result = subprocess.run(command, cwd=ROOT, check=False)
    if result.returncode == 0:
        return {"name": step.name, "status": "success"}
    if step.optional:
        print(f"{step.name}: optional step failed with code {result.returncode}; continuing with available data")
        return {"name": step.name, "status": "failed_optional", "returncode": result.returncode}
    raise subprocess.CalledProcessError(result.returncode, command)


def _format_step(step: Step) -> str:
    marker = "optional" if step.optional else "required"
    if step.internal:
        return f"{step.name} [{marker}]: internal"
    return f"{step.name} [{marker}]: {sys.executable} {' '.join(step.command)}"


def _resolve_target_date(value: str) -> date:
    if str(value).strip().lower() == "latest":
        return expected_latest_trading_day()
    return expected_latest_trading_day(date.fromisoformat(str(value)))


def _stop_web() -> None:
    subprocess.run(["bash", "-lc", "fuser -k 8000/tcp 8501/tcp >/dev/null 2>&1 || true"], cwd=ROOT, check=False)


def _start_web() -> None:
    log_dir = ROOT / "reports" / "latest_market_impact"
    log_dir.mkdir(parents=True, exist_ok=True)
    fastapi_log = (log_dir / "fastapi.log").open("ab")
    streamlit_log = (log_dir / "streamlit.log").open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"],
        cwd=ROOT,
        stdout=fastapi_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "app_streamlit.py", "--server.address", "0.0.0.0", "--server.port", "8501"],
        cwd=ROOT,
        stdout=streamlit_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(2)
    print("FastAPI: http://localhost:8000/demo/today")
    print("Streamlit: http://localhost:8501")


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()

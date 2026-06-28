#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.dashboard.dashboard_service import build_today_market_snapshot
from roboquant.data.collectors.failures import collection_failure_row
from roboquant.data.providers.naver_news import (
    NaverNewsConfigurationError,
    NaverNewsProvider,
    queries_from_config,
)
from roboquant.data.providers.yahoo_unofficial import YahooUnofficialProvider, symbols_from_config
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import KST
from roboquant.signals.market_move_explanations import refresh_market_move_explanations


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run today's local market/news/global update pipeline.")
    parser.add_argument("--config", default="configs/today_update.yaml")
    parser.add_argument("--restart-web", action="store_true")
    parser.add_argument("--skip-global", action="store_true")
    parser.add_argument("--skip-yahoo", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    parser.add_argument("--skip-market-news", action="store_true")
    parser.add_argument("--skip-move-explanations", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    _load_dotenv(ROOT / ".env")
    if args.restart_web:
        _stop_web()
    config = load_config(args.config)
    ensure_project_dirs(config)
    run_id = uuid4().hex
    started_at = _utcnow()
    steps: list[dict] = []
    if not args.skip_market_news:
        steps.append(_run_market_news_step(config))
    else:
        steps.append({"name": "market_news", "status": "skipped"})
    conn = connect_database(get_database_path(config))
    try:
        _write_run(conn, run_id, "running", started_at, None, steps, None)
        if not args.skip_yahoo:
            steps.append(_run_yahoo_step(conn, config))
        else:
            steps.append({"name": "yahoo_unofficial", "status": "skipped"})
        if not args.skip_global:
            steps.append(_run_global_step(config))
        else:
            steps.append({"name": "global_market", "status": "skipped"})
        if not args.skip_news:
            steps.append(_run_news_step(conn, config))
        else:
            steps.append({"name": "naver_news", "status": "skipped"})
        if not args.skip_move_explanations:
            steps.append(_run_move_explanations_step(conn, config))
        else:
            steps.append({"name": "market_move_explanations", "status": "skipped"})
        snapshot = build_today_market_snapshot(conn, config)
        status = "ready" if snapshot.get("status") == "ready" else "partial_ready"
        _write_run(conn, run_id, status, started_at, _utcnow(), steps, None)
        print(json.dumps({"run_id": run_id, "status": status, "snapshot": snapshot["status"]}, ensure_ascii=False))
    except Exception as exc:
        _write_run(conn, run_id, "failed", started_at, _utcnow(), steps, str(exc))
        raise
    finally:
        conn.close()
    if args.restart_web:
        _start_web()


def _run_market_news_step(config: dict) -> dict:
    settings = config.get("market_news", {})
    command = [
        sys.executable,
        "scripts/collect_market_news.py",
        "--config",
        str(settings.get("config", "configs/market_news.yaml")),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        return {
            "name": "market_news",
            "status": "failed",
            "message": (result.stderr or result.stdout).strip()[-1000:],
        }
    return {"name": "market_news", "status": "success", "message": result.stdout.strip()[-1000:]}


def _run_yahoo_step(conn, config: dict) -> dict:
    if str(os.environ.get("ALLOW_UNOFFICIAL_YAHOO", "")).lower() != "true":
        return {
            "name": "yahoo_unofficial",
            "status": "skipped_missing_opt_in",
            "message": "ALLOW_UNOFFICIAL_YAHOO=true가 없어 Yahoo/yfinance 수집을 건너뜁니다.",
        }
    settings = config.get("yahoo_unofficial", {})
    symbols = symbols_from_config(settings.get("symbols", []))
    max_symbols = int(settings.get("max_symbols", 100))
    if len(symbols) > max_symbols:
        raise ValueError(f"Too many Yahoo symbols: {len(symbols)} > {max_symbols}")
    provider = YahooUnofficialProvider()
    end_date = datetime.now(KST).date()
    start_date = end_date - timedelta(days=7)
    sleep_seconds = float(settings.get("sleep_seconds", 1.0))
    rows = 0
    failures = 0
    for spec in symbols:
        try:
            frame = provider.get_price_history([spec], start_date, end_date)
            append_dedup_table(conn, "yahoo_prices_daily", frame, ["date", "yahoo_symbol"])
            rows += len(frame)
        except Exception as exc:
            failures += 1
            append_dedup_table(
                conn,
                "collection_failures",
                collection_failure_row(
                    step="run_today_market_update.yahoo",
                    source="yahoo_unofficial",
                    symbol=spec.yahoo_symbol,
                    target_date=end_date.isoformat(),
                    error=exc,
                ),
                ["collected_at", "step", "source", "error_message"],
            )
        time.sleep(max(0.0, sleep_seconds))
    return {"name": "yahoo_unofficial", "status": "success", "rows": rows, "failures": failures}


def _run_global_step(config: dict) -> dict:
    settings = config.get("global_market", {})
    command = [
        sys.executable,
        "scripts/run_premarket_global_pipeline.py",
        "--config",
        str(settings.get("config", "configs/global_market.yaml")),
        "--from-date",
        str(settings.get("from_date", "2022-01-01")),
        "--to-date",
        str(settings.get("to_date", "latest")),
        "--cutoff",
        str(settings.get("cutoff", "latest")),
        "--prediction-date",
        str(settings.get("prediction_date", "latest")),
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
    if result.returncode != 0:
        return {
            "name": "global_market",
            "status": "failed",
            "message": (result.stderr or result.stdout).strip()[-1000:],
        }
    return {"name": "global_market", "status": "success", "message": result.stdout.strip()[-1000:]}


def _run_news_step(conn, config: dict) -> dict:
    settings = config.get("news", {})
    if not settings.get("enabled", True):
        return {"name": "naver_news", "status": "skipped_disabled"}
    try:
        provider = NaverNewsProvider()
    except NaverNewsConfigurationError as exc:
        append_dedup_table(
            conn,
            "collection_failures",
            collection_failure_row(
                step="collect_naver_news",
                source="naver_search_api",
                target_date=datetime.now(KST).date().isoformat(),
                error=exc,
            ),
            ["collected_at", "step", "source", "error_message"],
        )
        return {"name": "naver_news", "status": "skipped_missing_key", "message": str(exc)}
    frame = provider.fetch_articles(
        queries_from_config(config.get("focus_stocks", []), str(settings.get("query_template", "{name} 주가"))),
        query_date=datetime.now(KST).date(),
        display=int(settings.get("display", 10)),
        sort=str(settings.get("sort", "date")),
    )
    append_dedup_table(conn, "news_articles", frame, ["article_id"])
    return {"name": "naver_news", "status": "success", "rows": len(frame)}


def _run_move_explanations_step(conn, config: dict) -> dict:
    frame = refresh_market_move_explanations(conn, config, asof_date="latest")
    triggered = int(frame["triggered"].sum()) if not frame.empty else 0
    return {"name": "market_move_explanations", "status": "success", "rows": len(frame), "triggered": triggered}


def _write_run(
    conn,
    run_id: str,
    status: str,
    started_at: datetime,
    completed_at: datetime | None,
    steps: list[dict],
    error_message: str | None,
) -> None:
    row = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "run_date": datetime.now(KST).date(),
                "status": status,
                "started_at": started_at,
                "completed_at": completed_at,
                "steps_json": json.dumps(steps, ensure_ascii=False, allow_nan=False),
                "error_message": error_message,
            }
        ]
    )
    append_dedup_table(conn, "today_market_update_runs", row, ["run_id"])


def _stop_web() -> None:
    subprocess.run(["bash", "-lc", "fuser -k 8000/tcp 8501/tcp >/dev/null 2>&1 || true"], cwd=ROOT, check=False)


def _start_web() -> None:
    log_dir = ROOT / "reports" / "today_update"
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


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


if __name__ == "__main__":
    main()

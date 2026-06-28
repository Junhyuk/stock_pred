#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.dashboard.price_gap_service import build_prediction_price_gap
from roboquant.db import connect_database


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backtest recent prediction-to-price return gaps.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--target-days", type=int, default=30)
    parser.add_argument("--horizon", default="3M", help="3M, 6M, or all")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols such as 005930,000660")
    parser.add_argument("--as-of-date", default=None)
    parser.add_argument("--completed-only", action="store_true")
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--report-dir", default="reports")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config), read_only=True, initialize_schema=False)
    try:
        payload = build_prediction_price_gap(
            conn,
            lookback_days=args.lookback_days,
            target_days=args.target_days,
            horizon=args.horizon,
            symbols=_parse_symbols(args.symbols),
            as_of_date=args.as_of_date,
            include_pending=not args.completed_only,
            limit=args.limit,
        )
    finally:
        conn.close()
    report_dir = Path(args.report_dir)
    if not report_dir.is_absolute():
        report_dir = ROOT / report_dir
    report_dir.mkdir(parents=True, exist_ok=True)
    stem = f"prediction_price_gap_{args.lookback_days}d_{args.horizon.lower()}"
    csv_path = report_dir / f"{stem}.csv"
    md_path = report_dir / f"{stem}.md"
    pd.DataFrame(payload["items"]).to_csv(csv_path, index=False)
    md_path.write_text(_markdown_report(payload), encoding="utf-8")
    print(f"status: {payload['status']}")
    print(f"sample_count: {payload['summary']['sample_count']}")
    print(f"completed_count: {payload['summary']['completed_count']}")
    print(f"pending_count: {payload['summary']['pending_count']}")
    print(f"csv: {csv_path}")
    print(f"markdown: {md_path}")


def _parse_symbols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip().zfill(6) for item in value.split(",") if item.strip()]


def _markdown_report(payload: dict) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Prediction Price Gap Backtest",
        "",
        f"- status: `{payload.get('status')}`",
        f"- horizon: `{payload.get('horizon')}`",
        f"- lookback_days: `{payload.get('lookback_days')}`",
        f"- target_days: `{payload.get('target_days')}`",
        f"- as_of_date: `{payload.get('as_of_date')}`",
        f"- sample_count: `{summary.get('sample_count')}`",
        f"- completed_count: `{summary.get('completed_count')}`",
        f"- pending_count: `{summary.get('pending_count')}`",
        f"- missing_count: `{summary.get('missing_count')}`",
        f"- MAE latest: `{_pct(summary.get('mae_latest'))}`",
        f"- Bias latest: `{_pct(summary.get('bias_latest'))}`",
        f"- Direction accuracy latest: `{_pct(summary.get('direction_accuracy_latest'))}`",
        f"- MAE {payload.get('target_days')}D completed: `{_pct(summary.get('mae_30d'))}`",
        f"- Direction accuracy {payload.get('target_days')}D completed: `{_pct(summary.get('direction_accuracy_30d'))}`",
        "",
        "결과는 연구·정보제공용이며, 목표일 미도래 예측은 pending으로 분리했습니다.",
    ]
    return "\n".join(lines) + "\n"


def _pct(value) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


if __name__ == "__main__":
    main()

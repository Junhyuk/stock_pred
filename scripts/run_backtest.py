#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.backtest.engine import run_topk_backtest
from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.loaders import load_modeling_dataset, load_prediction_dataset
from roboquant.db import connect_database
from roboquant.models.train import baseline_predictions
from roboquant.recommend.scorer import score_predictions
from roboquant.reports.generate_report import (
    render_backtest_comparison_html,
    render_backtest_html,
    write_text,
)
from roboquant.utils import write_json


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Top-K recommendation backtest.")
    parser.add_argument("--config", default="configs/poc.yaml")
    parser.add_argument("--horizon", required=True)
    parser.add_argument("--top-k", type=int, default=20)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    ensure_project_dirs(config)
    conn = connect_database(get_database_path(config))
    report_dir = Path(config["paths"]["report_dir"])

    scored = load_prediction_dataset(conn, args.horizon)
    dataset = load_modeling_dataset(conn, args.horizon)
    comparison_rows = []
    if scored.empty:
        print("No stored predictions found; using factor baseline predictions for backtest.")
        predictions = baseline_predictions(dataset, args.horizon)
        scored = score_predictions(predictions, dataset, config.get("recommendation", {}).get("weights"))
        labels = dataset.rename(columns={"date": "asof_date"})
        labels["asof_date"] = pd.to_datetime(labels["asof_date"]).dt.date
        scored = scored.merge(
            labels[
                [
                    "asof_date",
                    "symbol",
                    "horizon",
                    "future_return",
                    "benchmark_return",
                    "excess_return",
                    "is_top20pct",
                ]
            ],
            on=["asof_date", "symbol", "horizon"],
            how="left",
        )
    else:
        scored = score_predictions(scored, None, config.get("recommendation", {}).get("weights"))

    backtest_cfg = config.get("backtest", {})
    curve, summary = run_topk_backtest(
        scored,
        horizon=args.horizon,
        top_k=args.top_k,
        transaction_cost_bps=float(backtest_cfg.get("transaction_cost_bps", 30)),
        rebalance_frequency=str(backtest_cfg.get("rebalance_frequency", "M")),
    )
    comparison_rows.append({"baseline": "model_or_factor", **summary})

    if not dataset.empty:
        labels = dataset.rename(columns={"date": "asof_date"})
        labels["asof_date"] = pd.to_datetime(labels["asof_date"]).dt.date
        factor_scored = score_predictions(
            baseline_predictions(dataset, args.horizon, model_version="factor-baseline-comparison"),
            dataset,
            config.get("recommendation", {}).get("weights"),
        ).merge(
            labels[
                [
                    "asof_date",
                    "symbol",
                    "horizon",
                    "future_return",
                    "benchmark_return",
                    "excess_return",
                    "is_top20pct",
                ]
            ],
            on=["asof_date", "symbol", "horizon"],
            how="left",
        )
        _, factor_summary = run_topk_backtest(
            factor_scored,
            horizon=args.horizon,
            top_k=args.top_k,
            transaction_cost_bps=float(backtest_cfg.get("transaction_cost_bps", 30)),
            rebalance_frequency=str(backtest_cfg.get("rebalance_frequency", "M")),
        )
        comparison_rows.append({"baseline": "factor", **factor_summary})

        random_scored = _random_scored(dataset, args.horizon)
        _, random_summary = run_topk_backtest(
            random_scored,
            horizon=args.horizon,
            top_k=args.top_k,
            transaction_cost_bps=float(backtest_cfg.get("transaction_cost_bps", 30)),
            rebalance_frequency=str(backtest_cfg.get("rebalance_frequency", "M")),
        )
        comparison_rows.append({"baseline": "random", **random_summary})

    html = render_backtest_html(curve, summary, args.horizon)
    write_text(report_dir / f"backtest_{args.horizon}.html", html)
    comparison = pd.DataFrame(comparison_rows)
    write_text(
        report_dir / f"backtest_comparison_{args.horizon}.html",
        render_backtest_comparison_html(comparison, args.horizon),
    )
    if not curve.empty:
        curve.to_csv(report_dir / f"backtest_{args.horizon}_top{args.top_k}.csv", index=False)
    write_json(report_dir / f"backtest_{args.horizon}_summary.json", summary)
    print(summary)


def _random_scored(dataset: pd.DataFrame, horizon: str) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    frame = dataset[dataset["horizon"] == horizon].copy()
    frame["asof_date"] = pd.to_datetime(frame["date"]).dt.date
    frame["final_score"] = rng.random(len(frame))
    return frame[
        [
            "asof_date",
            "symbol",
            "horizon",
            "final_score",
            "future_return",
            "benchmark_return",
            "excess_return",
            "is_top20pct",
        ]
    ]


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.backtest.engine import run_topk_backtest
from roboquant.backtest.gatekeeper import evaluate_gate, production_weight_for_decision
from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.loaders import load_modeling_dataset, load_prediction_dataset
from roboquant.db import connect_database
from roboquant.models.train import baseline_predictions
from roboquant.registry.model_registry import (
    load_model_predictions,
    record_backtest_run,
    update_model_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run DNN shadow model backtest gate.")
    parser.add_argument("--config", default="configs/backtest_gate.yaml")
    parser.add_argument("--model", default=None)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--horizon", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    gate_config = _load_yaml(args.config)
    project_config = load_config(_rooted(gate_config.get("config", "configs/poc.yaml")))
    ensure_project_dirs(project_config)
    conn = connect_database(get_database_path(project_config))
    gate = gate_config.get("gate", {})
    model_name = args.model or str(gate.get("candidate_model", "patchtst_v1_lookback252"))
    baseline_name = args.baseline or str(gate.get("baseline_model", "lightgbm"))
    horizons = [args.horizon] if args.horizon else list(gate.get("horizons", ["3M", "6M"]))
    top_k = int(gate.get("top_k", 20))
    accepted_all = True
    fail_reasons: list[str] = []
    combined_metrics: dict[str, object] = {}

    for horizon in horizons:
        dataset = load_modeling_dataset(conn, horizon)
        candidate = _candidate_scored(conn, model_name, horizon, dataset)
        baseline = _baseline_scored(conn, baseline_name, horizon, dataset)
        candidate_curve, candidate_summary = run_topk_backtest(
            candidate,
            horizon=horizon,
            top_k=top_k,
            transaction_cost_bps=float(gate.get("transaction_cost_bps", 30)),
            rebalance_frequency=str(gate.get("rebalance_frequency", "M")),
            score_column="pred_prob_top20",
        )
        baseline_curve, baseline_summary = run_topk_backtest(
            baseline,
            horizon=horizon,
            top_k=top_k,
            transaction_cost_bps=float(gate.get("transaction_cost_bps", 30)),
            rebalance_frequency=str(gate.get("rebalance_frequency", "M")),
            score_column="pred_prob_top20",
        )
        candidate_summary = _augment_summary(candidate_curve, candidate_summary)
        baseline_summary = _augment_summary(baseline_curve, baseline_summary)
        accepted, reason, decision_metrics = evaluate_gate(
            candidate_summary,
            baseline_summary,
            gate,
            horizon=horizon,
        )
        if not accepted:
            accepted_all = False
            fail_reasons.append(f"{horizon}: {reason}")
        record_backtest_run(
            conn,
            model_name=model_name,
            baseline_model_name=baseline_name,
            horizon=horizon,
            metrics={**candidate_summary, "baseline": baseline_summary, "decision": decision_metrics},
            accepted=accepted,
            fail_reason=reason,
            top_k=top_k,
            start_date=candidate_curve["asof_date"].min() if not candidate_curve.empty else None,
            end_date=candidate_curve["asof_date"].max() if not candidate_curve.empty else None,
        )
        combined_metrics[horizon] = {
            "candidate": candidate_summary,
            "baseline": baseline_summary,
            "accepted": accepted,
            "reason": reason,
            "decision": decision_metrics,
        }
        print(f"{horizon}: accepted={accepted} reason={reason}")

    final_status = "accepted" if accepted_all else "rejected"
    final_reason = "accepted" if accepted_all else "; ".join(fail_reasons)
    update_model_status(
        conn,
        model_name=model_name,
        status=final_status,
        production_weight=production_weight_for_decision(accepted_all, gate),
        fail_reason=final_reason,
        metrics=combined_metrics,
    )
    print({"model_name": model_name, "status": final_status, "fail_reason": final_reason})


def _candidate_scored(conn, model_name: str, horizon: str, dataset: pd.DataFrame) -> pd.DataFrame:
    predictions = load_model_predictions(conn, model_name=model_name, horizon=horizon)
    if predictions.empty:
        raise ValueError(f"No model_predictions found for {model_name} {horizon}")
    labels = dataset.rename(columns={"date": "asof_date"}).copy()
    labels["asof_date"] = pd.to_datetime(labels["asof_date"]).dt.date
    frame = predictions.rename(columns={"date": "asof_date"}).copy()
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["pred_return"] = frame["pred_score"]
    frame["pred_prob_top20"] = frame["pred_prob"]
    frame["pred_risk"] = 0.5
    frame["confidence"] = frame["pred_prob_top20"].map(lambda value: max(float(value), 1.0 - float(value)))
    frame["model_version"] = model_name
    return frame.merge(
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
        how="inner",
    )


def _baseline_scored(conn, baseline_name: str, horizon: str, dataset: pd.DataFrame) -> pd.DataFrame:
    scored = pd.DataFrame()
    if baseline_name == "lightgbm":
        scored = load_prediction_dataset(conn, horizon)
        if not scored.empty:
            scored = scored.sort_values("model_version").drop_duplicates(
                ["asof_date", "symbol", "horizon"],
                keep="last",
            )
    if scored.empty:
        predictions = baseline_predictions(dataset, horizon, model_version="factor-baseline-gate")
        labels = dataset.rename(columns={"date": "asof_date"}).copy()
        labels["asof_date"] = pd.to_datetime(labels["asof_date"]).dt.date
        scored = predictions.merge(
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
    return scored


def _augment_summary(curve: pd.DataFrame, summary: dict) -> dict:
    output = dict(summary)
    if curve.empty:
        output.setdefault("top20_return", None)
        output.setdefault("transaction_cost_adjusted_return", None)
        return output
    output["top20_return"] = float(curve["net_return"].mean())
    output["transaction_cost_adjusted_return"] = float(curve["net_return"].mean())
    return output


def _load_yaml(path: str | Path) -> dict:
    with _rooted(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _rooted(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    main()

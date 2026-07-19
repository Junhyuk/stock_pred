#!/usr/bin/env python3
"""Run the isolated MarketNewsFeed local-LLM A/B shadow experiment."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.backtest.engine import attach_forward_returns, run_topk_backtest
from roboquant.config import get_database_path, get_feature_columns, get_horizons, load_config
from roboquant.data.loaders import load_labels, load_modeling_dataset
from roboquant.db import append_dedup_table, connect_database
from roboquant.marketnewsfeed_llm_shadow import (
    build_market_range_shadow,
    evaluate_shadow_gate,
    persist_market_range_shadow,
)
from roboquant.models.train import train_horizon_model
from roboquant.registry.model_registry import (
    record_backtest_run,
    register_feature_set,
    register_model,
)
from roboquant.signals.marketnewsfeed_llm import LLM_FEATURE_COLUMNS, settings_from_config


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate local MarketNewsFeed LLM features in shadow mode.")
    parser.add_argument("--config", default="configs/marketnewsfeed_llm.yaml")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = run_shadow(args.config)
    print(json.dumps(result, ensure_ascii=False, default=str))


def run_shadow(config_path: str | Path = "configs/marketnewsfeed_llm.yaml") -> dict:
    config = load_config(config_path)
    settings = settings_from_config(config)
    shadow = dict(config.get("marketnewsfeed_llm", {}).get("shadow") or {})
    base_config = load_config(_resolve_base_config(config, str(config["marketnewsfeed_llm"].get("base_config"))))
    database = get_database_path(config)
    conn = connect_database(database)
    try:
        report_dir = Path(config.get("paths", {}).get("report_dir") or ROOT / "reports" / "marketnewsfeed_llm_shadow")
        report_dir.mkdir(parents=True, exist_ok=True)
        model_name = str(shadow.get("model_name") or "marketnewsfeed_llm_shadow_v1")
        candidate_features = [*get_feature_columns(base_config), *LLM_FEATURE_COLUMNS]
        feature_set_name = f"{model_name}_features"
        register_feature_set(
            conn,
            feature_set_name,
            candidate_features,
            status="experimental",
            description="Local MarketNewsFeed structured signals; shadow-only until A/B gate.",
        )
        signal_days = int(
            conn.execute(
                "SELECT COUNT(DISTINCT signal_date) FROM marketnewsfeed_llm_signal_daily WHERE llm_news_coverage > 0"
            ).fetchone()[0]
        )
        model_rows = []
        for horizon in [str(value) for value in shadow.get("horizons") or ["2M", "3M"]]:
            model_rows.append(
                _run_horizon_shadow(
                    conn,
                    base_config=base_config,
                    model_name=model_name,
                    feature_set_name=feature_set_name,
                    horizon=horizon,
                    candidate_features=candidate_features,
                    signal_days=signal_days,
                    shadow=shadow,
                )
            )
        range_frame, range_summaries = build_market_range_shadow(
            conn,
            model_version=f"{model_name}-market-range-v1",
            min_signal_days=settings.min_signal_days_for_shadow,
        )
        persist_market_range_shadow(conn, range_frame)
        report = {
            "generated_at": _utcnow().isoformat(),
            "model_name": model_name,
            "production_weight": 0.0,
            "signal_days": signal_days,
            "stock_shadow": model_rows,
            "market_range_shadow": range_summaries,
            "disclaimer": "연구·정보제공용 shadow 검증이며 production 예측과 추천에는 반영되지 않습니다.",
        }
        report_path = report_dir / f"marketnewsfeed_llm_shadow_{_utcnow().date().isoformat()}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        markdown_path = report_path.with_suffix(".md")
        markdown_path.write_text(_report_markdown(report), encoding="utf-8")
        for row in model_rows:
            _persist_shadow_run(conn, row, model_name=model_name, feature_set_name=feature_set_name, report_path=report_path)
        report["report_path"] = str(report_path)
        return report
    finally:
        conn.close()


def _run_horizon_shadow(
    conn,
    *,
    base_config: dict,
    model_name: str,
    feature_set_name: str,
    horizon: str,
    candidate_features: list[str],
    signal_days: int,
    shadow: dict,
) -> dict:
    minimum = int(shadow.get("min_mature_dates", 60))
    horizon_model_name = f"{model_name}-{horizon}"
    dataset = load_modeling_dataset(conn, horizon=horizon)
    coverage = pd.to_numeric(dataset.get("llm_news_coverage_score"), errors="coerce").fillna(0.0) > 0 if not dataset.empty else pd.Series(dtype=bool)
    mature_dates = int(pd.to_datetime(dataset.loc[coverage, "date"], errors="coerce").dt.date.nunique()) if not dataset.empty else 0
    initial = {
        "horizon": horizon,
        "signal_days": signal_days,
        "mature_prediction_dates": mature_dates,
        "model_name": horizon_model_name,
        "gate_status": "not_ready",
        "production_weight": 0.0,
        "baseline_metrics": {},
        "candidate_metrics": {},
        "fail_reason": None,
    }
    if signal_days < minimum or mature_dates < minimum:
        initial["fail_reason"] = f"requires {minimum} LLM signal and mature prediction dates"
        register_model(
            conn,
            model_name=horizon_model_name,
            model_type="lightgbm_shadow",
            feature_set_name=feature_set_name,
            label_name="excess_return,is_top20pct",
            horizons=[horizon],
            status="experimental",
            production_weight=0.0,
            shadow_mode=True,
            metrics={"signal_days": signal_days, "mature_prediction_dates": mature_dates},
            fail_reason=initial["fail_reason"],
        )
        return initial

    horizon_days = get_horizons(base_config).get(horizon)
    if horizon_days is None:
        initial["fail_reason"] = f"unknown horizon {horizon}"
        return initial
    base_features = get_feature_columns(base_config)
    model_config = dict(base_config.get("model") or {})
    baseline_result = train_horizon_model(dataset, horizon, base_features, model_config, horizon_days)
    candidate_result = train_horizon_model(dataset, horizon, candidate_features, model_config, horizon_days)
    baseline_predictions = _oos_predictions(baseline_result)
    candidate_predictions = _oos_predictions(candidate_result)
    eligible_dates = set(pd.to_datetime(dataset.loc[coverage, "date"], errors="coerce").dt.date.dropna())
    baseline_predictions = baseline_predictions[baseline_predictions["asof_date"].isin(eligible_dates)]
    candidate_predictions = candidate_predictions[candidate_predictions["asof_date"].isin(eligible_dates)]
    labels = load_labels(conn, horizon=horizon)
    baseline_scored = attach_forward_returns(baseline_predictions, labels)
    candidate_scored = attach_forward_returns(candidate_predictions, labels)
    top_k = int(shadow.get("top_k", 20))
    _, baseline_backtest = run_topk_backtest(
        baseline_scored,
        horizon,
        top_k,
        transaction_cost_bps=float(base_config.get("backtest", {}).get("transaction_cost_bps", 30)),
        rebalance_frequency=str(base_config.get("backtest", {}).get("rebalance_frequency", "M")),
    )
    _, candidate_backtest = run_topk_backtest(
        candidate_scored,
        horizon,
        top_k,
        transaction_cost_bps=float(base_config.get("backtest", {}).get("transaction_cost_bps", 30)),
        rebalance_frequency=str(base_config.get("backtest", {}).get("rebalance_frequency", "M")),
    )
    baseline_metrics = {**baseline_result.metrics, **baseline_backtest}
    candidate_metrics = {**candidate_result.metrics, **candidate_backtest}
    gate_status, fail_reason = evaluate_shadow_gate(candidate_metrics, baseline_metrics, shadow)
    register_model(
        conn,
        model_name=horizon_model_name,
        model_type="lightgbm_shadow",
        feature_set_name=feature_set_name,
        label_name="excess_return,is_top20pct",
        horizons=[horizon],
        status="experimental" if gate_status == "not_ready" else gate_status,
        production_weight=0.0,
        shadow_mode=True,
        metrics={"baseline": baseline_metrics, "candidate": candidate_metrics},
        fail_reason=fail_reason,
    )
    record_backtest_run(
        conn,
        model_name=horizon_model_name,
        baseline_model_name=f"domestic_only_{horizon}",
        horizon=horizon,
        metrics={"baseline": baseline_metrics, "candidate": candidate_metrics, **candidate_backtest},
        accepted=gate_status == "accepted_shadow",
        fail_reason=fail_reason or "shadow-only gate not promoted",
        top_k=top_k,
        start_date=candidate_predictions["asof_date"].min() if not candidate_predictions.empty else None,
        end_date=candidate_predictions["asof_date"].max() if not candidate_predictions.empty else None,
    )
    _persist_shadow_predictions(conn, candidate_predictions, horizon_model_name)
    return {
        **initial,
        "gate_status": gate_status,
        "baseline_metrics": baseline_metrics,
        "candidate_metrics": candidate_metrics,
        "fail_reason": fail_reason,
    }


def _oos_predictions(result) -> pd.DataFrame:
    return result.walk_forward_predictions.copy() if not result.walk_forward_predictions.empty else result.validation_predictions.copy()


def _persist_shadow_predictions(conn, predictions: pd.DataFrame, model_name: str) -> None:
    if predictions.empty:
        return
    frame = predictions.copy()
    frame["date"] = pd.to_datetime(frame["asof_date"], errors="coerce").dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["model_name"] = model_name
    frame["model_version"] = frame.get("model_version")
    frame["pred_score"] = frame["pred_return"]
    frame["pred_prob"] = frame["pred_prob_top20"]
    frame["risk_score"] = frame["pred_risk"]
    frame["recommendation_score"] = frame["long_score"]
    frame["rank"] = frame.groupby(["date", "horizon"])["pred_prob"].rank(ascending=False, method="first").astype(int)
    frame["created_at"] = _utcnow()
    append_dedup_table(
        conn,
        "model_predictions",
        frame[
            [
                "date",
                "symbol",
                "model_name",
                "model_version",
                "horizon",
                "pred_score",
                "pred_prob",
                "risk_score",
                "recommendation_score",
                "rank",
                "created_at",
            ]
        ],
        ["date", "symbol", "model_name", "horizon"],
    )


def _persist_shadow_run(conn, row: dict, *, model_name: str, feature_set_name: str, report_path: Path) -> None:
    frame = pd.DataFrame(
        [
            {
                "run_id": uuid4().hex,
                "model_name": row["model_name"],
                "feature_set_name": feature_set_name,
                "horizon": row["horizon"],
                "signal_days": int(row["signal_days"]),
                "mature_prediction_dates": int(row["mature_prediction_dates"]),
                "gate_status": row["gate_status"],
                "production_weight": 0.0,
                "baseline_metrics_json": json.dumps(row["baseline_metrics"], ensure_ascii=False, default=str),
                "candidate_metrics_json": json.dumps(row["candidate_metrics"], ensure_ascii=False, default=str),
                "fail_reason": row["fail_reason"],
                "report_path": str(report_path),
                "created_at": _utcnow(),
            }
        ]
    )
    append_dedup_table(conn, "marketnewsfeed_llm_shadow_runs", frame, ["run_id"])


def _resolve_base_config(config: dict, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    root = Path(config["_root"])
    return root / candidate


def _report_markdown(report: dict) -> str:
    lines = [
        "# MarketNewsFeed Local LLM Shadow",
        "",
        f"- Signal days: {report['signal_days']}",
        "- Production weight: 0.0",
        "",
        "## Stock A/B Gate",
        "",
    ]
    for row in report["stock_shadow"]:
        lines.append(f"- {row['horizon']}: {row['gate_status']} ({row.get('fail_reason') or 'no failure'})")
    lines.extend(["", "## Market Range Shadow", ""])
    for row in report["market_range_shadow"]:
        lines.append(f"- {row['market']}: {row['status']} ({row.get('reason') or 'coverage ready'})")
    lines.extend(["", report["disclaimer"], ""])
    return "\n".join(lines)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


if __name__ == "__main__":
    main()

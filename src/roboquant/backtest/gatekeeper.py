from __future__ import annotations

from typing import Any

DEFAULT_GATE_CONFIG = {
    "min_excess_gain": {"3M": 0.01, "6M": 0.015},
    "min_hit_ratio_gain": 0.03,
    "max_mdd_worsening": 0.02,
    "max_turnover_ratio": 1.30,
    "min_transaction_cost_adjusted_gain": 0.0,
    "accepted_production_weight": 0.05,
    "rejected_production_weight": 0.0,
}


def evaluate_gate(
    candidate_metrics: dict[str, Any],
    baseline_metrics: dict[str, Any],
    config: dict[str, Any] | None = None,
    horizon: str = "3M",
) -> tuple[bool, str, dict[str, float]]:
    gate = {**DEFAULT_GATE_CONFIG, **(config or {})}
    min_excess_gain = gate.get("min_excess_gain", {})
    if isinstance(min_excess_gain, dict):
        min_excess = float(min_excess_gain.get(horizon, min_excess_gain.get("default", 0.0)))
    else:
        min_excess = float(min_excess_gain)

    candidate_excess = _number(candidate_metrics, "avg_excess_return")
    baseline_excess = _number(baseline_metrics, "avg_excess_return")
    excess_gain = candidate_excess - baseline_excess

    candidate_hit = _number(candidate_metrics, "hit_ratio")
    baseline_hit = _number(baseline_metrics, "hit_ratio")
    hit_gain = candidate_hit - baseline_hit

    candidate_mdd = _number(candidate_metrics, "mdd")
    baseline_mdd = _number(baseline_metrics, "mdd")
    mdd_worsening = baseline_mdd - candidate_mdd

    candidate_turnover = _number(candidate_metrics, "avg_turnover")
    baseline_turnover = max(_number(baseline_metrics, "avg_turnover"), 1e-6)
    turnover_ratio = candidate_turnover / baseline_turnover

    candidate_tc = _number(candidate_metrics, "transaction_cost_adjusted_return", candidate_excess)
    baseline_tc = _number(baseline_metrics, "transaction_cost_adjusted_return", baseline_excess)
    transaction_cost_adjusted_gain = candidate_tc - baseline_tc

    decision_metrics = {
        "excess_gain": excess_gain,
        "hit_gain": hit_gain,
        "mdd_worsening": mdd_worsening,
        "turnover_ratio": turnover_ratio,
        "transaction_cost_adjusted_gain": transaction_cost_adjusted_gain,
        "candidate_excess_return": candidate_excess,
        "baseline_excess_return": baseline_excess,
    }

    if excess_gain < min_excess:
        return False, f"{horizon} excess return gain is below threshold", decision_metrics
    if hit_gain < float(gate.get("min_hit_ratio_gain", 0.03)):
        return False, "Hit ratio did not improve enough", decision_metrics
    if mdd_worsening > float(gate.get("max_mdd_worsening", 0.02)):
        return False, "MDD became worse than allowed threshold", decision_metrics
    if turnover_ratio > float(gate.get("max_turnover_ratio", 1.30)):
        return False, "Turnover increased too much", decision_metrics
    if transaction_cost_adjusted_gain < float(gate.get("min_transaction_cost_adjusted_gain", 0.0)):
        return False, "Transaction-cost adjusted return did not improve", decision_metrics
    return True, "accepted", decision_metrics


def production_weight_for_decision(accepted: bool, config: dict[str, Any] | None = None) -> float:
    gate = {**DEFAULT_GATE_CONFIG, **(config or {})}
    key = "accepted_production_weight" if accepted else "rejected_production_weight"
    return float(gate.get(key, 0.05 if accepted else 0.0))


def _number(metrics: dict[str, Any], key: str, default: float = 0.0) -> float:
    value = metrics.get(key, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

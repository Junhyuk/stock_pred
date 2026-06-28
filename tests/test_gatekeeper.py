from __future__ import annotations

from roboquant.backtest.gatekeeper import evaluate_gate, production_weight_for_decision

BASELINE = {
    "avg_excess_return": 0.02,
    "hit_ratio": 0.50,
    "mdd": -0.08,
    "avg_turnover": 0.40,
    "transaction_cost_adjusted_return": 0.03,
}


def test_gatekeeper_accepts_model_that_beats_all_thresholds() -> None:
    candidate = {
        "avg_excess_return": 0.04,
        "hit_ratio": 0.55,
        "mdd": -0.09,
        "avg_turnover": 0.45,
        "transaction_cost_adjusted_return": 0.05,
    }

    accepted, reason, metrics = evaluate_gate(candidate, BASELINE, horizon="3M")

    assert accepted
    assert reason == "accepted"
    assert metrics["excess_gain"] >= 0.01
    assert production_weight_for_decision(True) == 0.05


def test_gatekeeper_rejects_low_excess_gain() -> None:
    candidate = {
        "avg_excess_return": 0.025,
        "hit_ratio": 0.55,
        "mdd": -0.08,
        "avg_turnover": 0.40,
        "transaction_cost_adjusted_return": 0.04,
    }

    accepted, reason, _ = evaluate_gate(candidate, BASELINE, horizon="3M")

    assert not accepted
    assert "excess return gain" in reason
    assert production_weight_for_decision(False) == 0.0


def test_gatekeeper_rejects_mdd_worsening_and_turnover() -> None:
    candidate = {
        "avg_excess_return": 0.05,
        "hit_ratio": 0.56,
        "mdd": -0.12,
        "avg_turnover": 0.80,
        "transaction_cost_adjusted_return": 0.06,
    }

    accepted, reason, metrics = evaluate_gate(candidate, BASELINE, horizon="3M")

    assert not accepted
    assert "MDD" in reason
    assert metrics["mdd_worsening"] > 0.02

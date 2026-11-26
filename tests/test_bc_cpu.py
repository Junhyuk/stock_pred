from datetime import date, timedelta

import numpy as np
import pytest

from roboquant.index_forecast.bc_close_distribution import c_close_estimate
from roboquant.index_forecast.bc_cpu import forecast_market


def test_model_c_close_is_the_probability_weighted_mean_and_central_range():
    estimate = c_close_estimate(1000.0, [-0.06, -0.04, -0.02, 0.01, 0.05], (0.2, 0.5, 0.3))
    assert estimate["expected_return"] == pytest.approx(0.0025)
    assert estimate["expected_index"] == pytest.approx(1002.5)
    assert estimate["interval"] == pytest.approx({"low": 940.0, "high": 1050.0, "level": 0.8})


def test_model_c_close_requires_realized_returns_for_every_class():
    assert c_close_estimate(1000.0, [-0.02, 0.01], (0.2, 0.5, 0.3)) == {}


def test_model_b_carries_close_and_counts_only_supported_direction_labels():
    closes = [1000.0] * 400
    closes[10] = None

    result = forecast_market("KOSPI", closes, run_c=False)

    assert result["c"] is None
    assert result["b"] == {
        "close": 1000.0,
        "p_up": 1 / 370,
        "p_down": 1 - 1 / 370,
        "expected_return": 0.0,
        "expected_index": 1000.0,
        "interval": {"low": 1000.0, "high": 1000.0, "level": 0.8},
    }


def test_recent_missing_close_abstains_instead_of_bridging_the_gap():
    closes = [1000.0] * 400
    closes[-4] = None

    with pytest.raises(ValueError, match="21 consecutive closes"):
        forecast_market("KOSPI", closes, run_c=False)


@pytest.mark.parametrize(
    "market,method,extreme,up_given_extreme",
    [
        ("KOSPI", "hist_gradient", 0.3842798712343482, 0.4942782992298437),
        ("KOSDAQ", "regime_prior", 0.41393250110315577, 0.49382716049382713),
    ],
)
def test_model_c_matches_the_registered_three_way_recipe(market, method, extreme, up_given_extreme):
    daily_returns = np.random.default_rng(42).normal(0.001, 0.035, size=450)
    closes = 1000 * np.cumprod(1 + daily_returns)
    sessions = [(date(2019, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(closes))]

    result = forecast_market(market, closes, run_c=True, sessions=sessions)

    assert result["b"]["interval"]["low"] < closes[-1] < result["b"]["interval"]["high"]
    c = result["c"]
    assert c["method"] == method
    assert c["close"] == closes[-1]
    assert c["p_extreme"] == pytest.approx(extreme, abs=1e-7)
    assert c["p_up_given_extreme"] == pytest.approx(up_given_extreme, abs=1e-7)
    assert c["p_down"] + c["p_normal"] + c["p_up"] == pytest.approx(1)
    assert c["p_down"] + c["p_up"] == pytest.approx(c["p_extreme"])


def test_rejects_a_live_anchor_that_arrived_after_the_requested_cutoff():
    closes = [1000.0] * 400
    sessions = [(date(2019, 1, 1) + timedelta(days=i)).isoformat() for i in range(len(closes))]
    anchor = sessions[-1]

    with pytest.raises(ValueError, match="anchor receipt is after the forecast cutoff"):
        forecast_market(
            "KOSPI",
            closes,
            run_c=False,
            sessions=sessions,
            close_received_at={anchor: f"{anchor}T18:05:00+09:00"},
            prediction_cutoff=f"{anchor}T18:00:00+09:00",
        )

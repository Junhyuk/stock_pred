import numpy as np
import pytest

from roboquant.models import bc_chrono_eval as ev


def test_brier_log_loss_and_hit_match_hand_values():
    p = np.array([0.5, 0.8, 0.2])
    y = np.array([1.0, 1.0, 1.0])
    assert ev.brier(p, y).tolist() == pytest.approx([0.25, 0.04, 0.64])
    assert ev.log_loss(np.array([0.5]), np.array([1.0]))[0] == pytest.approx(np.log(2.0))
    assert ev.hit(p, y).tolist() == [1.0, 1.0, 0.0]
    # Clipping keeps a confident miss finite.
    assert np.isfinite(ev.log_loss(np.array([0.0]), np.array([1.0]))[0])


def test_ece_is_zero_for_calibrated_bins_and_positive_otherwise():
    p = np.array([0.1] * 10 + [0.9] * 10)
    y = np.array([1] + [0] * 9 + [1] * 9 + [0])
    assert ev.ece(p, y, 5) == pytest.approx(0.0)
    assert ev.ece(np.full(4, 0.9), np.zeros(4), 5) == pytest.approx(0.9)


def test_expanding_event_rate_uses_only_earlier_events():
    y = np.array([1, 0, 1, 1, 0], dtype=float)
    event = np.array([True, False, True, False, True])
    rate = ev.expanding_event_up_rate(y, event)
    # position 0 sees nothing; position 1 sees one up event; position 3 sees two up events.
    assert rate.tolist() == pytest.approx([0.5, 2 / 3, 2 / 3, 3 / 4, 3 / 4])
    # Changing a label never changes the rate at its own position or earlier.
    changed = y.copy()
    changed[2] = 0.0
    assert ev.expanding_event_up_rate(changed, event)[:3].tolist() == pytest.approx(rate[:3].tolist())


def test_previous_event_direction_lags_by_one_event():
    y = np.array([1, 0, 0, 1], dtype=float)
    event = np.array([True, False, True, False])
    assert ev.previous_event_direction(y, event).tolist() == [0.5, 1.0, 1.0, 0.0]


def test_abs_weights_are_normalised_on_training_rows_only():
    r = np.array([0.01, -0.03, 0.02, 0.5])
    w = ev.abs_return_weights(r, np.arange(3))
    assert w.mean() == pytest.approx(1.0)
    assert len(w) == 3


def test_conditional_expected_return_uses_earlier_event_magnitudes():
    r = np.array([0.04, -0.01, -0.06, 0.05])
    event = np.abs(r) >= 0.03
    out = ev.conditional_expected_return(np.array([0.9, 0.5, 0.75, 0.0]), r, event)
    assert np.isnan(out[0])
    assert out[1] == pytest.approx(0.0)
    assert out[2] == pytest.approx(0.5 * 0.04)
    assert out[3] == pytest.approx(-1.0 * 0.05)


def test_block_bootstrap_detects_a_clear_improvement_and_not_noise():
    rng = np.random.default_rng(0)
    better = -0.05 + 0.01 * rng.standard_normal(200)
    noise = 0.05 * rng.standard_normal(200)
    clear = ev.block_bootstrap(better, block=10, draws=500, seed=42)
    vague = ev.block_bootstrap(noise, block=10, draws=500, seed=42)
    assert clear["high"] < 0 and clear["p_one_sided"] < 0.01
    assert vague["low"] < 0 < vague["high"]
    assert clear["mde_80_power"] == pytest.approx(2.8 * clear["se"])
    again = ev.block_bootstrap(better, block=10, draws=500, seed=42)
    assert again == clear


def test_holm_adjustment_is_monotone_and_capped():
    adjusted = ev.holm({"a": 0.01, "b": 0.04, "c": 0.03, "d": 0.5})
    assert adjusted["a"] == pytest.approx(0.04)
    assert adjusted["c"] == pytest.approx(0.09)
    assert adjusted["b"] == pytest.approx(0.09)
    assert adjusted["d"] == pytest.approx(0.5)
    assert ev.holm({"x": 0.9, "y": 0.8})["x"] == 1.0

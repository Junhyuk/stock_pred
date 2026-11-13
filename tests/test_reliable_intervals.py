"""Hand-computed interval evidence and temporal scaling regression tests."""
import importlib

import numpy as np
import pytest

from roboquant.index_forecast.reliable import _simple_metrics


def engine():
    spec = importlib.util.find_spec('roboquant.index_forecast.reliable_intervals')
    assert spec is not None, 'volatility interval shadow is missing'
    return importlib.import_module('roboquant.index_forecast.reliable_intervals')


def records():
    return [{'asof_date': f'2026-10-{i + 1:02d}',
             'label_available_at': f'2026-10-{i + 2:02d}T07:00:00+00:00',
             'target_close_at': f'2026-10-{i + 2:02d}T06:30:00+00:00',
             'actual_return': (i + 1) / 1000, 'expected_return': 0., 'scale_at_prediction': .01,
             'baselines': {'mean': {'expected_return': .001}}} for i in range(10)]


def test_scaled_interval_uses_finite_sample_prior_normalized_residual_quantile():
    result = engine().normalized_interval_shadows(records(), cutoff='2026-11-01T00:00:00Z',
        predicted=.002, scale=.02, mean_prediction=.003, window=252, min_rows=10)
    candidate = result['volatility_normalized']
    assert candidate['status'] == 'ready'
    assert candidate['calibration_n'] == 10
    # ceil((10 + 1) * .8) = 9; normalized score .9, current radius .018.
    assert candidate['range_low'] == pytest.approx(-.016)
    assert candidate['range_high'] == pytest.approx(.020)
    baseline = result['mean_volatility_normalized']
    assert baseline['range_low'] == pytest.approx(-.013)
    assert baseline['range_high'] == pytest.approx(.019)
    assert candidate['production_weight'] == 0


def test_future_or_unmatured_residual_cannot_change_interval():
    history = records()
    kwargs = dict(cutoff='2026-11-01T00:00:00Z', predicted=0., scale=.02,
                  mean_prediction=.001, window=252, min_rows=10)
    before = engine().normalized_interval_shadows(history, **kwargs)
    future = {**history[-1], 'actual_return': 999., 'label_available_at': '2030-01-01T00:00:00Z'}
    assert engine().normalized_interval_shadows([*history, future], **kwargs) == before
    history[-1]['target_close_at'] = '2030-01-01T00:00:00Z'
    assert engine().normalized_interval_shadows(history, **kwargs)['volatility_normalized']['status'] == 'not_ready'


@pytest.mark.parametrize('scale', [None, 0, -1, float('nan')])
def test_invalid_current_scale_does_not_invent_an_interval(scale):
    result = engine().normalized_interval_shadows(records(), cutoff='2026-11-01T00:00:00Z',
        predicted=0., scale=scale, mean_prediction=.001, window=252, min_rows=10)
    assert result['volatility_normalized']['status'] == 'not_ready'
    assert result['volatility_normalized']['range_low'] is None


def test_interval_score_penalizes_both_missed_tails_in_return_units():
    metrics = _simple_metrics(np.array([.02, -.01]), np.zeros(2), np.full(2, .5),
                             np.array([-.01, -.005]), np.array([.01, .005]))
    assert metrics.get('interval_score80') == pytest.approx(.09)
    assert metrics.get('upper_breach') == .5
    assert metrics['lower_breach'] == .5


def test_probability_half_tie_predicts_up_while_zero_return_predicts_nonpositive():
    metrics = _simple_metrics(np.array([.01]), np.array([0.]), np.array([.5]))
    assert metrics['probability_direction_hit'] == 1.
    assert metrics['return_direction_hit'] == 0.

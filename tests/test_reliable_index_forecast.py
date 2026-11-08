"""Adversarial temporal checks for research-only index forecasts."""
import importlib
import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from roboquant.index_forecast.dataset import PRICE_FEATURES


def engine():
    spec = importlib.util.find_spec('roboquant.index_forecast.reliable')
    assert spec is not None, 'reliable chronological engine is missing'
    return importlib.import_module('roboquant.index_forecast.reliable')


def fixture(n=150):
    rng = np.random.default_rng(17)
    dates = pd.bdate_range('2020-01-02', periods=n + 1)
    x = rng.normal(size=(n, 7))
    frame = pd.DataFrame(x, columns=PRICE_FEATURES)
    frame['volatility_20d'] = abs(frame['volatility_20d']) * .01
    frame['label_return'] = .007 * x[:, 0] + rng.normal(scale=.009, size=n)
    frame['asof_date'] = dates[:-1]
    frame['target_date'] = dates[1:]
    return frame


def settings(**kwargs):
    return engine().ReliableSettings(initial_train_days=35, purge_days=1, retrain_step_days=15,
        calibration_window=30, min_calibration_rows=10, development_days=25, test_days=25,
        bootstrap_samples=80, bootstrap_block_days=5, min_oos_rows=15,
        model_families=('linear',), **kwargs)


def test_chronological_predictions_and_selection_ignore_terminal_outcomes():
    mod = engine()
    frame = fixture()
    before = frame.copy(deep=True)
    result = mod.evaluate_reliable_forecasts(frame, settings=settings())
    pd.testing.assert_frame_equal(frame, before)
    altered = frame.copy()
    altered.loc[altered.index[-25:], 'label_return'] *= -50
    other = mod.evaluate_reliable_forecasts(altered, settings=settings())
    assert result['selected'] == other['selected']
    assert result['candidate_reports'] == other['candidate_reports']
    first, changed = result['observations'][0], other['observations'][0]
    for key in ('expected_return', 'p_up', 'range_low', 'range_high', 'calibration_n'):
        assert first[key] == changed[key]
    assert result['production_weight'] == 0
    assert result['status'] in {'rejected', 'not_ready', 'accepted_shadow'}
    json.dumps(result, allow_nan=False)


def test_calibration_is_previous_out_of_sample_only():
    mod = engine()
    result = mod.evaluate_reliable_forecasts(fixture(100), settings=settings())
    records = result['candidate_observations']['linear:price']
    assert records[0]['calibration_n'] == 0
    assert records[0]['range_low'] is None
    assert records[9]['range_low'] is None
    assert records[10]['calibration_n'] == 10
    assert records[10]['range_low'] <= records[10]['expected_return'] <= records[10]['range_high']
    for row in records:
        assert 0 <= row['p_up'] <= 1
        assert row['p_up'] + row['p_down'] + row['p_flat'] == pytest.approx(1)
        assert row['calibration_n'] <= 30
    row = records[10]
    absolute_errors = sorted(abs(r['actual_return'] - r['expected_return']) for r in records[:10])
    # ceil((10+1)*.8) = ninth order statistic, without interpolation/clipping.
    assert row['range_high'] - row['expected_return'] == pytest.approx(absolute_errors[8])


def test_current_label_and_delayed_labels_cannot_enter_fit_or_calibration():
    mod = engine()
    frame = fixture(110)
    date = frame.loc[60, 'asof_date']
    a = mod.evaluate_reliable_forecasts(frame, settings=settings())
    changed = frame.copy()
    changed.loc[60:, 'label_return'] = 5
    b = mod.evaluate_reliable_forecasts(changed, settings=settings())
    for x, y in zip(a['candidate_observations']['linear:price'], b['candidate_observations']['linear:price'], strict=True):
        if x['asof_date'] <= str(date.date()):
            assert {k: x[k] for k in ('expected_return', 'raw_p_up', 'range_low', 'p_up')} == {
                k: y[k] for k in ('expected_return', 'raw_p_up', 'range_low', 'p_up')}
    frame['label_available_at'] = frame['target_date']
    frame.loc[:30, 'label_available_at'] = '2030-01-01'
    late_changed = frame.copy()
    late_changed.loc[:30, 'label_return'] = 100
    c = mod.evaluate_reliable_forecasts(frame, settings=settings())
    d = mod.evaluate_reliable_forecasts(late_changed, settings=settings())
    assert c['candidate_observations'] == d['candidate_observations']


def test_missing_short_and_single_class_fail_closed():
    mod = engine()
    for frame in (fixture(20), fixture().drop(columns=PRICE_FEATURES[0])):
        result = mod.evaluate_reliable_forecasts(frame, settings=settings())
        assert result['status'] == 'not_ready'
    frame = fixture()
    frame['label_return'] = .01
    result = mod.evaluate_reliable_forecasts(frame, settings=settings())
    assert result['status'] == 'not_ready'
    assert not any(row['calibration_ready'] for row in result['candidate_observations']['linear:price'])


def test_live_ignores_live_and_unmatured_labels_and_fails_without_oof():
    mod = engine()
    frame = fixture()
    config = settings()
    evaluation = mod.evaluate_reliable_forecasts(frame.iloc[:-1], settings=config)
    latest = frame.iloc[[-1]].copy()
    first = mod.forecast_reliable_latest(frame, latest, evaluation, settings=config)
    latest['label_return'] = 999
    history = frame.copy()
    history.loc[history.index[-1], 'label_return'] = -999
    second = mod.forecast_reliable_latest(history, latest, evaluation, settings=config)
    assert first == second
    assert first['p_up'] + first['p_down'] == pytest.approx(1)
    assert first['status'] in {'research_candidate', 'rejected'}
    empty_oof = {**evaluation, 'calibration_observations': []}
    no_range = mod.forecast_reliable_latest(history, latest, empty_oof, settings=config)
    assert no_range['status'] == 'not_ready'
    assert no_range['range_low'] is None
    assert mod.forecast_reliable_latest(history, latest, {}, settings=config)['status'] == 'not_ready'


def test_tree_ensemble_and_repeatable_bootstrap_paths():
    mod = engine()
    config = replace(settings(), model_families=('linear', 'lightgbm', 'ensemble'), retrain_step_days=40)
    first = mod.evaluate_reliable_forecasts(fixture(130), settings=config)
    second = mod.evaluate_reliable_forecasts(fixture(130), settings=config)
    assert first == second
    assert {r['family'] for r in first['candidate_reports']} == {'linear', 'lightgbm', 'ensemble'}
    records = first['candidate_observations']
    for a, b, c in zip(records['linear:price'], records['lightgbm:price'], records['ensemble:price'], strict=True):
        assert c['expected_return'] == pytest.approx((a['expected_return'] + b['expected_return']) / 2)
        assert c['raw_p_up'] == pytest.approx((a['raw_p_up'] + b['raw_p_up']) / 2)
    assert 'brier_difference_ci95' in first['candidate_reports'][0]['development_metrics']


def test_explicit_midnight_cutoff_does_not_mature_afternoon_labels():
    mod = engine()
    frame = fixture(110)
    # Cutoffs are explicit timestamps, so midnight must stay midnight. Changing
    # labels published at 10:00 that morning cannot affect that day's forecast.
    frame['prediction_cutoff'] = frame['asof_date'].dt.strftime('%Y-%m-%dT00:00:00')
    frame['label_available_at'] = frame['target_date'].dt.strftime('%Y-%m-%dT10:00:00+09:00')
    first = mod.evaluate_reliable_forecasts(frame, settings=settings())
    changed = frame.copy()
    changed.loc[59:, 'label_return'] = .9
    second = mod.evaluate_reliable_forecasts(changed, settings=settings())
    cutoff_date = frame.loc[60, 'asof_date'].date().isoformat()
    a = next(r for r in first['candidate_observations']['linear:price'] if r['asof_date'] == cutoff_date)
    b = next(r for r in second['candidate_observations']['linear:price'] if r['asof_date'] == cutoff_date)
    assert a['prediction_cutoff'].endswith('15:00:00+00:00')
    for key in ('p_up', 'range_low', 'calibration_n'):
        assert a[key] == b[key]


def test_unavailable_terminal_outcomes_are_not_scored_or_used_as_live_calibration():
    mod = engine()
    frame = fixture(120)
    frame['label_available_at'] = frame['target_date'].astype(str)
    frame.loc[frame.index[-20:], 'label_available_at'] = '2030-01-01T15:30:00+09:00'
    report = mod.evaluate_reliable_forecasts(frame, settings=settings())
    assert report['metrics']['n'] <= 5
    assert report['status'] == 'not_ready'
    assert all(r['label_available_at'] <= report['evaluated_through'] for r in report['calibration_observations'])


def test_explicit_reporting_cutoff_excludes_future_prediction_rows_before_selection():
    mod = engine()
    frame = fixture(150)
    cutoff = str(frame.loc[109, 'asof_date'].date()) + 'T15:30:00+09:00'
    prefix = mod.evaluate_reliable_forecasts(frame.iloc[:110], settings=settings(), reporting_cutoff=cutoff)
    future = mod.evaluate_reliable_forecasts(frame, settings=settings(), reporting_cutoff=cutoff)
    assert prefix['selection_frozen_before'] == future['selection_frozen_before']
    assert prefix['selected'] == future['selected']
    assert prefix['candidate_reports'] == future['candidate_reports']
    assert prefix['metrics'] == future['metrics']


def test_auditor_references_are_aligned_and_descriptive_not_trading_profits():
    result = engine().evaluate_reliable_forecasts(fixture(), settings=settings())
    references = result['metrics']['reference_baselines']
    assert set(references) == {'previous_direction', 'simple_trend', 'buy_hold', 'cash'}
    assert all(row['n'] == result['metrics']['n'] for row in references.values())
    assert references['cash']['hypothetical_exposure_return'] == 0
    assert references['buy_hold']['role'] == 'descriptive_index_exposure_not_execution_pnl'
    assert references['previous_direction']['probability_method'] == 'hard_direction_reference_uncalibrated'
    assert 'execution_pnl' not in references['buy_hold']

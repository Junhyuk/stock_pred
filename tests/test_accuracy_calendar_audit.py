"""Independent adversarial checks for the calendar-aligned fit protocol."""
import json
from dataclasses import asdict

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest

from roboquant.index_forecast import reliable
from roboquant.index_forecast.dataset import PRICE_FEATURES


def _frame():
    sessions = xcals.get_calendar('XNYS').sessions_in_range('2019-01-02', '2019-08-01')
    rng = np.random.default_rng(933)
    frame = pd.DataFrame(rng.normal(0, .01, (len(sessions) - 1, len(PRICE_FEATURES))),
                         columns=PRICE_FEATURES)
    frame['volatility_20d'] = .01
    frame['asof_date'] = sessions[:-1].strftime('%Y-%m-%d')
    frame['target_date'] = sessions[1:].strftime('%Y-%m-%d')
    frame['prediction_cutoff'] = frame.asof_date + 'T22:00:00+00:00'
    frame['target_close_at'] = frame.target_date + 'T21:00:00+00:00'
    frame['label_available_at'] = frame.target_close_at
    frame['label_return'] = rng.normal(0, .01, len(frame))
    return frame


def _settings():
    return reliable.ReliableSettings(initial_train_days=25, purge_days=1, retrain_step_days=21,
        calibration_window=30, min_calibration_rows=10, development_days=25, test_days=25,
        bootstrap_samples=20, bootstrap_block_days=5, min_oos_rows=15,
        model_families=('linear',), calendar_name='XNYS')


def _anchor():
    from roboquant.index_forecast.fit_protocol import session_anchor
    return session_anchor('2019-05-20', 'XNYS', '2019-01-02', 21)


def _fit(frame, anchor):
    from roboquant.index_forecast.fit_protocol import make_fit_snapshot, restore_fit_snapshot
    settings = asdict(_settings())
    snap = make_fit_snapshot(reliable._prepare(frame), PRICE_FEATURES, anchor, settings)
    assert snap['status'] == 'ready'
    return snap, restore_fit_snapshot(snap, PRICE_FEATURES, settings)


def test_purge_excludes_scheduled_session_not_most_recent_available_row():
    frame, anchor = _frame(), _anchor()
    sessions = frame.asof_date.tolist()
    position = sessions.index(anchor['anchor_asof'])
    missing, eligible = sessions[position - 1], sessions[position - 2]
    frame = frame.loc[frame.asof_date != missing]
    _, training = _fit(frame, anchor)
    assert eligible in training['_asof_label'].tolist()
    assert missing not in training['_asof_label'].tolist()


def test_unmatured_target_mutation_cannot_change_anchor_training():
    frame, anchor = _frame(), _anchor()
    old = 30
    frame.loc[old, 'label_available_at'] = '2030-01-01T00:00:00+00:00'
    first_snap, first = _fit(frame, anchor)
    altered = frame.copy(deep=True)
    altered.loc[old, 'label_return'] = 999
    altered.loc[altered.asof_date >= anchor['anchor_asof'], 'label_return'] = -999
    second_snap, second = _fit(altered, anchor)
    assert frame.loc[old, 'asof_date'] not in first['_asof_label'].tolist()
    pd.testing.assert_frame_equal(first, second)
    assert first_snap['status'] == second_snap['status']


def test_target_close_after_anchor_is_ineligible_even_if_release_claims_earlier():
    frame, anchor = _frame(), _anchor()
    old = 30
    frame.loc[old, 'label_available_at'] = '2019-01-02T00:00:00+00:00'
    frame.loc[old, 'target_close_at'] = '2030-01-01T00:00:00+00:00'
    _, training = _fit(frame, anchor)
    assert frame.loc[old, 'asof_date'] not in training['_asof_label'].tolist()


def test_walk_and_latest_match_on_both_sides_of_exchange_anchor():
    from roboquant.index_forecast.fit_protocol import session_anchor

    frame, config = _frame(), _settings()
    anchors = [session_anchor(day, 'XNYS', '2019-01-02', 21)['anchor_asof']
               for day in frame.asof_date]
    boundaries = [i for i in range(70, len(frame) - 2) if anchors[i] != anchors[i - 1]]
    assert boundaries, 'fixture must span a populated 21-session refit boundary'
    boundary = boundaries[-1]
    for position in (boundary - 1, boundary, boundary + 1):
        # The historical walk produces a forecast at this row before its target
        # matures; the live request itself carries no current target label.
        prefix = frame.iloc[:position + 1].copy()
        evaluation = reliable.evaluate_reliable_forecasts(prefix, settings=config)
        record = evaluation['candidate_observations']['linear:price'][-1]
        current = prefix.iloc[[-1]].copy()
        current['label_return'] = np.nan
        latest = reliable.forecast_reliable_latest(prefix, current, evaluation, settings=config)
        assert record['asof_date'] == frame.iloc[position].asof_date
        assert latest['fit_asof'] == record['fit_asof'] == anchors[position]
        assert latest['fit_snapshot_hash'] == record['fit_snapshot_hash']
        for key in ('expected_return', 'raw_p_up', 'p_up', 'range_low', 'range_high'):
            assert latest[key] == pytest.approx(record[key], abs=1e-12)
        assert latest['calibration_n'] == record['calibration_n']


@pytest.mark.parametrize(('before', 'holiday', 'after'), [
    ('2026-06-02', '2026-06-03', '2026-06-04'),
    ('2026-07-16', '2026-07-17', '2026-07-20'),
])
def test_krx_official_holidays_do_not_shift_fit_ordinal(before, holiday, after):
    from roboquant.index_forecast.fit_protocol import session_anchor
    from roboquant.index_forecast.reliable_inputs import session_schedule

    schedule, _ = session_schedule('2019-01-02', after)
    days = schedule['date'].tolist()
    assert holiday not in days
    assert days.index(after) == days.index(before) + 1
    left = session_anchor(before, 'XKRX', '2019-01-02', 21)
    right = session_anchor(after, 'XKRX', '2019-01-02', 21)
    assert right['session_ordinal'] == left['session_ordinal'] + 1
    assert right['session_ordinal'] == days.index(after)


def test_live_forecast_rejects_artifact_captured_after_prediction_cutoff(tmp_path):
    frame, config = _frame(), _settings()
    anchor_day = '2019-03-05'
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    current = frame.loc[frame.asof_date == anchor_day].copy()
    current['label_return'] = np.nan
    history = frame.loc[frame.asof_date < anchor_day].copy()
    captured = reliable.resolve_live_fit(history, current, selected, settings=config,
                                         state_dir=tmp_path, market='US_SPX')
    assert captured['status'] == 'ready'
    earlier = current.copy()
    earlier['prediction_cutoff'] = '2019-03-05T21:00:00+00:00'
    evaluation = {'selected': selected, 'status': 'accepted_shadow',
                  'gate_reasons': [], 'calibration_observations': []}
    result = reliable.forecast_reliable_latest(history, earlier, evaluation, settings=config,
        fit_artifact=captured['artifact'], require_live_snapshot=True)
    assert result['status'] == 'not_ready'
    assert result['expected_return'] is None
    assert any('prediction cutoff' in reason for reason in result['gate_reasons'])


def test_missing_anchor_outcome_does_not_erase_later_walk_forecasts(tmp_path):
    frame, config = _frame(), _settings()
    anchor_day, next_day = '2019-03-05', '2019-03-06'
    frame.loc[frame.asof_date == anchor_day, 'label_return'] = np.nan
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    current = frame.loc[frame.asof_date == anchor_day]
    history = frame.loc[frame.asof_date < anchor_day]
    captured = reliable.resolve_live_fit(history, current, selected, settings=config,
                                         state_dir=tmp_path, market='US_SPX')
    assert captured['status'] == 'ready'
    report = reliable.evaluate_reliable_forecasts(frame, settings=config)
    records = report['candidate_observations']['linear:price']
    matching = [row for row in records if row['asof_date'] == next_day]
    assert len(matching) == 1
    assert matching[0]['fit_asof'] == anchor_day
    # Provenance changes the artifact hash, but not the underlying model output.
    evaluation = {'selected': selected, 'status': 'accepted_shadow',
                  'gate_reasons': [], 'calibration_observations': []}
    prediction = reliable.forecast_reliable_latest(history, frame.loc[frame.asof_date == next_day],
        evaluation, settings=config, fit_artifact=captured['artifact'], require_live_snapshot=True)
    assert prediction['expected_return'] == pytest.approx(matching[0]['expected_return'])
    assert prediction['raw_p_up'] == pytest.approx(matching[0]['raw_p_up'])


def test_first_origin_anchor_can_train_using_pre_origin_sessions():
    from roboquant.index_forecast.fit_protocol import session_anchor

    sessions = xcals.get_calendar('XNYS').sessions_in_range('2018-09-04', '2019-01-07')
    rng = np.random.default_rng(94)
    frame = pd.DataFrame(rng.normal(0, .01, (len(sessions) - 1, len(PRICE_FEATURES))),
                         columns=PRICE_FEATURES)
    frame['volatility_20d'] = .01
    frame['asof_date'] = sessions[:-1].strftime('%Y-%m-%d')
    frame['target_date'] = sessions[1:].strftime('%Y-%m-%d')
    frame['prediction_cutoff'] = frame.asof_date + 'T22:00:00+00:00'
    frame['target_close_at'] = frame.target_date + 'T21:00:00+00:00'
    frame['label_available_at'] = frame.target_close_at
    frame['label_return'] = rng.normal(0, .01, len(frame))
    anchor = session_anchor('2019-01-02', 'XNYS', '2019-01-02', 21)
    _, training = _fit(frame, anchor)
    assert '2018-12-28' in training['_asof_label'].tolist()
    assert '2018-12-31' not in training['_asof_label'].tolist()


def test_late_feature_receipt_cannot_be_used_by_live_or_walk(tmp_path):
    frame, config = _frame(), _settings()
    anchor_day, prediction_day = '2019-03-05', '2019-03-06'
    frame['feature_available_at'] = frame.prediction_cutoff
    frame.loc[frame.asof_date == prediction_day, 'feature_available_at'] = '2030-01-01T00:00:00+00:00'
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    captured = reliable.resolve_live_fit(frame.loc[frame.asof_date < anchor_day],
        frame.loc[frame.asof_date == anchor_day], selected, settings=config,
        state_dir=tmp_path, market='US_SPX')
    assert captured['status'] == 'ready'
    evaluation = {'selected': selected, 'status': 'accepted_shadow',
                  'gate_reasons': [], 'calibration_observations': []}
    result = reliable.forecast_reliable_latest(frame, frame.loc[frame.asof_date == prediction_day],
        evaluation, settings=config, fit_artifact=captured['artifact'], require_live_snapshot=True)
    assert result['status'] == 'not_ready'
    assert result['expected_return'] is None
    report = reliable.evaluate_reliable_forecasts(frame, settings=config)
    assert not any(row['asof_date'] == prediction_day
                   for row in report['candidate_observations']['linear:price'])


@pytest.mark.parametrize("failure", ["partial_json", "tampered_model", "runtime_changed"])
def test_corrupt_or_incompatible_live_artifact_never_recaptures(tmp_path, failure):
    from roboquant.index_forecast.fit_protocol import artifact_path, content_hash

    frame, config = _frame(), _settings()
    day = '2019-03-05'
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    current = frame.loc[frame.asof_date == day]
    history = frame.loc[frame.asof_date < day]
    captured = reliable.resolve_live_fit(history, current, selected, settings=config,
                                         state_dir=tmp_path, market='US_SPX')
    assert captured['status'] == 'ready'
    path = artifact_path(tmp_path, 'US_SPX', day)
    if failure == 'partial_json':
        raw = '{'
    else:
        changed = json.loads(path.read_text())
        if failure == 'tampered_model':
            changed['models']['linear']['up_rate'] = .123
        else:
            changed['snapshot']['runtime']['packages']['numpy'] = 'impossible-version'
            changed['artifact_hash'] = content_hash({key: value for key, value in changed.items()
                                                      if key != 'artifact_hash'})
        raw = json.dumps(changed)
    path.write_text(raw)
    result = reliable.resolve_live_fit(history, current, selected, settings=config,
                                       state_dir=tmp_path, market='US_SPX')
    assert result['status'] == 'not_ready'
    assert result['reason']
    assert path.read_text() == raw
def test_interval_preserves_positive_current_volatility_below_denominator_floor():
    from roboquant.index_forecast.reliable_intervals import normalized_interval_shadows

    rows = [{'label_available_at': '2019-01-03T22:00:00+00:00',
             'target_close_at': '2019-01-03T21:00:00+00:00',
             'actual_return': (i + 1) / 1000, 'expected_return': 0.,
             'scale_at_prediction': .01,
             'baselines': {'mean': {'expected_return': .001}}} for i in range(10)]
    shadow = normalized_interval_shadows(rows, cutoff='2019-01-04T22:00:00Z',
        predicted=0., scale=1e-8, mean_prediction=.001, window=252, min_rows=10)['volatility_normalized']
    assert shadow['status'] == 'ready'
    assert shadow['range_high'] == pytest.approx(.9e-8)
    assert shadow['range_low'] == pytest.approx(-.9e-8)

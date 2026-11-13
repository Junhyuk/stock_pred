"""Behavioral regression tests for shared fit snapshots and distinct directions."""
import importlib
from dataclasses import asdict, replace

import numpy as np
import pandas as pd
import pytest

from roboquant.index_forecast import reliable
from roboquant.index_forecast.dataset import PRICE_FEATURES


def protocol():
    spec = importlib.util.find_spec('roboquant.index_forecast.fit_protocol')
    assert spec is not None, 'shared immutable fit protocol is missing'
    return importlib.import_module('roboquant.index_forecast.fit_protocol')


def sample_frame():
    import exchange_calendars as xcals
    sessions = xcals.get_calendar('XNYS').sessions_in_range('2019-01-02', '2019-08-01')
    rng = np.random.default_rng(13)
    frame = pd.DataFrame(rng.normal(0, .01, (len(sessions)-1, 7)), columns=PRICE_FEATURES)
    frame['volatility_20d'] = .01
    frame['asof_date'] = sessions[:-1].strftime('%Y-%m-%d')
    frame['target_date'] = sessions[1:].strftime('%Y-%m-%d')
    frame['prediction_cutoff'] = frame.asof_date + 'T22:00:00+00:00'
    frame['label_available_at'] = frame.target_date + 'T21:00:00+00:00'
    frame['target_close_at'] = frame.label_available_at
    frame['label_return'] = frame.return_1d * .2 + rng.normal(0, .01, len(frame))
    return frame


def config():
    return reliable.ReliableSettings(initial_train_days=25, purge_days=1, retrain_step_days=21,
        calibration_window=30, min_calibration_rows=10, development_days=25, test_days=25,
        bootstrap_samples=20, bootstrap_block_days=5, min_oos_rows=15, model_families=('linear',))


def test_exchange_anchor_does_not_depend_on_available_price_rows():
    api = protocol()
    # XNYS closes Jan 21; the 22nd exchange session starting Jan 2 is Feb 1.
    first = api.session_anchor('2019-02-01', 'XNYS', '2019-01-02', 21)
    middle = api.session_anchor('2019-02-12', 'XNYS', '2019-01-02', 21)
    assert first['anchor_asof'] == middle['anchor_asof'] == '2019-02-01'
    assert first['next_refit_asof'] == '2019-03-05'
    assert api.session_anchor('2019-01-01', 'XNYS', '2019-01-02', 21) is None


def test_snapshot_freezes_values_and_rejects_tampering_or_settings_change():
    api = protocol()
    data = reliable._prepare(sample_frame())
    anchor = api.session_anchor('2019-04-10', 'XNYS', '2019-01-02', 21)
    snapshot = api.make_fit_snapshot(data, PRICE_FEATURES, anchor, asdict(config()))
    assert snapshot['status'] == 'ready'
    restored = api.restore_fit_snapshot(snapshot, PRICE_FEATURES, asdict(config()))
    assert restored['_asof_label'].max() < snapshot['purge_boundary']
    assert (restored['_available'] <= pd.Timestamp(snapshot['fit_cutoff'])).all()
    expected = restored['label_return'].tolist()
    data['label_return'] = 999
    assert api.restore_fit_snapshot(snapshot, PRICE_FEATURES, asdict(config()))['label_return'].tolist() == expected
    changed = {**snapshot, 'fit_cutoff': '2040-01-01T00:00:00+00:00'}
    with pytest.raises(ValueError, match='hash'):
        api.restore_fit_snapshot(changed, PRICE_FEATURES, asdict(config()))
    with pytest.raises(ValueError, match='settings'):
        api.restore_fit_snapshot(snapshot, PRICE_FEATURES, asdict(replace(config(), purge_days=2)))


def test_known_feature_revision_after_its_row_cutoff_is_not_historical_pit_training():
    api = protocol()
    data = reliable._prepare(sample_frame())
    data['feature_available_at'] = data['_cutoff']
    bad_day = data.iloc[2]['_asof_label']
    # Known by the April fit, but unavailable at this January feature row.
    data.loc[2, 'feature_available_at'] = '2019-02-20T22:00:00+00:00'
    anchor = api.session_anchor('2019-04-10', 'XNYS', '2019-01-02', 21)
    snapshot = api.make_fit_snapshot(data, PRICE_FEATURES, anchor, asdict(config()))
    restored = api.restore_fit_snapshot(snapshot, PRICE_FEATURES, asdict(config()))
    assert bad_day not in restored['_asof_label'].tolist()


def test_latest_replays_same_anchor_between_refits_and_without_current_labels():
    protocol()
    settings = replace(config(), calendar_name='XNYS')
    frame = sample_frame()
    evaluation = reliable.evaluate_reliable_forecasts(frame, settings=settings)
    record = evaluation['candidate_observations']['linear:price'][-1]
    latest = frame.iloc[[-1]].copy()
    latest['label_return'] = np.nan
    live = reliable.forecast_reliable_latest(frame, latest, evaluation, settings=settings)
    assert live['fit_snapshot_hash'] == record['fit_snapshot_hash']
    assert live['fit_asof'] == record['fit_asof']
    assert live['expected_return'] == pytest.approx(record['expected_return'])
    assert live['raw_p_up'] == pytest.approx(record['raw_p_up'])


def test_live_mode_cannot_use_retrospective_snapshot_as_issued_evidence():
    protocol()
    settings = replace(config(), calendar_name='XNYS')
    frame = sample_frame()
    evaluation = reliable.evaluate_reliable_forecasts(frame, settings=settings)
    result = reliable.forecast_reliable_latest(frame, frame.iloc[[-1]], evaluation,
        settings=settings, require_live_snapshot=True)
    assert result['status'] == 'not_ready'
    assert result['expected_return'] is None
    assert 'missing_live_anchor_snapshot' in result['gate_reasons']


def test_probability_direction_is_not_return_direction():
    result = reliable._simple_metrics(np.array([.02, -.01]), np.array([-.01, .01]), np.array([.7, .2]))
    assert result.get('return_direction_hit') == 0.0
    assert result.get('probability_direction_hit') == 1.0
    assert result['direction_hit'] == 0.0  # Existing consumers retain their field.


def test_pending_anchor_can_capture_once_and_later_revisions_cannot_refit(tmp_path):
    assert hasattr(reliable, 'resolve_live_fit'), 'live immutable fit capture is missing'
    settings = replace(config(), calendar_name='XNYS')
    frame = sample_frame()
    latest = frame.loc[frame.asof_date == '2019-03-05'].copy()
    latest['label_return'] = np.nan
    history = frame.loc[frame.asof_date < '2019-03-05'].copy()
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    first = reliable.resolve_live_fit(history, latest, selected, settings=settings,
                                     state_dir=tmp_path, market='US_SPX')
    assert first['status'] == 'ready'
    assert first['artifact']['snapshot']['provenance'] == 'live_captured'
    assert 'rows' not in first['artifact']['snapshot']
    history['label_return'] = 123.0
    later = frame.loc[frame.asof_date == '2019-03-06']
    second = reliable.resolve_live_fit(history, later, selected, settings=settings,
                                      state_dir=tmp_path, market='US_SPX')
    assert second['artifact'] == first['artifact']
    changed = reliable.resolve_live_fit(history, later, selected,
        settings=replace(settings, purge_days=2), state_dir=tmp_path, market='US_SPX')
    assert changed['status'] == 'not_ready'
    assert changed['reason'] == 'fit model artifact protocol mismatch'


def test_missing_live_fit_between_anchors_waits_for_next_scheduled_anchor(tmp_path):
    assert hasattr(reliable, 'resolve_live_fit'), 'live immutable fit capture is missing'
    frame = sample_frame()
    result = reliable.resolve_live_fit(frame, frame.loc[frame.asof_date == '2019-03-06'],
        {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES},
        settings=replace(config(), calendar_name='XNYS'), state_dir=tmp_path, market='US_SPX')
    assert result['status'] == 'not_ready'
    assert result['reason'] == 'missing_live_anchor_snapshot'
    assert result['next_refit_asof'] == '2019-04-03'


def test_later_anchor_artifact_is_unavailable_at_an_earlier_prediction_cutoff(tmp_path):
    frame = sample_frame()
    settings = replace(config(), calendar_name='XNYS')
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    latest = frame.loc[frame.asof_date == '2019-03-05'].copy()
    captured = reliable.resolve_live_fit(frame, latest, selected, settings=settings,
                                        state_dir=tmp_path, market='US_SPX')
    assert captured['status'] == 'ready'
    latest['prediction_cutoff'] = '2019-03-05T21:00:00+00:00'
    earlier = reliable.resolve_live_fit(frame, latest, selected, settings=settings,
                                       state_dir=tmp_path, market='US_SPX')
    assert earlier['status'] == 'not_ready'
    forecast = reliable.forecast_reliable_latest(frame, latest, {'selected': selected},
        settings=settings, fit_artifact=captured['artifact'], require_live_snapshot=True)
    assert forecast['expected_return'] is None
    assert 'fit model artifact unavailable at prediction cutoff' in forecast['gate_reasons']


def test_shared_krx_sessions_exclude_election_and_constitution_holidays():
    from roboquant.index_forecast.reliable_inputs import session_schedule
    june, _ = session_schedule('2026-06-02', '2026-06-04')
    july, _ = session_schedule('2026-07-16', '2026-07-20')
    assert june.date.tolist() == ['2026-06-02', '2026-06-04']
    assert july.date.tolist() == ['2026-07-16', '2026-07-20']
    api = protocol()
    assert not api.session_anchor('2026-06-03', 'XKRX')['asof_is_session']
    assert not api.session_anchor('2026-07-17', 'XKRX')['asof_is_session']


def test_actual_pipeline_publishes_context_without_claiming_retrospective_fit_is_live(tmp_path, monkeypatch):
    import sqlite3
    from datetime import UTC, datetime

    from roboquant.index_forecast import reliable_pipeline

    dates = pd.bdate_range(end='2026-10-23', periods=140)
    rng = np.random.default_rng(29)
    prices = pd.DataFrame({'session_date': dates.strftime('%Y-%m-%d'),
        'close': 2000 * np.exp(np.cumsum(rng.normal(0, .01, len(dates)))),
        'source': 'test', 'published_at': None, 'ingested_at': '2026-10-23T13:00:00+00:00'})
    database = tmp_path / 'research.sqlite'
    with sqlite3.connect(database) as connection:
        pd.concat([prices.assign(index_code=market) for market in ('KOSPI', 'KOSDAQ')]).to_sql(
            'index_eod', connection, index=False)
    class Clock:
        @staticmethod
        def now(tz):
            return datetime(2026, 10, 23, 14, tzinfo=UTC).astimezone(tz)
    monkeypatch.setattr(reliable_pipeline, 'datetime', Clock)
    result = reliable_pipeline.run_reliable_pipeline(database, tmp_path / 'out',
        config={'settings': asdict(config())}, sessions=dates.append(pd.DatetimeIndex(['2026-10-26'])))
    assert result['status'] == 'research_only'
    assert not result['replay']
    for item in result['markets']:
        assert item['forecast']['status'] == 'not_ready'
        assert item['forecast']['expected_return'] is None
        assert item['incumbent_reference']['fit_provenance'] == 'retrospective'
        assert item['fit_state']['next_refit_asof']

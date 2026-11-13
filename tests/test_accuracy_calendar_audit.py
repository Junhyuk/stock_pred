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


def _audit_event(**changes):
    return {'event_id': 'audit-us-cpi', 'country': 'US', 'event_type': 'inflation',
            'title': 'CPI', 'scheduled_at': '2026-11-10T08:30:00-05:00',
            'scheduled_date': '2026-11-10', 'timezone': 'America/New_York',
            'time_precision': 'datetime', 'status': 'confirmed', 'published_at': None,
            'first_seen_at': '2026-10-01T00:00:00Z', 'reference_period': '2026-10',
            'source_id': 'bls', 'source_url': 'https://www.bls.gov/schedule/',
            'source_hash': 'audit-source', **changes}


def _audit_release(**changes):
    return {'event_id': 'audit-us-cpi', 'country': 'US', 'measure': 'cpi_headline_yoy',
            'value': 3.2, 'unit': 'percent', 'reference_period': '2026-10',
            'actual_release_at': '2026-11-10T13:30:00Z', 'first_seen_at': '2026-10-01T00:00:00Z',
            'vintage': 'first_release', 'seasonal_adjustment': 'NSA', 'source_id': 'bls',
            'source_url': 'https://www.bls.gov/news.release/cpi.htm', 'source_hash': 'audit-release',
            'revision_of': None, **changes}


def test_current_calendar_and_release_import_cannot_backdate_first_seen(tmp_path):
    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    store = EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    store.ingest({'events': [_audit_event()], 'releases': [_audit_release()], 'sources': []},
                 collected_at='2026-11-10T14:00:00Z')
    assert not store.events_asof('2026-11-10T13:59:59Z')
    assert not store.releases_asof('2026-11-10T13:59:59Z')
    assert len(store.events_asof('2026-11-10T14:00:00Z')) == 1
    assert len(store.releases_asof('2026-11-10T14:00:00Z')) == 1


def test_same_event_recollection_does_not_create_new_economic_version(tmp_path):
    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    store = EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    store.ingest({'events': [_audit_event()], 'releases': [], 'sources': []},
                 collected_at='2026-11-01T00:00:00Z')
    store.ingest({'events': [_audit_event(first_seen_at='2026-11-02T00:00:00Z')],
                  'releases': [], 'sources': []}, collected_at='2026-11-02T00:00:00Z')
    history = store.export_history()
    assert len(history['event_versions']) == 1
    assert len(history['collection_runs']) == 2


def test_bounded_fetch_accepts_large_valid_response_without_pipe_deadlock():
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from roboquant.index_forecast import economic_sources

    body = b'x' * (1024 * 1024)
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format, *_args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_address[1]}/'
        result = economic_sources._wall_bounded_fetch(url, 5.0)
        assert result.body == body
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_unverified_first_release_label_cannot_activate_numeric_feature(tmp_path):
    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    store = EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    previous = _audit_release(event_id='audit-us-cpi-prev', reference_period='2026-09',
        value=3.0, actual_release_at='2026-10-10T12:30:00Z')
    store.ingest({'events': [], 'releases': [previous], 'sources': []},
                 collected_at='2026-10-10T12:31:00Z')
    current = _audit_release()
    store.ingest({'events': [], 'releases': [current], 'sources': []},
                 collected_at='2026-11-10T13:31:00Z')
    rows = store.releases_asof('2026-11-10T14:00:00Z')
    assert len(rows) == 2 and not any(row['original_publication_verified'] for row in rows)
    state = store.numeric_features('2026-11-10T14:00:00Z')
    assert state['features']['us_cpi_yoy_change_pp'] is None


def test_source_receipt_is_recorded_after_delayed_response_not_run_start():
    import time
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from roboquant.index_forecast.economic_sources import collect_economic_sources

    receipt = []
    body = (b'<p>Wednesday, January 13, 2027 08:30 AM '
            b'Consumer Price Index for December 2026</p>')
    def delayed_fetch(_url, _timeout):
        time.sleep(.05)
        receipt.append(datetime.now(UTC))
        return SimpleNamespace(status=200, headers={}, body=body)
    source = {'source_id': 'audit-bls', 'country': 'US', 'access': 'public_no_key',
              'source_url': 'https://www.bls.gov/schedule/', 'parser': 'bls_schedule'}
    start = datetime.now(UTC)
    result = collect_economic_sources(now=start, registry={'sources': [source]}, fetch=delayed_fetch)
    assert result['events']
    assert pd.Timestamp(result['events'][0]['first_seen_at']) >= receipt[0]
    assert pd.Timestamp(result['sources'][0]['first_seen_at']) >= receipt[0]


def test_schedule_local_time_uses_correct_utc_offset_across_us_dst():
    from datetime import UTC, datetime

    from roboquant.index_forecast.economic_sources import _event

    source = {'source_id': 'audit-bls', 'country': 'US',
              'source_url': 'https://www.bls.gov/schedule/'}
    now = datetime(2026, 10, 1, tzinfo=UTC)
    daylight = _event(source, now, 'a', 'inflation', 'CPI', '2026-10-30',
                      timezone='America/New_York', scheduled_time='08:30')
    standard = _event(source, now, 'a', 'inflation', 'CPI', '2026-11-02',
                      timezone='America/New_York', scheduled_time='08:30')
    assert daylight['scheduled_at_utc'] == '2026-10-30T12:30:00+00:00'
    assert standard['scheduled_at_utc'] == '2026-11-02T13:30:00+00:00'


def test_observed_monthly_data_cannot_claim_a_future_reference_month(tmp_path):
    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    store = EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    for month, value in [('2026-11', 3.0), ('2026-12', 3.2)]:
        row = _audit_release(event_id=f'audit-{month}', reference_period=month, value=value,
            vintage='current_snapshot', definition_version='bls:cpi-u:headline:yoy:nsa',
            actual_release_at=None)
        store.ingest({'events': [], 'releases': [row], 'sources': []},
                     collected_at='2026-11-10T14:00:00Z')
    state = store.numeric_features('2026-11-10T15:00:00Z', policy='observed_current_receipt_v1')
    assert state['features']['us_cpi_yoy_change_pp'] is None


def test_irregular_policy_meetings_compare_previous_observed_rate(tmp_path):
    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    store = EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    for month, value, collected in [('2026-10', 4.25, '2026-10-29T18:01:00Z'),
                                    ('2026-12', 4.50, '2026-12-17T19:01:00Z')]:
        row = _audit_release(event_id=f'audit-fed-{month}', measure='policy_rate',
            reference_period=month, value=value, vintage='current_snapshot',
            definition_version='fed:target-midpoint', seasonal_adjustment='not_applicable',
            actual_release_at=None)
        store.ingest({'events': [], 'releases': [row], 'sources': []}, collected_at=collected)
    state = store.numeric_features('2026-12-18T00:00:00Z', policy='observed_current_receipt_v1')
    assert state['features']['us_policy_change_bp'] == pytest.approx(25.0)


def test_failed_anchor_fit_cannot_be_recaptured_later_on_same_anchor(tmp_path):
    frame, config = _frame(), _settings()
    anchor_day = '2019-03-05'
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    current = frame.loc[frame.asof_date == anchor_day]
    history = frame.loc[frame.asof_date < anchor_day]
    first = reliable.resolve_live_fit(history.iloc[:5], current, selected, settings=config,
                                      state_dir=tmp_path, market='US_SPX')
    assert first['status'] == 'not_ready'
    assert first['reason'] == 'insufficient_anchor_training_rows'
    rerun = reliable.resolve_live_fit(history, current, selected, settings=config,
                                      state_dir=tmp_path, market='US_SPX')
    assert rerun['status'] == 'not_ready'
    assert rerun['reason'] == first['reason']


def test_forward_ledger_cannot_score_before_published_target_close(tmp_path):
    import hashlib

    from roboquant.index_forecast.forward_ledger import ForwardLedger

    path = tmp_path / 'public'
    path.mkdir()
    context = {'asof_date': '2026-11-12', 'target_date': '2026-11-13',
               'stage': 'premarket_final', 'prediction_cutoff': '2026-11-12T23:00:00Z',
               'target_close_at': '2026-11-13T06:30:00Z', 'issuance_deadline': '2026-11-13T00:00:00Z'}
    forecast = {'expected_return': .01, 'p_up': .6, 'range_low': -.01, 'range_high': .03,
                'status': 'rejected', 'fit_provenance': 'live_captured'}
    payload = {'status': 'research_only', 'replay': False, 'run_id': 'audit',
               'markets': [{'market': 'KOSPI', 'context': context, 'forecast': forecast}]}
    raw = json.dumps(payload).encode()
    (path / 'reliable_index_forecast.json').write_bytes(raw)
    manifest = {'schema_version': 'reliable_publication_v1', 'run_id': 'audit', 'replay': False,
                'publication_checked_at': '2026-11-12T23:01:00Z',
                'payload_checksums': {'reliable_index_forecast.json': hashlib.sha256(raw).hexdigest()}}
    (path / 'reliable_publication.json').write_text(json.dumps(manifest))
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    protocol = {'candidate': 'audit'}
    ledger.register_protocol(protocol, registered_at='2026-11-12T22:49:00Z')
    ledger.record_publication(path, protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    early = {'market': 'KOSPI', 'asof_date': '2026-11-12', 'target_date': '2026-11-13',
             'actual_return': .02, 'target_close_at': '2026-11-13T05:00:00Z',
             'label_available_at': '2026-11-13T05:01:00Z'}
    ledger.record_outcomes([early], observed_at='2026-11-13T05:02:00Z')
    assert ledger.report()['markets']['KOSPI']['primary']['mature_n'] == 0


def test_blocked_forecast_cannot_become_forward_evidence(tmp_path):
    import hashlib

    from roboquant.index_forecast.forward_ledger import ForwardLedger

    directory = tmp_path / 'public'
    directory.mkdir()
    context = {'asof_date': '2026-11-12', 'target_date': '2026-11-13',
               'stage': 'premarket_final', 'prediction_cutoff': '2026-11-12T23:00:00Z',
               'target_close_at': '2026-11-13T06:30:00Z', 'issuance_deadline': '2026-11-13T00:00:00Z'}
    forecast = {'expected_return': .01, 'p_up': .6, 'range_low': -.01, 'range_high': .03,
                'status': 'blocked_unavailable_input', 'fit_provenance': 'live_captured'}
    payload = {'status': 'research_only', 'replay': False, 'run_id': 'audit-blocked',
               'markets': [{'market': 'KOSPI', 'context': context, 'forecast': forecast}]}
    raw = json.dumps(payload).encode()
    (directory / 'reliable_index_forecast.json').write_bytes(raw)
    manifest = {'schema_version': 'reliable_publication_v1', 'run_id': 'audit-blocked', 'replay': False,
                'publication_checked_at': '2026-11-12T23:01:00Z',
                'payload_checksums': {'reliable_index_forecast.json': hashlib.sha256(raw).hexdigest()}}
    (directory / 'reliable_publication.json').write_text(json.dumps(manifest))
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    protocol = {'candidate': 'audit'}
    ledger.register_protocol(protocol, registered_at='2026-11-12T22:49:00Z')
    ledger.record_publication(directory, protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    assert ledger.export_history()['forecasts'] == []
    assert len(ledger.export_history()['attempts']) == 1


def test_korean_price_frame_supplies_target_clock_for_calendar_context():
    from roboquant.index_forecast.economic_forecast import calendar_frame
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame

    class CompleteStore:
        def forecast_context(self, **kwargs):
            assert pd.Timestamp(kwargs['asof_close']) < pd.Timestamp(kwargs['target_close'])
            return {'event_bucket': True, 'calendar_complete': True}

    sessions = pd.bdate_range('2026-10-01', periods=40)
    prices = pd.DataFrame({'session_date': sessions.strftime('%Y-%m-%d'),
                           'close': 2000 + np.arange(len(sessions), dtype=float)})
    frame = build_reliable_price_frame(prices, sessions=sessions)
    enriched, contexts = calendar_frame(frame, CompleteStore())
    assert contexts
    assert (enriched['calendar_event_bucket'] == True).any()  # noqa: E712


def test_korean_target_and_asof_clocks_follow_supplied_exchange_schedule():
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame

    sessions = pd.DataFrame({'date': ['2026-10-01', '2026-10-02', '2026-10-05'],
        'close_at': ['2026-10-01T14:30:00+09:00', '2026-10-02T15:00:00+09:00',
                     '2026-10-05T15:30:00+09:00']})
    prices = pd.DataFrame({'session_date': sessions['date'], 'close': [2000., 2001., 2002.]})
    row = build_reliable_price_frame(prices, sessions=sessions).iloc[0]
    assert pd.Timestamp(row['asof_close_at']) == pd.Timestamp('2026-10-01T05:30:00Z')
    assert pd.Timestamp(row['target_close_at']) == pd.Timestamp('2026-10-02T06:00:00Z')


def test_collection_with_all_sources_unavailable_is_not_reported_collected(tmp_path):
    import sys

    from roboquant.index_forecast.automation_state import run_calendar_collection

    payload = {'events': [], 'releases': [], 'sources': [
        {'source_id': 'failed', 'status': 'unavailable', 'reason': 'http_status_403'}]}
    script = 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(' + repr(json.dumps(payload)) + ')'
    result = run_calendar_collection(tmp_path / 'public', tmp_path / 'private', budget_seconds=10,
        worker_command=[sys.executable, '-c', script, '{result}'])
    assert result['status'] in {'unavailable', 'partial'}
    assert result.get('reason') or result['status'] == 'partial'


def test_forward_report_scores_shadow_interval_on_aligned_mature_days(tmp_path):
    import hashlib

    from roboquant.index_forecast.forward_ledger import ForwardLedger

    directory = tmp_path / 'public'
    directory.mkdir()
    context = {'asof_date': '2026-11-12', 'target_date': '2026-11-13',
               'stage': 'premarket_final', 'prediction_cutoff': '2026-11-12T23:00:00Z',
               'target_close_at': '2026-11-13T06:30:00Z', 'issuance_deadline': '2026-11-13T00:00:00Z'}
    forecast = {'expected_return': .01, 'p_up': .6, 'range_low': -.01, 'range_high': .03,
                'status': 'rejected', 'fit_provenance': 'live_captured',
                'interval_shadows': {'volatility_normalized': {'status': 'ready',
                    'range_low': -.02, 'range_high': .04}}}
    payload = {'status': 'research_only', 'replay': False, 'run_id': 'audit-interval',
               'markets': [{'market': 'KOSPI', 'context': context, 'forecast': forecast}]}
    raw = json.dumps(payload).encode()
    (directory / 'reliable_index_forecast.json').write_bytes(raw)
    manifest = {'schema_version': 'reliable_publication_v1', 'run_id': 'audit-interval', 'replay': False,
                'publication_checked_at': '2026-11-12T23:01:00Z',
                'payload_checksums': {'reliable_index_forecast.json': hashlib.sha256(raw).hexdigest()}}
    (directory / 'reliable_publication.json').write_text(json.dumps(manifest))
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    protocol = {'candidate': 'audit'}
    ledger.register_protocol(protocol, registered_at='2026-11-12T22:49:00Z')
    ledger.record_publication(directory, protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    outcome = {'market': 'KOSPI', 'asof_date': '2026-11-12', 'target_date': '2026-11-13',
               'actual_return': .05, 'target_close_at': '2026-11-13T06:30:00Z',
               'label_available_at': '2026-11-13T07:00:00Z'}
    ledger.record_outcomes([outcome], observed_at='2026-11-13T07:01:00Z')
    report = ledger.report()['markets']['KOSPI']['primary']
    shadow = report['interval_shadows']['volatility_normalized']
    assert shadow['mature_n'] == 1
    assert shadow['interval_score80'] == pytest.approx(.16)
    assert shadow['incumbent_interval_score80'] == pytest.approx(.24)


def test_frozen_sqlite_includes_committed_wal_rows(tmp_path):
    import sqlite3

    from roboquant.index_forecast.automation_state import freeze_inputs, restore_inputs

    source = tmp_path / 'prices.sqlite'
    connection = sqlite3.connect(source)
    try:
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
        connection.execute('CREATE TABLE observed_prices (session_date TEXT)')
        connection.commit()
        connection.execute('PRAGMA wal_checkpoint(FULL)')
        connection.execute("INSERT INTO observed_prices VALUES ('2026-11-13')")
        connection.commit()
        freeze_inputs({'sqlite': source}, tmp_path / 'frozen',
                      prepared_at='2026-11-15T22:58:00Z', code_hash='audit')
        result = restore_inputs(tmp_path / 'frozen', now='2026-11-15T23:01:00Z', code_hash='audit')
        assert result['status'] == 'ready'
        with sqlite3.connect(result['paths']['sqlite']) as restored:
            assert restored.execute('SELECT session_date FROM observed_prices').fetchall() == [('2026-11-13',)]
    finally:
        connection.close()


def test_encrypted_cache_cannot_bypass_prepared_day_or_missing_key(tmp_path):
    import base64
    import sqlite3

    from roboquant.index_forecast.automation_state import (
        freeze_inputs,
        open_private_cache,
        restore_inputs,
        seal_private_cache,
    )

    source = tmp_path / 'prices.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE observed_prices (session_date TEXT)')
    bundle = tmp_path / 'bundle'
    freeze_inputs({'sqlite': source}, bundle, prepared_at='2026-11-15T22:58:00Z', code_hash='audit')
    archive = tmp_path / 'cache.enc'
    with pytest.raises(ValueError):
        seal_private_cache(bundle, archive, key='')
    key = base64.b64encode(b'a' * 32).decode()
    seal_private_cache(bundle, archive, key=key)
    restored = tmp_path / 'restored'
    open_private_cache(archive, restored, key=key)
    assert restore_inputs(restored, now='2026-11-15T23:01:00Z', code_hash='audit')['status'] == 'ready'
    assert restore_inputs(restored, now='2026-11-16T23:01:00Z', code_hash='audit')['status'] == 'not_ready'


def test_prepare_cannot_complete_manifest_after_0800_even_if_backup_started_before(tmp_path, monkeypatch):
    import os
    import sqlite3
    from datetime import UTC, datetime

    from roboquant.index_forecast import automation_state

    source = tmp_path / 'prices.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE observed_prices (session_date TEXT)')
    clock = {'after_link': False}
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = cls(2026, 11, 15, 23, 0, 1, tzinfo=UTC) if clock['after_link'] else cls(2026, 11, 15, 22, 59, 59, tzinfo=UTC)
            return moment if tz is None else moment.astimezone(tz)
    link = os.link
    def late_link(source_path, target_path):
        link(source_path, target_path)
        clock['after_link'] = True
    monkeypatch.setattr(automation_state, 'datetime', Clock)
    monkeypatch.setattr(automation_state.os, 'link', late_link)
    directory = tmp_path / 'prepared'
    with pytest.raises(ValueError, match='08:00'):
        automation_state.freeze_inputs({'sqlite': source}, directory, code_hash='audit')
    assert not (directory / 'manifest.json').exists()


def test_invalid_source_does_not_rollback_independent_source_in_live_collection(tmp_path):
    import sys

    from roboquant.index_forecast.automation_state import run_calendar_collection

    good = _audit_release(event_id='audit-good', source_id='source_good',
                          source_url='https://good.example/release')
    bad = _audit_release(event_id='audit-bad', source_id='source_bad',
                         source_url='https://bad.example/release', reference_period=None)
    payload = {'events': [], 'releases': [bad, good], 'sources': [
        {'source_id': 'source_bad', 'status': 'ok'}, {'source_id': 'source_good', 'status': 'ok'}]}
    script = 'import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(' + repr(json.dumps(payload)) + ')'
    output = tmp_path / 'public'
    status = run_calendar_collection(output, tmp_path / 'private', budget_seconds=10,
        worker_command=[sys.executable, '-c', script, '{result}'])
    assert status['status'] == 'partial'
    history = json.loads((output / 'economic_history.json').read_text())
    assert [row['source_id'] for row in history['release_versions']] == ['source_good']
    bad_sources = [row for row in history['collection_runs'][-1]['sources'] if row['source_id'] == 'source_bad']
    assert bad_sources and bad_sources[0]['status'] != 'ok'

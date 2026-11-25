from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from roboquant.index_forecast import postclose
from roboquant.index_forecast.reliable_inputs import resolve_forecast_context

KST = ZoneInfo('Asia/Seoul')
PROTOCOL = 'after_close_daily_v1'
SESSIONS = ['2026-11-26', '2026-11-27', '2026-11-30', '2026-12-01']


def local(value):
    return datetime.fromisoformat(value).replace(tzinfo=KST)


@pytest.mark.parametrize(('close', 'want'), [
    ('2026-11-26T15:30:00', '2026-11-27T08:00:00'),
    ('2026-11-27T15:30:00', '2026-11-28T08:00:00'),
    ('2026-11-28T06:00:00', '2026-11-28T08:00:00'),
    ('2026-11-28T15:30:00', '2026-11-30T08:00:00'),
])
def test_first_morning_capture_clock_handles_friday_saturday_and_us_close(close, want):
    assert postclose.morning_cutoff(local(close)) == pd.Timestamp(local(want))


def test_morning_capture_only_on_first_scheduled_date():
    anchor = local('2026-11-27T15:30:00')
    assert not postclose.morning_fit_capture_allowed(anchor, local('2026-11-28T07:59:59'))
    assert postclose.morning_fit_capture_allowed(anchor, local('2026-11-28T08:03:00'))
    assert not postclose.morning_fit_capture_allowed(anchor, local('2026-11-30T08:03:00'))


@pytest.mark.parametrize(('asof', 'now', 'expected', 'target'), [
    ('2026-11-27', '2026-11-28T07:59:59', 'blocked_issuance_window', '2026-11-30'),
    ('2026-11-27', '2026-11-28T08:03:00', 'ready', '2026-11-30'),
    ('2026-11-27', '2026-11-29T08:03:00', 'blocked_issuance_window', '2026-11-30'),
    ('2026-11-27', '2026-11-30T08:03:00', 'ready', '2026-11-30'),
    ('2026-11-27', '2026-11-30T09:00:00', 'blocked_issuance_deadline', '2026-11-30'),
    ('2026-11-26', '2026-11-28T08:03:00', 'blocked_stale_input', None),
])
def test_morning_uses_actual_cutoff_and_latest_kr_close(asof, now, expected, target):
    context = resolve_forecast_context(asof, now=local(now), sessions=SESSIONS,
                                       issuance_protocol=PROTOCOL, operation_mode='morning')
    assert context['status'] == expected
    if target:
        assert context['target_date'] == target
    if expected == 'ready':
        assert context['stage'] == 'morning'
        assert pd.Timestamp(context['prediction_cutoff']) == pd.Timestamp(local(now))


def test_legacy_postclose_does_not_accept_morning_implicitly():
    result = resolve_forecast_context('2026-11-27', now=local('2026-11-28T08:03:00'),
                                      sessions=SESSIONS, issuance_protocol=PROTOCOL)
    assert result['status'] == 'blocked_issuance_window'


def test_prepared_config_transfers_signed_manifest_operation_mode(monkeypatch, tmp_path):
    from roboquant.index_forecast import automation_state
    manifest = {'manifest_hash': 'id', 'prepared_at': '2026-11-28T08:01:00+09:00',
                'kst_date': '2026-11-28', 'code_hash': 'hash', 'operation_mode': 'morning'}
    monkeypatch.setattr(automation_state, 'restore_inputs', lambda *a, **kw: {
        'status': 'ready', 'manifest': manifest, 'paths': {'market_closes': tmp_path / 'closes', 'market_closes_receipt': tmp_path / 'receipt'}})
    monkeypatch.setattr(automation_state, 'source_fingerprint', lambda: 'hash')
    monkeypatch.setattr(postclose, 'verify_market_closes', lambda *a: {})
    args = SimpleNamespace(issuance_protocol=PROTOCOL, markets=None, prepared_manifest=tmp_path / 'manifest.json',
                           sqlite=None, us_observations=None, economic_sqlite=None, fit_state_dir=None)
    config = postclose.prepared_config(args, {})
    assert config['operation_mode'] == 'morning'
    assert config['prepared_inputs']['operation_mode'] == 'morning'


def test_late_morning_cannot_roll_to_the_next_anchor_after_market_close():
    result = resolve_forecast_context('2026-11-30', now=local('2026-11-30T18:00:00'),
                                      sessions=SESSIONS, issuance_protocol=PROTOCOL, operation_mode='morning')
    assert result['status'] != 'ready'
    assert result['expected_asof_date'] == '2026-11-27'


def test_morning_completion_does_not_conflict_with_manual_postclose(monkeypatch, tmp_path):
    def payload(path):
        return {'status': 'research_only', 'issuance_protocol': PROTOCOL, 'issued_at': '2026-11-30T00:02:00Z',
                'run_id': 'one', 'markets': [{'market': 'KOSPI', 'forecast_id': 'id',
                    'context': {'stage': 'morning', 'target_date': '2026-11-30'},
                    'forecast': {'status': 'research_candidate', 'calibration_ready': True,
                                 'expected_return': 0.01, 'p_up': .55}}]}
    monkeypatch.setattr(postclose, 'verified_latest', payload)
    completed = postclose.verified_completion_records(tmp_path)
    assert all(row['operation_mode'] == 'morning' for row in completed)
    targets = {'KOSPI': '2026-11-30'}
    manual = [{'issuance_protocol': PROTOCOL, 'market': 'KOSPI', 'target_date': '2026-11-30'}]
    assert postclose.pending_markets(targets, manual, operation_mode='morning') == ['KOSPI']
    assert postclose.pending_markets(targets, completed, operation_mode='morning') == []
    assert postclose.pending_markets(targets, manual) == []


def test_us_morning_uses_latest_completed_session_and_actual_frozen_cutoff(tmp_path):
    from dataclasses import asdict
    from test_us_forecast_integration import fixture, settings
    from roboquant.index_forecast.us_market_pipeline import run_us_reliable_pipeline
    _, _, observations = fixture(tmp_path)
    prepared = '2026-10-23T23:05:00+00:00'
    result = run_us_reliable_pipeline(
        observations, tmp_path / 'us',
        config={'issuance_protocol': PROTOCOL, 'operation_mode': 'morning',
                'prepared_inputs': {'prepared_at': prepared}, 'markets': ['US_SPX'],
                'settings': asdict(settings())},
        now=pd.Timestamp('2026-10-23T23:10:00Z').to_pydatetime())
    assert len(result['markets']) == 1
    context = result['markets'][0]['context']
    assert context['asof_date'] == '2026-10-23'
    assert context['target_date'] == '2026-10-26'
    assert context['stage'] == 'morning'
    assert pd.Timestamp(context['prediction_cutoff']) == pd.Timestamp(prepared)


def test_us_morning_cannot_publish_after_target_open(tmp_path):
    from dataclasses import asdict
    from test_us_forecast_integration import fixture, settings
    from roboquant.index_forecast.us_market_pipeline import run_us_reliable_pipeline
    _, _, observations = fixture(tmp_path)
    result = run_us_reliable_pipeline(
        observations, tmp_path / 'late-us',
        config={'issuance_protocol': PROTOCOL, 'operation_mode': 'morning',
                'prepared_inputs': {'prepared_at': '2026-10-25T23:05:00Z'},
                'markets': ['US_SPX'], 'settings': asdict(settings())},
        now=pd.Timestamp('2026-10-26T14:00:00Z').to_pydatetime())
    assert result['markets'] == []
    assert 'US_SPX:target_session_already_open' in result['reasons']


def test_morning_current_row_keeps_actual_cutoff_but_kr_historical_rows_stay_postclose():
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame
    sessions = pd.bdate_range('2026-09-01', '2026-11-20').strftime('%Y-%m-%d').tolist()
    prices = pd.DataFrame({'session_date': sessions[:-4], 'close': range(100, 100 + len(sessions[:-4]))})
    context = {'asof_date': prices.session_date.iloc[-1], 'stage': 'morning',
               'prediction_cutoff': sessions[-4] + 'T08:05:00+09:00'}
    result = build_reliable_price_frame(prices, sessions=sessions, forecast_context=context)
    assert pd.Timestamp(result.iloc[-2].prediction_cutoff).tz_convert(KST).hour == 18
    assert pd.Timestamp(result.iloc[-1].prediction_cutoff) == pd.Timestamp(context['prediction_cutoff'])


def test_forward_ledger_separates_morning_and_postclose_uses_issued_close_and_roundtrips(tmp_path):
    import hashlib
    import json
    from test_postclose_review_fixes import issued
    from roboquant.index_forecast.forward_ledger import ForwardLedger
    protocol = {'protocol': PROTOCOL}
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    ledger.register_protocol(protocol, registered_at='2026-11-04T08:59:00Z')
    path = tmp_path / 'public'
    issued(path, baseline=100, cohort='manual')
    ledger.record_publication(path, protocol=protocol, attempted_at='2026-11-04T09:02:00Z')

    def morning_issue(baseline, minute):
        payload = issued(path, baseline=baseline, cohort=f'morning-{minute}')
        ctx = payload['markets'][0]['context']
        ctx['stage'] = 'morning'
        ctx['prediction_cutoff'] = '2026-11-04T23:00:00Z'
        raw = json.dumps(payload).encode()
        receipt = json.loads((path / 'reliable_publication.json').read_text())
        receipt['publication_checked_at'] = f'2026-11-04T23:{minute}:00Z'
        for name in receipt['payload_checksums']:
            (path / name).write_bytes(raw)
            receipt['payload_checksums'][name] = hashlib.sha256(raw).hexdigest()
        (path / 'reliable_publication.json').write_text(json.dumps(receipt))
        return ledger.record_publication(path, protocol=protocol, attempted_at=f'2026-11-04T23:{int(minute)+1:02}:00Z')

    assert morning_issue(120, '01')['status'] == 'verified'
    morning_issue(125, '04')
    forecasts = ledger.export_history()['forecasts']
    assert len(forecasts) == 2
    assert sorted(row['issued_baseline_close'] for row in forecasts) == [100, 120]
    ledger.record_outcomes([{'market': 'KOSPI', 'asof_date': '2026-11-04', 'target_date': '2026-11-05',
        'target_close_at': '2026-11-05T06:30:00Z', 'label_available_at': '2026-11-05T06:30:00Z',
        'target_close': 132, 'target_observed_at': '2026-11-05T06:40:00Z', 'actual_return': 999}],
        observed_at='2026-11-05T07:00:00Z')
    report = ledger.report()
    assert len(report['cohorts']) == 2
    assert {x['operation_mode'] for x in report['cohorts']} == {'morning', 'postclose'}
    assert all(x['mature_n'] == 1 for x in report['cohorts'])
    assert sorted(x['actual_return'] for x in ledger.export_history()['outcomes']) == pytest.approx([.1, .32])
    restored = ForwardLedger(tmp_path / 'restored.sqlite')
    restored.import_history(ledger.export_history(), trusted_archive=True)
    assert restored.export_history() == ledger.export_history()


def test_unsigned_config_cannot_override_prepared_operation_mode(monkeypatch, tmp_path):
    from roboquant.index_forecast import automation_state
    manifest = {'manifest_hash': 'id', 'prepared_at': '2026-11-27T09:01:00Z',
                'kst_date': '2026-11-27', 'code_hash': 'hash'}
    monkeypatch.setattr(automation_state, 'restore_inputs', lambda *a, **kw: {
        'status': 'ready', 'manifest': manifest,
        'paths': {'market_closes': tmp_path / 'closes', 'market_closes_receipt': tmp_path / 'receipt'}})
    monkeypatch.setattr(automation_state, 'source_fingerprint', lambda: 'hash')
    monkeypatch.setattr(postclose, 'verify_market_closes', lambda *a: {})
    args = SimpleNamespace(issuance_protocol=PROTOCOL, markets=None, prepared_manifest=tmp_path / 'manifest.json',
                           sqlite=None, us_observations=None, economic_sqlite=None, fit_state_dir=None)
    config = postclose.prepared_config(args, {'operation_mode': 'morning'})
    assert config.get('operation_mode') != 'morning'

"""Forward evidence starts at verified publication, never preparation/replay."""
import hashlib
import importlib
import json

import pytest


def engine():
    spec = importlib.util.find_spec('roboquant.index_forecast.forward_ledger')
    assert spec is not None, 'verified-publication forward ledger is missing'
    return importlib.import_module('roboquant.index_forecast.forward_ledger')


def publication(path, *, p=.6, checked='2026-11-12T23:01:00+00:00', replay=False):
    path.mkdir(parents=True, exist_ok=True)
    forecast = {'expected_return': .01, 'p_up': p, 'range_low': -.01, 'range_high': .03,
                'status': 'rejected', 'calibration_ready': True, 'fit_provenance': 'live_captured',
                'forecast_id': f'forecast-{p}', 'production_weight': 0.,
                'baselines': {'mean': {'expected_return': 0., 'p_up': .5}, 'up_rate': {'p_up': .5}}}
    context = {'asof_date': '2026-11-12', 'target_date': '2026-11-13', 'stage': 'premarket_final',
               'prediction_cutoff': '2026-11-12T23:00:00+00:00',
               'target_close_at': '2026-11-13T06:30:00+00:00',
               'issuance_deadline': '2026-11-13T00:00:00+00:00'}
    payload = {'schema_version': 'reliable_index_forecast_v1', 'status': 'research_only',
               'run_id': f'run-{p}', 'run_started_at': '2026-11-12T22:50:00+00:00',
               'prepared_at': '2026-11-12T22:59:00+00:00', 'replay': replay,
               'markets': [{'market': 'KOSPI', 'context': context, 'forecast': forecast,
                            'incumbent_reference': {**forecast, 'fit_provenance': 'retrospective'}}]}
    raw = json.dumps(payload).encode()
    (path / 'reliable_index_forecast.json').write_bytes(raw)
    manifest = {'schema_version': 'reliable_publication_v1', 'run_id': payload['run_id'],
                'status': 'research_only', 'replay': replay, 'publication_checked_at': checked,
                'payload_checksums': {'reliable_index_forecast.json': hashlib.sha256(raw).hexdigest()}}
    (path / 'reliable_publication.json').write_text(json.dumps(manifest))
    return payload


def test_first_verified_publication_is_immutable_and_uses_manifest_clock(tmp_path):
    ledger = engine().ForwardLedger(tmp_path / 'forward.sqlite')
    ledger.register_protocol({'candidate': 'frozen-v1'}, registered_at='2026-11-12T22:49:00Z')
    path = tmp_path / 'public'
    publication(path)
    protocol = {'candidate': 'frozen-v1'}
    ledger.record_publication(path, protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    publication(path, p=.9, checked='2026-11-12T23:05:00+00:00')
    ledger.record_publication(path, protocol=protocol, attempted_at='2026-11-12T23:06:00Z')
    records = ledger.export_history()['forecasts']
    primary = [row for row in records if row['role'] == 'primary']
    assert len(primary) == 1
    assert primary[0]['p_up'] == .6
    assert primary[0]['issued_at'] == '2026-11-12T23:01:00+00:00'
    assert len(ledger.export_history()['attempts']) == 2


@pytest.mark.parametrize('failure', ['replay', 'late', 'missing_manifest', 'checksum'])
def test_unverified_or_late_runs_are_attempts_not_forward_forecasts(tmp_path, failure):
    ledger = engine().ForwardLedger(tmp_path / 'forward.sqlite')
    ledger.register_protocol({'candidate': 'frozen-v1'}, registered_at='2026-11-12T22:49:00Z')
    path = tmp_path / 'public'
    publication(path, replay=failure == 'replay',
                checked='2026-11-13T00:00:00+00:00' if failure == 'late' else '2026-11-12T23:01:00+00:00')
    if failure == 'missing_manifest':
        (path / 'reliable_publication.json').unlink()
    if failure == 'checksum':
        (path / 'reliable_index_forecast.json').write_text('{}')
    ledger.record_publication(path, protocol={'candidate': 'frozen-v1'}, attempted_at='2026-11-13T01:00:00Z')
    assert ledger.export_history()['forecasts'] == []
    assert len(ledger.export_history()['attempts']) == 1


def test_only_mature_first_observed_outcomes_score_and_revisions_do_not_rewrite(tmp_path):
    ledger = engine().ForwardLedger(tmp_path / 'forward.sqlite')
    ledger.register_protocol({'candidate': 'frozen-v1'}, registered_at='2026-11-12T22:49:00Z')
    path = tmp_path / 'public'
    publication(path)
    ledger.record_publication(path, protocol={'candidate': 'frozen-v1'}, attempted_at='2026-11-12T23:02:00Z')
    observation = {'market': 'KOSPI', 'asof_date': '2026-11-12', 'target_date': '2026-11-13',
                   'actual_return': .02, 'target_close_at': '2026-11-13T06:30:00Z',
                   'label_available_at': '2026-11-13T07:00:00Z'}
    ledger.record_outcomes([observation], observed_at='2026-11-13T06:59:00Z')
    assert ledger.report()['markets']['KOSPI']['primary']['mature_n'] == 0
    ledger.record_outcomes([observation], observed_at='2026-11-13T07:01:00Z')
    ledger.record_outcomes([{**observation, 'actual_return': -.5}], observed_at='2026-11-14T07:01:00Z')
    report = ledger.report()['markets']['KOSPI']['primary']
    assert report['mature_n'] == 1
    assert report['metrics']['brier'] == pytest.approx(.16)
    assert report['metrics']['mae'] == pytest.approx(.01)
    assert report['status'] == 'collecting_forward_evidence'
    assert report['production_weight'] == 0


def test_protocol_must_be_registered_before_verified_publication(tmp_path):
    ledger = engine().ForwardLedger(tmp_path / 'forward.sqlite')
    path = tmp_path / 'public'
    publication(path)
    ledger.register_protocol({'candidate': 'late'}, registered_at='2026-11-12T23:02:00Z')
    ledger.record_publication(path, protocol={'candidate': 'late'}, attempted_at='2026-11-12T23:03:00Z')
    assert ledger.export_history()['forecasts'] == []
    assert ledger.export_history()['attempts'][0]['reason'] == 'protocol_not_registered_before_issue'


def test_forward_baselines_share_mature_forecast_denominator(tmp_path):
    ledger = engine().ForwardLedger(tmp_path / 'ledger.sqlite')
    protocol = {'candidate': 'baseline-v1'}
    ledger.register_protocol(protocol, registered_at='2026-11-12T22:49:00Z')
    publication(tmp_path / 'public')
    ledger.record_publication(tmp_path / 'public', protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    ledger.record_outcomes([{'market': 'KOSPI', 'asof_date': '2026-11-12', 'target_date': '2026-11-13',
        'actual_return': .02, 'target_close_at': '2026-11-13T06:30:00Z',
        'label_available_at': '2026-11-13T07:00:00Z'}], observed_at='2026-11-13T07:01:00Z')
    report = ledger.report()['markets']['KOSPI']['primary']
    assert report['baselines']['up_rate']['mature_n'] == 1
    assert report['baselines']['up_rate']['brier'] == pytest.approx(.25)
    assert report['baselines']['up_rate']['candidate_minus_baseline_brier'] == pytest.approx(-.09)
    assert report['metrics']['log_loss'] > 0
    assert sum(row['n'] for row in report['calibration_bins']) == 1


def test_forward_regime_denominators_are_preserved_from_issuance(tmp_path):
    ledger = engine().ForwardLedger(tmp_path / 'ledger.sqlite')
    protocol = {'candidate': 'regime-v1'}
    ledger.register_protocol(protocol, registered_at='2026-11-12T22:49:00Z')
    path = tmp_path / 'public'
    payload = publication(path)
    for item in payload['markets']:
        item['forecast']['volatility_regime'] = 'high'
    raw = json.dumps(payload).encode()
    (path / 'reliable_index_forecast.json').write_bytes(raw)
    manifest = json.loads((path / 'reliable_publication.json').read_text())
    manifest['payload_checksums']['reliable_index_forecast.json'] = hashlib.sha256(raw).hexdigest()
    (path / 'reliable_publication.json').write_text(json.dumps(manifest))
    ledger.record_publication(path, protocol=protocol, attempted_at='2026-11-12T23:02:00Z')
    ledger.record_outcomes([{'market': 'KOSPI', 'asof_date': '2026-11-12', 'target_date': '2026-11-13',
        'actual_return': .02, 'target_close_at': '2026-11-13T06:30:00Z',
        'label_available_at': '2026-11-13T07:00:00Z'}], observed_at='2026-11-13T07:01:00Z')
    report = ledger.report()['markets']['KOSPI']['primary']
    assert report['regimes']['high']['mature_n'] == 1
    assert report['regimes']['low']['mature_n'] == 0

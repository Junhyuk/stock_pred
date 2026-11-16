"""Independent-review regressions: issued price basis, PIT, retries, late publication."""
import hashlib
import json
import sqlite3

import pandas as pd
import pytest

from roboquant.index_forecast.economic_forecast import finish_forward
from roboquant.index_forecast.forward_ledger import ForwardLedger
from roboquant.index_forecast.market_closes import effective_index_eod_frame, apply_live_revisions, record_observation
from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame, _publish_staged_run
from roboquant.index_forecast.sqlite_store import connect_index_forecast

PROTOCOL = {'protocol': 'after_close_daily_v1'}


def issued(path, baseline=120, *, cohort='one', reference_baseline=None):
    path.mkdir(exist_ok=True)
    forecast = {'status': 'rejected', 'fit_provenance': 'live_captured', 'expected_return': .05,
                'p_up': .6, 'range_low': -.1, 'range_high': .2, 'latest_close': baseline}
    context = {'asof_date': '2026-11-04', 'target_date': '2026-11-05', 'stage': 'postclose',
               'prediction_cutoff': '2026-11-04T09:00:00Z', 'issuance_deadline': '2026-11-05T00:00:00Z',
               'target_close_at': '2026-11-05T06:30:00Z'}
    row = {'market': 'KOSPI', 'context': context, 'forecast': forecast}
    if reference_baseline is not None:
        row['incumbent_reference'] = {**forecast, 'latest_close': reference_baseline, 'fit_provenance': 'retrospective'}
    payload = {'run_id': cohort, 'issuance_protocol': PROTOCOL['protocol'], 'replay': False, 'markets': [row]}
    checksums = {}
    for name in ('reliable_index_forecast.json', 'reliable_validation_report.json', 'reliable_model_card.json'):
        raw = json.dumps(payload).encode()
        (path / name).write_bytes(raw)
        checksums[name] = hashlib.sha256(raw).hexdigest()
    receipt = {'schema_version': 'reliable_publication_v1', 'status': 'research_only', 'run_id': cohort,
               'replay': False, 'publication_checked_at': '2026-11-04T09:01:00Z', 'payload_checksums': checksums}
    (path / 'reliable_publication.json').write_text(json.dumps(receipt))
    return payload


def target_frame(close=132, seen='2026-11-05T06:40:00Z'):
    return pd.DataFrame([{'asof_date': '2026-11-04', 'target_date': '2026-11-05', 'label_return': .32,
        'label_target_close': close, 'label_observed_at': seen, 'target_close_at': '2026-11-05T06:30:00Z',
        'label_available_at': '2026-11-05T06:30:00Z'}])


def test_actual_forward_scores_issued_baseline_per_role_and_cohort(tmp_path):
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    out = tmp_path / 'public'
    for cohort, baseline in [('one', 120), ('two', 110)]:
        protocol = {**PROTOCOL, 'candidate': cohort}
        ledger.register_protocol(protocol, registered_at='2026-11-04T08:59:00Z')
        issued(out, baseline, cohort=cohort, reference_baseline=100)
        ledger.record_publication(out, protocol=protocol, attempted_at='2026-11-04T09:02:00Z')
    finish_forward(out, evidence=(ledger, protocol), frames={'KOSPI': target_frame()}, observed_at='2026-11-05T06:39:00Z')
    assert ledger.export_history()['outcomes'] == []
    finish_forward(out, evidence=(ledger, protocol), frames={'KOSPI': target_frame()}, observed_at='2026-11-05T09:00:00Z')
    history = ledger.export_history()
    assert len(history['outcomes']) == 4
    assert sorted(row['actual_return'] for row in history['outcomes']) == pytest.approx([.1, .2, .32, .32])
    assert {row['issued_baseline_close'] for row in history['forecasts']} == {100, 110, 120}
    original = history['outcomes']
    finish_forward(out, evidence=(ledger, protocol), frames={'KOSPI': target_frame(150)}, observed_at='2026-11-06T09:00:00Z')
    assert ledger.export_history()['outcomes'] == original
    restored = ForwardLedger(tmp_path / 'restore.sqlite')
    restored.import_history(ledger.export_history(), trusted_archive=True)
    assert restored.export_history() == ledger.export_history()
    assert {row['mature_n'] for row in restored.report()['cohorts']} == {1}


def test_postclose_missing_target_basis_abstains_instead_of_scoring_historical_label(tmp_path):
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    ledger.register_protocol(PROTOCOL, registered_at='2026-11-04T08:59:00Z')
    issued(tmp_path / 'public')
    frame = target_frame().drop(columns=['label_target_close', 'label_observed_at'])
    finish_forward(tmp_path / 'public', evidence=(ledger, PROTOCOL), frames={'KOSPI': frame}, observed_at='2026-11-05T09:00:00Z')
    assert ledger.export_history()['outcomes'] == []


def test_same_day_revision_never_changes_historical_close_or_prior_label(tmp_path):
    path = tmp_path / 'prices.sqlite'
    with connect_index_forecast(path) as db:
        db.executemany('INSERT INTO index_eod(index_code,session_date,close,source,ingested_at) VALUES(?,?,?,?,?)',
            [('KOSPI', day, 5000, 'first', day+'T07:00:00Z') for day in ['2026-11-13', '2026-11-16']])
        db.commit()
        record_observation(db, {'index_code': 'KOSPI', 'session_date': '2026-11-16', 'close': 5100,
            'source': 'revision', 'received_at': '2026-11-16T09:00:00Z', 'status': 'verified'})
    frames = [effective_index_eod_frame(path, cutoff=t) for t in ['2026-11-16T09:05:00Z', '2026-11-17T09:05:00Z']]
    for frame in frames:
        assert frame.close.tolist() == [5000, 5000]
        history = build_reliable_price_frame(frame, sessions=['2026-11-13', '2026-11-16', '2026-11-17'])
        assert history.iloc[0].label_return == 0
        assert apply_live_revisions(frame).iloc[-1].close == 5100


def test_late_postclose_preserves_verified_payloads_and_records_separate_attempt(tmp_path):
    output = tmp_path / 'public'
    issued(output)
    original = {p.name: p.read_bytes() for p in output.glob('*.json')}
    run = {'run_id': 'late', 'issuance_protocol': PROTOCOL['protocol'], 'run_started_at': '2026-11-04T09:00:00Z',
           'prepared_at': '2026-11-04T09:01:00Z', 'markets': []}
    docs = {'archive/late.json': run, 'reliable_index_forecast.json': run}
    result = _publish_staged_run(run, output, docs, contexts={'KOSPI': {'issuance_deadline': '2000-01-01T00:00:00Z'}}, replay=False)
    assert result['status'] == 'blocked_issuance_deadline'
    assert all((output / name).read_bytes() == raw for name, raw in original.items())
    assert json.loads((output / 'postclose_attempt_status.json').read_text())['status'] == 'blocked_issuance_deadline'


def test_same_actions_run_new_attempt_freezes_new_bytes_after_checkpoint_restore(tmp_path):
    import shutil
    from test_automation_cli_integration import Checkout
    from roboquant.index_forecast.market_closes import write_market_closes
    checkout = Checkout(tmp_path / 'checkout')
    us = checkout.seed()
    write_market_closes({'schema_version': 'market_closes_v1', 'run_id': 'first', 'markets': {}},
        checkout.root / 'docs/data/market_closes.json', published_at='2026-11-16T09:01:00Z')
    completed = {'records': [{'issuance_protocol': PROTOCOL['protocol'], 'market': 'US_SPX', 'target_date': '2026-11-16'}]}
    (checkout.root / 'docs/data/postclose_completions.json').write_text(json.dumps(completed))
    def freeze(attempt, now):
        return json.loads(checkout.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'postclose',
            '--sqlite', checkout.private / 'index_forecast.sqlite', '--us-csv', us,
            now=now, workflow_run_id=12345, workflow_attempt=attempt).stdout.splitlines()[-1])
    first = freeze(1, '2026-11-16T09:02:00+00:00')
    old_pointer = json.loads((checkout.private / 'postclose-prepared.json').read_text())
    old_manifest = (checkout.private / 'postclose-prepared.json').read_bytes()
    assert checkout.operation('cache-seal', mode='postclose', now='2026-11-16T09:03:00+00:00')['status'] == 'sealed'
    shutil.rmtree(checkout.private)
    assert checkout.operation('cache-open', mode='postclose', now='2026-11-16T09:10:00+00:00')['status'] == 'restored'
    with sqlite3.connect(checkout.private / 'index_forecast.sqlite') as db:
        db.execute("INSERT INTO index_eod(index_code,session_date,close,source,ingested_at) VALUES('KOSPI','2026-11-16',5100.5,'new-receipt','2026-11-16T09:10:00Z')")
    second = freeze(2, '2026-11-16T09:12:00+00:00')
    assert first['manifest_hash'] != second['manifest_hash']
    assert second['prepared_at'] == '2026-11-16T09:12:00+00:00'
    assert (checkout.private / 'postclose-prepared.json').read_bytes() != old_manifest
    from pathlib import Path
    assert json.loads((Path(old_pointer['directory']) / 'manifest.json').read_text())['manifest_hash'] == first['manifest_hash']
    inputs = checkout.operation('inputs', mode='postclose', now='2026-11-16T09:13:00+00:00')
    with sqlite3.connect(inputs['sqlite']) as db:
        assert db.execute("SELECT close FROM index_eod WHERE index_code='KOSPI' AND session_date='2026-11-16'").fetchone()[0] == 5100.5
    assert 'US_SPX' not in inputs['markets'].split(',')
    assert 'US_NASDAQ' in inputs['markets'].split(',')


def test_per_issued_basis_changes_direction_and_keeps_legacy_outcome_immutable(tmp_path):
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    out = tmp_path / 'public'
    # An existing legacy outcome remains untouched, even for the same dates.
    legacy = {'protocol': 'legacy'}
    ledger.register_protocol(legacy, registered_at='2026-11-04T08:59:00Z')
    payload = issued(out, baseline=100, cohort='legacy')
    payload['markets'][0]['context']['stage'] = 'premarket_final'
    raw = json.dumps(payload).encode()
    (out / 'reliable_index_forecast.json').write_bytes(raw)
    receipt = json.loads((out / 'reliable_publication.json').read_text())
    receipt['payload_checksums']['reliable_index_forecast.json'] = hashlib.sha256(raw).hexdigest()
    (out / 'reliable_publication.json').write_text(json.dumps(receipt))
    ledger.record_publication(out, protocol=legacy, attempted_at='2026-11-04T09:02:00Z')
    observation = {'market': 'KOSPI', 'asof_date': '2026-11-04', 'target_date': '2026-11-05',
        'actual_return': .32, 'target_close_at': '2026-11-05T06:30:00Z', 'label_available_at': '2026-11-05T06:30:00Z'}
    ledger.record_outcomes([observation], observed_at='2026-11-05T09:00:00Z')
    old_outcome = ledger.export_history()['outcomes'][0]
    for baseline in (100, 120):
        protocol = {**PROTOCOL, 'baseline_fixture': baseline}
        ledger.register_protocol(protocol, registered_at='2026-11-04T08:59:00Z')
        issued(out, baseline, cohort=str(baseline))
        ledger.record_publication(out, protocol=protocol, attempted_at='2026-11-04T09:02:00Z')
    finish_forward(out, evidence=(ledger, protocol), frames={'KOSPI': target_frame(110)}, observed_at='2026-11-05T09:00:00Z')
    assert ledger.export_history()['outcomes'][0] == old_outcome
    cohorts = [r for r in ledger.report()['cohorts'] if r['protocol_hash'] != ledger.export_history()['protocols'][0]['protocol_hash']]
    assert sorted(r['metrics']['brier'] for r in cohorts) == pytest.approx([.16, .36])
    assert sorted(r['actual_return'] for r in ledger.export_history()['outcomes'][1:]) == pytest.approx([110/120-1, .1])


def test_postclose_publication_without_baseline_never_scores_reconstruction(tmp_path):
    ledger = ForwardLedger(tmp_path / 'ledger.sqlite')
    ledger.register_protocol(PROTOCOL, registered_at='2026-11-04T08:59:00Z')
    issued(tmp_path / 'public', baseline=None)
    finish_forward(tmp_path / 'public', evidence=(ledger, PROTOCOL), frames={'KOSPI': target_frame()}, observed_at='2026-11-05T09:00:00Z')
    assert ledger.export_history()['outcomes'] == []
    assert ledger.export_history()['forecasts'] == []


def test_unverified_compact_files_excluded_while_independent_market_can_publish(tmp_path):
    from roboquant.index_forecast.automation_state import public_payload_paths
    issued(tmp_path, cohort='kr')
    issued(tmp_path / 'us', cohort='us')
    (tmp_path / 'reliable_index_forecast.json').write_text('{"issuance_protocol":"after_close_daily_v1","status":"blocked"}')
    paths = public_payload_paths(tmp_path)
    assert tmp_path / 'reliable_index_forecast.json' not in paths
    assert tmp_path / 'reliable_publication.json' not in paths
    assert tmp_path / 'us/reliable_index_forecast.json' in paths
    assert tmp_path / 'us/reliable_publication.json' in paths

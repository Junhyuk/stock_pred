"""Offline subprocess tests for scheduled preparation, cache and final issuance."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from roboquant.index_forecast.sqlite_store import connect_index_forecast
from roboquant.index_forecast.us_market_inputs import (
    US_SYMBOLS,
    normalize_us_observations,
    us_close_schedule,
)

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = Path(__file__).with_name('automation_cli_harness.py')
TEST_KEY = base64.b64encode(b'test-only-cache-key-32-bytes!!!!!'[:32].ljust(32, b'!')).decode()
PREPARE = '2026-11-15T22:53:00+00:00'  # Monday07:53 KST
PREDICT = '2026-11-15T23:01:00+00:00'


class Checkout:
    def __init__(self, root):
        self.root = root
        self.private = root / 'data/processed'
        self.private.mkdir(parents=True)
        for directory in ('src', 'scripts', 'configs'):
            shutil.copytree(ROOT / directory, root / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (root / 'docs/data').mkdir(parents=True)
        (root / 'provider.json').write_text(json.dumps({
            '^KS11': [['2026-11-13', 2500.]], '^KQ11': [['2026-11-13', 800.]],
            **{symbol: [['2026-11-12', 999.], ['2026-11-13', 120.]] for symbol in US_SYMBOLS}}))

    def run(self, script, *args, now=PREPARE, scenario='normal', key=TEST_KEY, check=True, workflow_run_id=None, workflow_attempt=None):
        # Never inherit real credentials, .env, PYTHONPATH or provider settings.
        env = {name: value for name, value in os.environ.items()
               if name in {'PATH', 'HOME', 'LANG', 'LD_LIBRARY_PATH', 'VIRTUAL_ENV'}}
        env.update(FORECAST_CACHE_KEY=key, PYTHONDONTWRITEBYTECODE='1',
                   GITHUB_OUTPUT=str(self.root / 'github-output'),
                   OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1')
        if workflow_run_id is not None:
            env.update(GITHUB_RUN_ID=str(workflow_run_id), GITHUB_RUN_ATTEMPT=str(workflow_attempt or 1))
        completed = subprocess.run([sys.executable, str(LAUNCHER), str(self.root), now,
                                   scenario, script, *map(str, args)], cwd=self.root,
                                  env=env, capture_output=True, text=True, timeout=90)
        if check:
            assert completed.returncode == 0, completed.stderr
        return completed

    def operation(self, phase, mode='predict', **kwargs):
        return json.loads(self.run('accuracy_operation.py', '--phase', phase, '--mode', mode,
                                  **kwargs).stdout.splitlines()[-1])

    def seed(self, *, day='20261114', covered=True):
        with connect_index_forecast(self.private / 'index_forecast.sqlite') as db:
            db.executemany('INSERT OR IGNORE INTO index_eod '
                '(index_code,session_date,close,source,ingested_at) VALUES (?,?,?,?,?)',
                [(code, '2026-11-13', value, 'offline fixture', '2026-11-13T16:30:00+00:00')
                 for code, value in [('KOSPI', 2500.), ('KOSDAQ', 800.)]])
        schedule = us_close_schedule('2019-01-02', '2026-11-12')
        days = sorted(schedule.session_date.unique())[:1000] if covered else []
        days += ['2026-11-12']
        raw = pd.DataFrame([{'symbol': symbol, 'session_date': date, 'close': 100.,
                             'source': 'offline fixture', 'source_url': 'https://example.test/fixture',
                             'fetched_at': '2026-11-13T16:30:00+00:00'}
                            for symbol in US_SYMBOLS for date in days])
        path = self.private / f'us_market_observations_v2_{day}.csv'
        normalize_us_observations(raw)['observations'].to_csv(path, index=False)
        return path

    def refresh(self, **kwargs):
        return json.loads(self.run('refresh_premarket_prices.py', **kwargs).stdout.splitlines()[-1])

    def freeze(self, prices, **kwargs):
        return json.loads(self.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'prepare',
            '--sqlite', prices['sqlite'], '--us-csv', prices['us_csv'], **kwargs).stdout.splitlines()[-1])


@pytest.fixture
def checkout(tmp_path):
    return Checkout(tmp_path / 'checkout')


def test_actual_gate_cli_runs_before_third_party_dependencies_are_installed(checkout):
    result = subprocess.run([sys.executable, '-S', 'scripts/accuracy_operation.py',
                             '--phase', 'gate', '--schedule', '50 22 * * 0-4'],
                            cwd=checkout.root, env={'PATH': os.environ.get('PATH', '')},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    decision = json.loads(result.stdout)
    assert decision['mode'] == 'postclose'
    assert isinstance(decision['allowed'], bool)
    assert decision['forecast_requested'] is decision['allowed']
    assert isinstance(decision['collect'], bool)


@pytest.mark.parametrize('mode', ['latest', 'daily'])
def test_actual_cli_fits_with_fractional_clock_and_frozen_daily_cutoff(checkout, mode):
    """Unlike short-history smoke, execute walk-forward snapshot restoration."""
    import yaml

    from roboquant.index_forecast.reliable_inputs import session_schedule

    now = '2026-11-14T01:42:47.385886+00:00' if mode == 'latest' else '2026-11-15T23:01:00.385886+00:00'
    days = session_schedule('2026-04-01', '2026-11-13')[0]['date'].tolist()
    fixture = json.loads((checkout.root / 'provider.json').read_text())
    fixture.update({symbol: [[day, 1000. + i * 2 + (i % 5)] for i, day in enumerate(days)]
                    for symbol in ('^KS11', '^KQ11')})
    (checkout.root / 'provider.json').write_text(json.dumps(fixture))
    # Smaller test-only training/bootstrap budgets exercise actual fitting with
    # deterministic fixtures. Production settings and acceptance stay unchanged.
    config_path = checkout.root / 'configs/reliable_index_forecast.yaml'
    config = yaml.safe_load(config_path.read_text())
    config['us_market']['enabled'] = False
    config['settings'].update(initial_train_days=25, calibration_window=30,
        min_calibration_rows=10, development_days=25, test_days=25,
        bootstrap_samples=20, bootstrap_block_days=5, min_oos_rows=15, model_families=['linear'])
    config_path.write_text(yaml.safe_dump(config))
    prices = checkout.refresh(now=now if mode == 'latest' else PREPARE)
    arguments = []
    if mode == 'daily':
        assert checkout.freeze(prices)['status'] == 'prepared'
        inputs = checkout.operation('inputs', 'daily', now=now)
        assert inputs['status'] == 'ready' and inputs['kr']
        prices = inputs
        arguments = ['--prepared-manifest', inputs['prepared_manifest'],
                     '--economic-sqlite', inputs['economic_sqlite'],
                     '--fit-state-dir', inputs['fit_models_kr']]
    completed = checkout.run('run_reliable_index_forecast.py', '--sqlite', prices['sqlite'], *arguments, now=now)
    result = json.loads(completed.stdout.splitlines()[-1])
    assert result['status'] == 'research_only' and result['issued_at'] == now
    archive = json.loads((checkout.root / 'docs/data/archive' / f"{result['run_id']}.json").read_text())
    assert archive['production_weight'] == 0 and archive['replay'] is False
    for market in archive['markets']:
        observations = market['evaluation']['candidate_observations']['linear:price']
        assert observations and all(row['fit_snapshot_hash'] for row in observations)
    log = checkout.root / 'fractional-clock-result.json'
    log.write_text(completed.stdout)
    checkout.run('verify_automation_publication.py', '--result', log, '--data-dir', 'docs/data', now=now)


@pytest.mark.parametrize('covered', [True, False], ids=['incremental', 'short-history-backfill'])
def test_same_kst_day_refresh_after_us_close_keeps_prior_snapshot_and_receipts(checkout, covered):
    """A date-only destination crashed prepare after a pre-US-close latest run."""
    old = checkout.seed(covered=covered)
    original = old.read_bytes()
    prices = checkout.refresh(now='2026-11-13T22:53:00+00:00')
    new = checkout.root / prices['us_csv']
    assert new != old
    assert old.read_bytes() == original
    frame = pd.read_csv(new)
    assert len(frame) == (1002 if covered else 2) * 4
    assert set(frame.loc[frame.session_date == '2026-11-12', 'close']) == {100.}
    assert set(frame.loc[frame.session_date == '2026-11-12', 'first_seen_at']) == {'2026-11-13T16:30:00+00:00'}
    assert set(frame.loc[frame.session_date == '2026-11-13', 'close']) == {120.}
    assert set(frame.loc[frame.session_date == '2026-11-13', 'first_seen_at']) == {'2026-11-13T22:53:00+00:00'}
    repeated = checkout.refresh(now='2026-11-13T22:54:00+00:00')
    assert repeated['us_csv'] == prices['us_csv']
    assert len(list(checkout.private.glob('us_market_observations_v2_*.csv'))) == 2


def test_versioned_snapshot_survives_cache_pruning_rerun_and_next_day(checkout):
    checkout.seed()
    prices = checkout.refresh(now='2026-11-13T22:53:00+00:00')
    expected = (checkout.root / prices['us_csv']).read_bytes()
    indicators = checkout.private / 'robo_session_inputs.sqlite'
    with sqlite3.connect(indicators) as db:
        db.execute('CREATE TABLE session_input_observations (value REAL)')
        db.execute('INSERT INTO session_input_observations VALUES (4.62)')
    assert checkout.operation('cache-seal', 'prepare', now=PREPARE)['status'] == 'sealed'
    assert len(list(checkout.private.glob('us_market_observations_v2_*.csv'))) == 1
    assert b'offline fixture' not in (checkout.root / '.forecast-cache/state.enc').read_bytes()
    shutil.rmtree(checkout.private)
    assert checkout.operation('cache-open', 'prepare')['status'] == 'restored'
    assert (checkout.root / prices['us_csv']).read_bytes() == expected
    with sqlite3.connect(indicators) as db:
        assert db.execute('SELECT value FROM session_input_observations').fetchall() == [(4.62,)]
    assert checkout.refresh(now='2026-11-13T22:55:00+00:00')['us_csv'] == prices['us_csv']
    following = checkout.refresh(now=PREPARE)
    frame = pd.read_csv(checkout.root / following['us_csv'])
    assert len(frame) == 4008
    assert set(frame.loc[frame.session_date == '2026-11-13', 'first_seen_at']) == {'2026-11-13T22:53:00+00:00'}
    # A new day and another receipt version must still merge from the newest
    # cache, retaining the original close despite a provider revision.
    fixture = json.loads((checkout.root / 'provider.json').read_text())
    for symbol in US_SYMBOLS:
        fixture[symbol] += [['2026-11-16', 130.]]
    (checkout.root / 'provider.json').write_text(json.dumps(fixture))
    next_day = checkout.refresh(now='2026-11-16T22:53:00+00:00')
    next_frame = pd.read_csv(checkout.root / next_day['us_csv'])
    assert len(next_frame) == 4012
    assert set(next_frame.loc[next_frame.session_date == '2026-11-12', 'close']) == {100.}


def test_changed_data_at_the_same_receipt_cannot_overwrite_version(checkout):
    checkout.seed(covered=False)
    now = '2026-11-13T22:53:00+00:00'
    prices = checkout.refresh(now=now)
    path = checkout.root / prices['us_csv']
    before = path.read_bytes()
    assert checkout.refresh(now=now)['us_csv'] == prices['us_csv']
    fixture = json.loads((checkout.root / 'provider.json').read_text())
    for symbol in US_SYMBOLS:
        fixture[symbol].append(['2026-11-11', 90.])
    (checkout.root / 'provider.json').write_text(json.dumps(fixture))
    collision = checkout.run('refresh_premarket_prices.py', now=now, check=False)
    assert collision.returncode != 0 and 'US snapshot is immutable' in collision.stderr
    assert path.read_bytes() == before


def _collect(checkout, **kwargs):
    return json.loads(checkout.run('collect_economic_calendar.py', **kwargs).stdout.splitlines()[-1])


def _prepared(checkout, *, now=PREPARE):
    checkout.seed(covered=False)
    prices = checkout.refresh(now=now)
    frozen = checkout.freeze(prices, now=now)
    assert frozen['status'] == 'prepared'
    return prices, checkout.private / 'prepared' / ('2026-11-14' if now.startswith('2026-11-13') else '2026-11-16')


@pytest.mark.parametrize('mode', ['predict', 'daily'])
def test_prepare_to_predict_runs_real_collectors_forecasts_and_publication_checks(checkout, mode):
    # Use real exchange sessions and deterministic local prices. Keep production
    # settings: this short history must remain model-not-ready, weight zero.
    from roboquant.index_forecast.reliable_inputs import session_schedule
    kr_days = session_schedule('2026-04-01', '2026-11-13')[0]['date'].tolist()
    us_days = sorted(us_close_schedule('2026-04-01', '2026-11-13').session_date.unique())
    fixture = {symbol: [[day, 100. + i * .1 + (i % 7) * .2] for i, day in enumerate(us_days)]
               for symbol in US_SYMBOLS}
    fixture.update({symbol: [[day, 1000. + i * 2 + (i % 5)] for i, day in enumerate(kr_days)]
                    for symbol in ('^KS11', '^KQ11')})
    (checkout.root / 'provider.json').write_text(json.dumps(fixture))
    assert checkout.operation('gate', 'prepare')['collect'] is True
    assert checkout.operation('cache-open', 'prepare')['reason'] == 'encrypted_cache_unavailable'
    collected = _collect(checkout)
    assert collected['release_count'] == 2 and collected['status'] == 'collected'
    prices = checkout.refresh()
    frozen = checkout.freeze(prices)
    assert frozen['status'] == 'prepared'
    manifest_path = checkout.private / 'prepared/2026-11-16/manifest.json'
    manifest_bytes = manifest_path.read_bytes()
    receipt_bytes = manifest_path.with_name('completion.json').read_bytes()
    assert checkout.operation('cache-seal', 'prepare')['status'] == 'sealed'
    shutil.rmtree(checkout.private)
    assert checkout.operation('cache-open', now=PREDICT)['status'] == 'restored'
    gate = checkout.operation('gate', mode, now=PREDICT)
    assert gate['forecast_requested'] is True and gate['collect'] is False
    inputs = checkout.operation('inputs', mode, now=PREDICT)
    assert inputs['status'] == 'ready' and inputs['kr'] is True and inputs['us'] is True
    assert manifest_path.read_bytes() == manifest_bytes
    assert manifest_path.with_name('completion.json').read_bytes() == receipt_bytes
    for market, script, output in [('kr', 'run_reliable_index_forecast.py', 'docs/data'),
                                   ('us', 'run_us_index_forecast.py', 'docs/data/us')]:
        args = ['--us-observations', inputs['us_csv'], '--prepared-manifest', inputs['prepared_manifest'],
                '--economic-sqlite', inputs['economic_sqlite'], '--fit-state-dir', inputs[f'fit_models_{market}']]
        if market == 'kr':
            args += ['--sqlite', inputs['sqlite']]
        completed = checkout.run(script, *args, now=PREDICT)
        report = json.loads(completed.stdout.splitlines()[-1])
        assert report['status'] == 'research_only'
        assert report['issued_at'] == PREDICT
        assert all(row['status'] == 'not_ready' for row in report['markets'])
        log = checkout.root / f'{market}-result.json'
        log.write_text(completed.stdout)
        verified = checkout.run('verify_automation_publication.py', '--result', log, '--data-dir', output, now=PREDICT)
        assert 'Verified live publication:' in verified.stdout
        public = json.loads((checkout.root / output / 'reliable_index_forecast.json').read_text())
        assert public['production_weight'] == 0 and public['replay'] is False
        receipt = json.loads((checkout.root / output / 'reliable_publication.json').read_text())
        assert receipt['publication_checked_at'] == PREDICT
        assert receipt['payload_checksums']['reliable_index_forecast.json'] == hashlib.sha256(
            (checkout.root / output / 'reliable_index_forecast.json').read_bytes()).hexdigest()
    assert checkout.operation('publish-models', mode, now=PREDICT)['status'] == 'persisted_derived_model_state'
    status = checkout.operation('status', mode, now=PREDICT)
    assert status['korea'] is not None and status['us'] is not None
    assert status['daily_completed_date'] == '2026-11-16'
    assert not checkout.operation('gate', 'daily', now=PREDICT)['allowed']
    public_paths = checkout.run('accuracy_operation.py', '--phase', 'public-paths', '--mode', 'daily', now=PREDICT).stdout.splitlines()
    assert 'docs/data/reliable_publication.json' in public_paths
    assert all(not path.endswith(('.sqlite', '.csv', '.enc')) for path in public_paths)
    replay = checkout.run('run_reliable_index_forecast.py', '--sqlite', inputs['sqlite'],
        '--us-observations', inputs['us_csv'], '--output-dir', 'replay-only', '--now', PREDICT,
        now='2026-11-16T01:00:00+00:00')
    replay_log = checkout.root / 'replay-result.json'
    replay_log.write_text(replay.stdout)
    rejected = checkout.run('verify_automation_publication.py', '--result', replay_log,
                            '--data-dir', 'replay-only', check=False)
    assert rejected.returncode != 0
    assert json.loads((checkout.root / 'replay-only/reliable_publication.json').read_text())['replay'] is True


@pytest.mark.parametrize('clock,allowed', [
    ('2026-11-16T09:00:00+00:00', True), ('2026-11-17T09:00:00+00:00', True),
    ('2026-11-18T09:00:00+00:00', True), ('2026-11-19T09:00:00+00:00', True),
    ('2026-11-20T09:00:00+00:00', True), ('2026-11-14T09:00:00+00:00', False),
    ('2026-11-15T09:00:00+00:00', False)])
def test_scheduled_gate_uses_single_daily_mode_on_kst_weekdays(checkout, clock, allowed):
    for schedule, mode in [('0 9 * * 1-5', 'postclose')]:
        data = json.loads(checkout.run('accuracy_operation.py', '--phase', 'gate', '--schedule', schedule,
                                      now=clock).stdout.splitlines()[-1])
        assert data['allowed'] is allowed and data['mode'] == mode
        assert data['collect'] is allowed
        assert data['forecast_requested'] is allowed


def test_saturday_cannot_prepare_or_issue_monday_premarket_final(checkout):
    result = checkout.operation('freeze', 'prepare', now='2026-11-13T22:53:00+00:00')
    assert result['allowed'] is False
    result = checkout.operation('inputs', now='2026-11-13T23:01:00+00:00')
    assert result['allowed'] is False
    assert not (checkout.root / 'docs/data/reliable_publication.json').exists()


def test_missing_early_stale_changed_and_unreceipted_preparations_abstain(checkout):
    assert checkout.operation('inputs', now=PREDICT)['status'] == 'not_ready'
    prices, directory = _prepared(checkout)
    assert checkout.operation('inputs', now=PREPARE)['status'] == 'not_ready'
    assert checkout.operation('inputs', now='2026-11-16T23:01:00+00:00')['status'] == 'not_ready'
    assert checkout.operation('inputs', now=PREDICT)['status'] == 'ready'
    after_open = checkout.operation('inputs', now='2026-11-16T00:01:00+00:00')
    assert not after_open['kr'] and not after_open['us']
    manifest = json.loads((directory / 'manifest.json').read_text())
    original_manifest = (directory / 'manifest.json').read_bytes()
    original_receipt = (directory / 'completion.json').read_bytes()
    # Rerunning prepare cannot replace the first manifest or its completion time.
    assert checkout.freeze(prices, now='2026-11-15T22:54:00+00:00')['status'] == 'prepared'
    assert (directory / 'manifest.json').read_bytes() == original_manifest
    assert (directory / 'completion.json').read_bytes() == original_receipt
    (directory / 'completion.json').unlink()
    result = checkout.operation('inputs', now=PREDICT)
    assert result['status'] == 'not_ready' and not result['kr'] and not result['us']
    (directory / 'completion.json').write_bytes(original_receipt)
    receipt = json.loads(original_receipt)
    receipt['completed_at'] = PREDICT
    (directory / 'completion.json').write_text(json.dumps(receipt))
    assert 'receipt mismatch' in checkout.operation('inputs', now=PREDICT)['reason']
    (directory / 'completion.json').write_bytes(original_receipt)
    script = checkout.root / 'scripts/accuracy_operation.py'
    source = script.read_bytes()
    script.write_bytes(source + b'\n# changed between preparation and prediction\n')
    assert 'source protocol mismatch' in checkout.operation('inputs', now=PREDICT)['reason']
    script.write_bytes(source)
    frozen_csv = directory / manifest['files']['us_csv']['path']
    frozen_csv.write_bytes(frozen_csv.read_bytes() + b'tamper')
    assert 'hash mismatch' in checkout.operation('inputs', now=PREDICT)['reason']
    assert not (checkout.root / 'docs/data/reliable_publication.json').exists()


def test_late_preparation_cannot_be_backdated_by_production_cli(checkout):
    checkout.seed(covered=False)
    prices = checkout.refresh()
    result = checkout.freeze(prices, now=PREDICT)
    assert result['status'] == 'not_ready' and '08:00' in result['reason']
    assert not list((checkout.private / 'prepared').glob('*/manifest.json'))
    rejected = checkout.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'prepare',
                            '--now', PREPARE, now=PREDICT, check=False)
    assert rejected.returncode != 0 and 'unrecognized arguments: --now' in rejected.stderr


@pytest.mark.parametrize('failure', ['missing', 'no-key', 'wrong-key', 'tampered'])
def test_cache_failure_abstains_without_plaintext_or_forecast(checkout, failure):
    if failure in {'wrong-key', 'tampered'}:
        checkout.seed(covered=False)
        checkout.operation('cache-seal', 'prepare')
        shutil.rmtree(checkout.private)
    if failure == 'tampered':
        path = checkout.root / '.forecast-cache/state.enc'
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
    key = '' if failure == 'no-key' else base64.b64encode(b'x' * 32).decode() if failure == 'wrong-key' else TEST_KEY
    result = checkout.operation('cache-open', now=PREDICT, key=key)
    assert result['status'] == 'not_ready'
    inputs = checkout.operation('inputs', now=PREDICT)
    assert not inputs['kr'] and not inputs['us']
    assert not (checkout.private / 'index_forecast.sqlite').exists()


@pytest.mark.parametrize('scenario', ['unavailable', 'timeout'])
def test_calendar_provider_failure_keeps_prior_public_evidence_and_price_path(checkout, scenario):
    prior = checkout.root / 'docs/data/economic_calendar.json'
    prior.write_text('{"previous_evidence":true}')
    result = _collect(checkout, scenario=scenario)
    assert result['status'] == 'unavailable'
    if scenario == 'timeout':
        assert result['reason'] == 'hard_collection_deadline'
    assert json.loads(prior.read_text()) == {'previous_evidence': True}
    checkout.seed(covered=False)
    assert checkout.freeze(checkout.refresh())['status'] == 'prepared'


def test_calendar_history_survives_encrypted_restore_and_partial_source_retry(checkout):
    assert _collect(checkout)['release_count'] == 2
    history_path = checkout.root / 'docs/data/economic_history.json'
    before = json.loads(history_path.read_text())
    assert checkout.operation('cache-seal', 'prepare')['status'] == 'sealed'
    shutil.rmtree(checkout.private)
    assert checkout.operation('cache-open', 'prepare')['status'] == 'restored'
    partial = _collect(checkout, scenario='partial', now='2026-11-15T22:54:00+00:00')
    assert partial['status'] == 'partial' and partial['release_count'] == 2
    after = json.loads(history_path.read_text())
    assert after['release_versions'] == before['release_versions']
    assert len(after['collection_runs']) > len(before['collection_runs'])


def _verified_day(checkout, now=PREDICT, mode='daily'):
    date = pd.Timestamp(now).tz_convert('Asia/Seoul').date().isoformat()
    data = checkout.root / 'docs/data'
    forecast = data / 'reliable_index_forecast.json'
    forecast.write_text(json.dumps({'run_id': 'verified-test-run', 'markets': []}))
    (data / 'reliable_publication.json').write_text(json.dumps({
        'schema_version': 'reliable_publication_v1', 'run_id': 'verified-test-run',
        'publication_checked_at': now, 'payload_checksums': {
            forecast.name: hashlib.sha256(forecast.read_bytes()).hexdigest()}}))
    inputs = {'status': 'ready', 'kr': True, 'us': False, 'prepared_at':
        (pd.Timestamp(now) - pd.Timedelta(minutes=10)).isoformat(), 'prepared_manifest_hash': 'test'}
    (checkout.private / 'accuracy-input-status.json').write_text(json.dumps(inputs))
    return checkout.operation('status', mode, now=now), date


@pytest.mark.parametrize('now', [PREDICT, '2026-11-19T23:01:00.385886+00:00'])
def test_daily_gate_collects_once_then_records_durable_duplicate_guard(checkout, now):
    early = (pd.Timestamp(now) - pd.Timedelta(minutes=10)).isoformat()
    gate = checkout.operation('gate', 'daily', now=early)
    assert gate['allowed'] and gate['collect'] and gate['forecast_requested']
    status, date = _verified_day(checkout, now)
    assert status['daily_completed_date'] == date
    repeated = checkout.operation('gate', 'daily', now=now)
    assert not repeated['allowed'] and not repeated['collect'] and not repeated['forecast_requested']
    assert repeated['reason'] == 'daily_already_completed'
    checkout.operation('status', 'latest', now=now)
    assert not checkout.operation('gate', 'daily', now=now)['allowed']


def test_legacy_predict_completion_blocks_daily_but_missing_or_unverified_does_not(checkout):
    status, date = _verified_day(checkout, mode='predict')
    status.pop('daily_completed_date', None)
    path = checkout.root / 'docs/data/automation_status.json'
    path.write_text(json.dumps(status))
    assert not checkout.operation('gate', 'daily', now=PREDICT)['allowed']
    checkout.operation('status', 'latest', now=PREDICT)
    assert not checkout.operation('gate', 'daily', now=PREDICT)['allowed']
    status['inputs'] = {'status': 'not_ready', 'kr': False, 'us': False}
    path.write_text(json.dumps(status))
    assert checkout.operation('gate', 'daily', now=PREDICT)['allowed']
    status['inputs'] = {'status': 'ready', 'kr': True, 'us': False, 'prepared_at': PREPARE}
    path.write_text(json.dumps(status))
    (checkout.root / 'docs/data/reliable_index_forecast.json').write_text('tampered')
    assert checkout.operation('gate', 'daily', now=PREDICT)['allowed']


@pytest.mark.parametrize('now', ['2026-11-13T22:53:00+00:00', '2026-11-14T22:53:00.385886+00:00'])
def test_weekend_cli_blocks_all_operational_mutations_and_public_paths(checkout, now):
    for mode in ('daily', 'prepare', 'predict', 'latest'):
        gate = checkout.operation('gate', mode, now=now)
        assert not gate['allowed'] and not gate['collect'] and not gate['forecast_requested']
        for phase in ('freeze', 'inputs', 'cache-open', 'cache-seal', 'publish-models', 'status'):
            result = checkout.operation(phase, mode, now=now)
            assert result['allowed'] is False
    (checkout.root / 'docs/data/automation_status.json').write_text('{}')
    assert checkout.run('accuracy_operation.py', '--phase', 'public-paths', now=now).stdout == ''
    assert checkout.operation('gate', 'validate', now=now)['allowed'] is True
    assert not (checkout.root / '.forecast-cache').exists()


def test_daily_late_start_does_not_collect_or_fabricate_preparation(checkout):
    gate = checkout.operation('gate', 'daily', now=PREDICT)
    assert gate['allowed'] and not gate['collect'] and gate['forecast_requested']
    result = checkout.operation('inputs', 'daily', now=PREDICT)
    assert result['status'] == 'not_ready' and not result['kr'] and not result['us']
    assert not (checkout.root / 'docs/data/reliable_publication.json').exists()
    status = checkout.operation('status', 'daily', now=PREDICT)
    assert status.get('daily_completed_date') is None
    assert checkout.operation('gate', 'daily', now=PREDICT)['allowed']


def test_daily_wait_advances_actual_clock_then_uses_frozen_receipts(checkout):
    prices, directory = _prepared(checkout)
    before = (directory / 'manifest.json').read_bytes()
    receipt = (directory / 'completion.json').read_bytes()
    waited = checkout.operation('wait', 'daily', now=PREPARE)
    assert waited['status'] == 'ready'
    assert waited['checked_at'] == '2026-11-15T23:00:00+00:00'
    inputs = checkout.operation('inputs', 'daily', now=PREDICT)
    assert inputs['status'] == 'ready' and inputs['kr'] and inputs['us']
    assert (directory / 'manifest.json').read_bytes() == before
    assert (directory / 'completion.json').read_bytes() == receipt
    assert checkout.operation('cache-seal', 'daily', now=PREDICT)['status'] == 'sealed'
    shutil.rmtree(checkout.private)
    assert checkout.operation('cache-open', 'daily', now=PREDICT)['status'] == 'restored'
    assert checkout.operation('inputs', 'daily', now=PREDICT)['prepared_manifest_hash'] == inputs['prepared_manifest_hash']


def test_daily_wait_stuck_clock_abstains_at_monotonic_budget(checkout):
    _prepared(checkout)
    result = checkout.operation('wait', 'daily', now=PREPARE, scenario='stuck-clock')
    assert result['status'] == 'not_ready' and result['reason'] == 'daily_wait_deadline'
    assert checkout.operation('inputs', 'daily', now=PREPARE)['status'] == 'not_ready'


def test_daily_preparation_survives_failed_runner_for_late_retry(checkout):
    _, directory = _prepared(checkout)
    manifest = (directory / 'manifest.json').read_bytes()
    receipt = (directory / 'completion.json').read_bytes()
    assert checkout.operation('cache-seal', 'daily', now=PREPARE)['status'] == 'sealed'
    shutil.rmtree(checkout.private)
    gate = checkout.operation('gate', 'daily', now=PREDICT)
    assert gate['allowed'] and gate['forecast_requested'] and not gate['collect']
    assert checkout.operation('cache-open', 'daily', now=PREDICT)['status'] == 'restored'
    inputs = checkout.operation('inputs', 'daily', now=PREDICT)
    assert inputs['status'] == 'ready'
    assert (directory / 'manifest.json').read_bytes() == manifest
    assert (directory / 'completion.json').read_bytes() == receipt
    script = checkout.root / 'scripts/accuracy_operation.py'
    script.write_bytes(script.read_bytes() + b'\n# changed source after crash\n')
    assert checkout.operation('inputs', 'daily', now=PREDICT)['status'] == 'not_ready'

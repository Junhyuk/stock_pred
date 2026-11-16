"""After-close issuance must use its own clock, state and completion cohort."""
import json
import sqlite3
from dataclasses import asdict

import pandas as pd
import pytest

from roboquant.index_forecast import automation_state as state
from roboquant.index_forecast.reliable import ReliableSettings
from roboquant.index_forecast.reliable_inputs import resolve_forecast_context
from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame

PROTOCOL = 'after_close_daily_v1'


def test_postclose_freeze_uses_actual_finished_cutoff_and_retains_integrity(tmp_path):
    source = tmp_path / 'prices.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE evidence(value REAL)')
        db.execute('INSERT INTO evidence VALUES(10)')
    manifest = state.freeze_inputs({'sqlite': source}, tmp_path / 'frozen',
        prepared_at='2026-11-16T09:03:00Z', code_hash='source', issuance_protocol=PROTOCOL)
    assert manifest['issuance_protocol'] == PROTOCOL
    assert manifest['prepared_at'] == '2026-11-16T09:03:00+00:00'
    assert state.restore_inputs(tmp_path / 'frozen', now='2026-11-16T09:04:00Z', code_hash='source', issuance_protocol=PROTOCOL)['status'] == 'ready'
    assert state.restore_inputs(tmp_path / 'frozen', now='2026-11-16T09:04:00Z', code_hash='changed', issuance_protocol=PROTOCOL)['status'] == 'not_ready'
    assert state.restore_inputs(tmp_path / 'frozen', now='2026-11-16T09:04:00Z', code_hash='source')['status'] == 'not_ready'


def test_postclose_context_uses_current_completed_close_and_next_session():
    result = resolve_forecast_context('2026-11-16', now=pd.Timestamp('2026-11-16T09:03:00Z'), issuance_protocol=PROTOCOL)
    assert result['status'] == 'ready'
    assert result['stage'] == 'postclose'
    assert result['target_date'] == '2026-11-17'
    assert result['prediction_cutoff'] == '2026-11-16T09:03:00+00:00'
    assert pd.Timestamp(result['issuance_deadline']) == pd.Timestamp('2026-11-17T00:00:00Z')


def test_historical_kr_cutoff_not_shifted_by_late_current_collection():
    sessions = pd.bdate_range('2026-09-01', '2026-11-20').strftime('%Y-%m-%d').tolist()
    prices = pd.DataFrame({'session_date': sessions[:-4], 'close': range(100, 100 + len(sessions[:-4]))})
    context = {'asof_date': prices.session_date.iloc[-1], 'stage': 'postclose',
               'prediction_cutoff': prices.session_date.iloc[-1] + 'T19:17:00+09:00'}
    result = build_reliable_price_frame(prices, sessions=sessions, forecast_context=context)
    assert pd.Timestamp(result.iloc[-2].prediction_cutoff).tz_convert('Asia/Seoul').hour == 18
    assert pd.Timestamp(result.iloc[-1].prediction_cutoff).tz_convert('Asia/Seoul').hour == 19


def test_us_historical_clock_skips_kst_weekend_and_respects_dst():
    from roboquant.index_forecast.postclose import historical_cutoff
    assert historical_cutoff('2026-11-13T21:00:00Z', 'XNYS') == pd.Timestamp('2026-11-16T09:00:00Z')
    assert historical_cutoff('2026-07-06T20:00:00Z', 'XNYS') == pd.Timestamp('2026-07-07T09:00:00Z')


def test_settings_identity_separates_same_anchor_and_features():
    post = ReliableSettings(issuance_protocol=PROTOCOL)
    assert asdict(post) != asdict(ReliableSettings())
    assert post.refit_origin == '2019-01-02' and post.retrain_step_days == 21


def test_completion_is_protocol_market_target_specific():
    from roboquant.index_forecast.postclose import pending_markets
    completed = [{'issuance_protocol': 'premarket_v1', 'market': 'KOSPI', 'target_date': '2026-11-17'},
                 {'issuance_protocol': PROTOCOL, 'market': 'US_SPX', 'target_date': '2026-11-16'}]
    targets = {'KOSPI': '2026-11-17', 'KOSDAQ': '2026-11-17', 'US_SPX': '2026-11-16', 'US_NASDAQ': '2026-11-16'}
    assert pending_markets(targets, completed) == ['KOSPI', 'KOSDAQ', 'US_NASDAQ']


def test_us_builder_postclose_rows_have_scheduled_1800_cutoff(tmp_path):
    from test_us_forecast_integration import fixture
    from roboquant.index_forecast.us_market_pipeline import build_us_target_frame
    _, _, observations = fixture(tmp_path)
    frame, _ = build_us_target_frame(observations, symbol='^GSPC',
        reporting_cutoff='2026-10-26T09:04:00Z', issuance_protocol=PROTOCOL)
    older = frame.loc[frame.asof_date < frame.asof_date.max()]
    assert {pd.Timestamp(value).tz_convert('Asia/Seoul').hour for value in older.prediction_cutoff} == {18}


def test_postclose_forward_cohort_does_not_import_legacy_history(tmp_path):
    from roboquant.index_forecast.economic_forecast import begin_forward, finish_forward
    (tmp_path / 'forward_history.json').write_text('{"invalid_legacy":true}')
    evidence = begin_forward(tmp_path, config={'private_state_dir': str(tmp_path / 'private')},
        settings=ReliableSettings(issuance_protocol=PROTOCOL), started_at=pd.Timestamp('2026-11-16T09:04:00Z'), code_hash='code', replay=False)
    finish_forward(tmp_path, evidence=evidence, frames={}, observed_at=pd.Timestamp('2026-11-16T09:05:00Z'))
    assert (tmp_path / 'forward_history.json').read_text() == '{"invalid_legacy":true}'
    assert json.loads((tmp_path / 'postclose_forward_history.json').read_text())['forecasts'] == []


def test_actual_auto_operation_selects_postclose_and_does_not_wait(tmp_path):
    from test_automation_cli_integration import Checkout
    checkout = Checkout(tmp_path / 'checkout')
    gate = checkout.operation('gate', mode='auto', now='2026-11-16T09:02:00+00:00')
    assert gate['mode'] == 'postclose'
    assert gate['collect'] is True and gate['forecast_requested'] is True
    assert checkout.operation('gate', mode='auto', now='2026-11-14T09:02:00+00:00')['allowed'] is False


def test_postclose_inputs_keep_us_eligible_when_kr_stale(tmp_path):
    from test_automation_cli_integration import Checkout
    checkout = Checkout(tmp_path / 'checkout')
    us_csv = checkout.seed()
    from roboquant.index_forecast.market_closes import write_market_closes
    write_market_closes({'schema_version': 'market_closes_v1', 'run_id': 'test', 'markets': {}}, checkout.root / 'docs/data/market_closes.json', published_at='2026-11-16T09:01:00Z')
    frozen = json.loads(checkout.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'postclose',
        '--sqlite', str(checkout.private / 'index_forecast.sqlite'), '--us-csv', str(us_csv),
        now='2026-11-16T09:02:00+00:00').stdout.splitlines()[-1])
    assert frozen['status'] == 'prepared'
    inputs = checkout.operation('inputs', mode='postclose', now='2026-11-16T09:03:00+00:00')
    assert inputs['kr'] is False and inputs['us'] is True
    assert inputs['issuance_protocol'] == PROTOCOL


def test_postclose_gate_rejects_before_1800():
    assert state.operation_decision('2026-11-15T21:00:00Z', 'postclose')['allowed'] is False
    assert state.operation_decision('2026-11-16T08:59:59Z', 'postclose')['allowed'] is False


def test_postclose_cli_filters_completed_market_targets(tmp_path):
    from test_automation_cli_integration import Checkout
    checkout = Checkout(tmp_path / 'checkout')
    us_csv = checkout.seed()
    from roboquant.index_forecast.market_closes import write_market_closes
    write_market_closes({'schema_version': 'market_closes_v1', 'run_id': 'test', 'markets': {}}, checkout.root / 'docs/data/market_closes.json', published_at='2026-11-16T09:01:00Z')
    (checkout.root / 'docs/data/postclose_completions.json').write_text(json.dumps({'records': [
        {'issuance_protocol': PROTOCOL, 'market': 'US_SPX', 'target_date': '2026-11-16'}]}))
    checkout.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'postclose',
        '--sqlite', checkout.private / 'index_forecast.sqlite', '--us-csv', us_csv, now='2026-11-16T09:02:00+00:00')
    result = checkout.operation('inputs', mode='postclose', now='2026-11-16T09:03:00+00:00')
    assert result['markets'] == 'US_NASDAQ'


def test_postclose_pipeline_uses_only_requested_market_with_actual_frozen_cutoff(tmp_path):
    from test_us_forecast_integration import fixture, settings
    from roboquant.index_forecast.reliable_pipeline import run_reliable_pipeline
    path, sessions, _ = fixture(tmp_path)
    result = run_reliable_pipeline(path, tmp_path / 'public', sessions=sessions,
        config={'issuance_protocol': PROTOCOL, 'markets': ['KOSPI'], 'settings': asdict(settings()),
                'prepared_inputs': {'prepared_at': '2026-10-26T09:04:00Z'}},
        now=pd.Timestamp('2026-10-26T09:05:00Z'))
    # Fixture only has October23; stale data remains blocked and must never be relabeled26.
    assert result['markets'] == []
    assert set(result['contexts']) == {'KOSPI'}


def test_postclose_pipeline_research_reference_has_current_target_without_live_anchor(tmp_path):
    from test_us_forecast_integration import fixture, settings
    from roboquant.index_forecast.reliable_pipeline import run_reliable_pipeline
    path, sessions, _ = fixture(tmp_path)
    result = run_reliable_pipeline(path, tmp_path / 'public', sessions=sessions,
        config={'issuance_protocol': PROTOCOL, 'markets': ['KOSPI'], 'settings': asdict(settings())},
        now=pd.Timestamp('2026-10-23T09:05:00Z'))
    # Fixture receipt is23:00 UTC, so this run cannot use it at09:05 UTC.
    assert result['markets'] == []
    assert 'latest_index_ingested_at_unavailable_at_cutoff' in result['reasons']


def test_postclose_window_rejects_next_morning_even_on_holiday():
    result = resolve_forecast_context('2026-11-13', now=pd.Timestamp('2026-11-15T21:00:00Z'), issuance_protocol=PROTOCOL)
    assert result['status'] == 'blocked_issuance_window'


def test_actual_postclose_without_live_anchor_still_recomputes_research_reference(tmp_path, monkeypatch):
    from datetime import datetime
    from test_us_forecast_integration import fixture, settings
    from roboquant.index_forecast import reliable_pipeline as pipeline
    path, sessions, _ = fixture(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE index_eod SET ingested_at='2026-10-23T09:00:00Z'")
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return pd.Timestamp('2026-10-23T09:05:00Z').to_pydatetime().astimezone(tz)
    monkeypatch.setattr(pipeline, 'datetime', Clock)
    result = pipeline.run_reliable_pipeline(path, tmp_path / 'public', sessions=sessions,
        config={'issuance_protocol': PROTOCOL, 'markets': ['KOSPI'], 'settings': asdict(settings()),
                'private_state_dir': str(tmp_path / 'private')})
    assert result['status'] == 'research_only'
    assert [row['market'] for row in result['markets']] == ['KOSPI']
    row = result['markets'][0]
    assert row['forecast']['status'] == 'not_ready'
    assert row['forecast']['p_up'] is None
    assert row['incumbent_reference']['p_up'] is not None
    assert row['incumbent_reference']['expected_index'] is not None
    assert row['incumbent_reference']['target_date'] == '2026-10-26'
    assert row['incumbent_reference']['production_weight'] == 0


def test_postclose_prices_preserve_public_close_receipt_on_history_429(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    import sys
    scripts = Path(__file__).parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('postclose_prices_test', scripts / 'run_postclose_prices.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    from roboquant.index_forecast.market_closes import collect_market_closes
    def collect(db, **kwargs):
        return collect_market_closes(db, now='2026-11-16T09:01:00Z', fetch_naver=lambda _: [], fetch_yahoo=lambda _: [])
    monkeypatch.setattr(runner, 'collect_market_closes', collect)
    def unavailable(**kwargs):
        raise RuntimeError('Yahoo429')
    monkeypatch.setattr(runner, 'refresh_premarket_prices', unavailable)
    result = runner.run(sqlite_path=tmp_path / 'data.sqlite', output_dir=tmp_path / 'private', public_dir=tmp_path / 'public')
    assert result['history_refresh_status'] == 'unavailable'
    assert pd.read_csv(result['us_csv']).empty
    assert (tmp_path / 'public/market_closes_publication.json').is_file()


def test_partial_retry_keeps_prior_card_original_issuance_and_identity(tmp_path):
    from roboquant.index_forecast.postclose import carry_verified_markets
    from roboquant.index_forecast.reliable_pipeline import _resolved_run
    old = {'market': 'KOSPI', 'forecast_id': 'immutable', 'context': {'issued_at': '2026-11-16T09:05:00Z'},
           'forecast': {'forecast_id': 'immutable', 'issued_at': '2026-11-16T09:05:00Z'}}
    prior = {'status': 'research_only', 'issuance_protocol': PROTOCOL, 'run_id': 'first', 'markets': [old]}
    new = {'markets': [{'market': 'KOSDAQ', 'context': {}, 'forecast': {}}]}
    merged = carry_verified_markets(new, prior)
    result = _resolved_run(merged, {'publication_checked_at': '2026-11-16T09:15:00Z'})
    previous = next(r for r in result['markets'] if r['market'] == 'KOSPI')
    assert previous['forecast_id'] == 'immutable'
    assert previous['context']['issued_at'] == '2026-11-16T09:05:00Z'
    assert previous['carried_forward'] is True
    assert previous['source_run_id'] == 'first'


def test_public_paths_reject_unverified_market_close_bytes(tmp_path):
    (tmp_path / 'market_closes.json').write_text('{"schema_version":"market_closes_v1","run_id":"one","markets":{}}')
    (tmp_path / 'market_closes_publication.json').write_text('{"payload_checksums":{"market_closes.json":"wrong"}}')
    with pytest.raises(ValueError, match='market.close'):
        state.public_payload_paths(tmp_path)


def test_actual_postclose_cli_freeze_publish_and_partial_completion(tmp_path):
    from test_automation_cli_integration import Checkout
    from roboquant.index_forecast.market_closes import write_market_closes
    from roboquant.index_forecast.reliable_inputs import session_schedule
    from roboquant.index_forecast.us_market_inputs import US_SYMBOLS, us_close_schedule
    checkout = Checkout(tmp_path / 'checkout')
    import yaml
    from test_us_forecast_integration import settings
    (checkout.root / 'configs/reliable_index_forecast.yaml').write_text(yaml.safe_dump({'settings': asdict(settings())}))
    kr_days = session_schedule('2026-04-01', '2026-11-16')[0]['date'].tolist()
    us_days = sorted(us_close_schedule('2026-04-01', '2026-11-13').session_date.unique())
    fixture = {symbol: [[day, 100 + i * .1 + i % 7] for i, day in enumerate(us_days)] for symbol in US_SYMBOLS}
    fixture.update({symbol: [[day, 1000 + i * 2 + i % 5] for i, day in enumerate(kr_days)] for symbol in ('^KS11', '^KQ11')})
    (checkout.root / 'provider.json').write_text(json.dumps(fixture))
    prices = checkout.refresh(now='2026-11-16T09:01:00+00:00')
    write_market_closes({'schema_version': 'market_closes_v1', 'run_id': 'test', 'markets': {
        m: {'status': 'verified'} for m in ('KOSPI', 'KOSDAQ')}}, checkout.root / 'docs/data/market_closes.json', published_at='2026-11-16T09:01:00Z')
    freeze = json.loads(checkout.run('accuracy_operation.py', '--phase', 'freeze', '--mode', 'postclose',
        '--sqlite', prices['sqlite'], '--us-csv', prices['us_csv'], now='2026-11-16T09:02:00+00:00').stdout.splitlines()[-1])
    assert freeze['status'] == 'prepared'
    inputs = checkout.operation('inputs', mode='postclose', now='2026-11-16T09:03:00+00:00')
    assert inputs['kr'] and inputs['us']
    for market, script, selection, output in [('kr', 'run_reliable_index_forecast.py', 'KOSPI', 'docs/data'),
                                             ('us', 'run_us_index_forecast.py', 'US_NASDAQ', 'docs/data/us')]:
        args = ['--us-observations', inputs['us_csv'], '--prepared-manifest', inputs['prepared_manifest'],
                '--economic-sqlite', inputs['economic_sqlite'], '--fit-state-dir', inputs[f'fit_models_{market}'],
                '--issuance-protocol', PROTOCOL, '--markets', selection]
        if market == 'kr':
            args += ['--sqlite', inputs['sqlite']]
        completed = checkout.run(script, *args, now='2026-11-16T09:04:00+00:00')
        report = json.loads(completed.stdout.splitlines()[-1])
        assert report['status'] == 'research_only'
        assert [row['market'] for row in report['markets']] == [selection]
        payload = json.loads((checkout.root / output / 'reliable_index_forecast.json').read_text())
        assert payload['markets'][0]['context']['prediction_cutoff'] == '2026-11-16T09:02:00+00:00'
    checkout.operation('status', mode='postclose', now='2026-11-16T09:05:00+00:00')
    pending = checkout.operation('inputs', mode='postclose', now='2026-11-16T09:06:00+00:00')
    assert set(pending['markets'].split(',')) == {'KOSDAQ', 'US_SPX'}


def test_postclose_cache_distinguishes_empty_start_from_failed_authentication(tmp_path):
    from test_automation_cli_integration import Checkout
    checkout = Checkout(tmp_path / 'checkout')
    cold = checkout.operation('cache-open', mode='postclose', now='2026-11-16T09:00:00+00:00')
    assert cold['cache_ready'] is True
    missing = checkout.operation('cache-open', mode='postclose', now='2026-11-16T09:00:00+00:00', key='')
    assert missing['cache_ready'] is False
    encrypted = checkout.root / '.forecast-cache/state.enc'
    encrypted.parent.mkdir(exist_ok=True)
    encrypted.write_bytes(b'unauthenticated')
    bad = checkout.operation('cache-open', mode='postclose', now='2026-11-16T09:00:00+00:00')
    assert bad['cache_ready'] is False


@pytest.mark.parametrize('reference_ready', [False, True])
def test_completion_requires_usable_calibrated_forecast_or_reference(tmp_path, monkeypatch, reference_ready):
    from roboquant.index_forecast import postclose
    row = {'market': 'KOSPI', 'forecast_id': 'id', 'context': {'target_date': '2026-11-17'},
           'forecast': {'status': 'not_ready'}, 'incumbent_reference': {
               'status': 'rejected' if reference_ready else 'not_ready', 'calibration_ready': reference_ready,
               'expected_return': .01 if reference_ready else None, 'p_up': .55 if reference_ready else None}}
    monkeypatch.setattr(postclose, 'verified_latest', lambda path: {'issuance_protocol': PROTOCOL,
        'issued_at': '2026-11-16T09:05:00Z', 'run_id': 'run', 'markets': [row]} if path == tmp_path else {})
    assert bool(postclose.verified_completion_records(tmp_path)) is reference_ready

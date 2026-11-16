#!/usr/bin/env python3
"""Actual-clock weekday orchestration with one daily preparation/forecast run."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
# Gate before installing the model stack; this helper itself uses stdlib only.
spec = importlib.util.spec_from_file_location('_accuracy_state', ROOT / 'src/roboquant/index_forecast/automation_state.py')
state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(state)


def emit(data):
    print(json.dumps(data, ensure_ascii=False, default=str))
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'], 'a') as stream:
            for key, value in data.items():
                if isinstance(value, (str, bool, int)):
                    stream.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


def kr_input_diagnostics(dates, *, now, mode):
    """Expose only session metadata while retaining the existing issuance guard."""
    from roboquant.index_forecast.reliable_inputs import resolve_forecast_context

    contexts = [(market, resolve_forecast_context(day, now=now, issuance_protocol='after_close_daily_v1' if mode == 'postclose' else 'premarket_v1')) for market, day in dates]
    eligible = len(contexts) == 2 and all(
        context['status'] == 'ready'
        and (mode not in {'predict', 'daily'} or context['stage'] == 'premarket_final')
        for _, context in contexts
    )
    if mode == 'postclose':
        eligible = any(context['status'] == 'ready' for _, context in contexts)
    fields = ('asof_date', 'status', 'expected_asof_date', 'target_date', 'stage', 'reasons')
    return {
        'kr': eligible,
        'kr_reason': 'eligible_actual_context' if eligible else 'no_valid_next_kr_session',
        'kr_sessions': [
            {'index_code': market, **{key: context[key] for key in fields}}
            for market, context in contexts
        ],
        'kr_missing_markets': sorted({'KOSPI', 'KOSDAQ'} - {market for market, _ in contexts}),
    }


def restored_cache_diagnostics(private):
    """Report restored session dates without exposing prices or database contents."""
    import sqlite3

    path = Path(private) / 'index_forecast.sqlite'
    result = {'restored_latest_index_dates': {}, 'restored_index_metadata_status': 'missing'}
    if path.is_file():
        try:
            dates = state.sqlite_metadata(path)['latest_index_dates']
            result.update(restored_latest_index_dates={
                market: dates[market] for market in ('KOSPI', 'KOSDAQ') if market in dates
            }, restored_index_metadata_status='available')
        except (OSError, ValueError, sqlite3.Error):
            result['restored_index_metadata_status'] = 'unavailable'
    return result


def completed_publication_date(status):
    """Recognize a verified split-predict run as well as a new daily run."""
    from write_automation_status import verified_publication

    try:
        inputs = status.get('inputs') or {}
        if status.get('mode') not in {'predict', 'daily'} or inputs.get('status') != 'ready':
            return None
        requested = [name for name in ('kr', 'us') if inputs.get(name) is True]
        if not requested:
            return None
        checked = state._utc(status['checked_at'])
        prepared = state._utc(inputs['prepared_at'])
        for market in requested:
            name, directory = ('korea', ROOT / 'docs/data') if market == 'kr' else ('us', ROOT / 'docs/data/us')
            actual = verified_publication(directory)
            if actual is None or actual != status.get(name):
                return None
            published = state._utc(actual['publication_checked_at'])
            if not prepared <= published <= checked or published.astimezone(state.KST).date().isoformat() != status['kst_date']:
                return None
        return status['kst_date']
    except (ValueError, KeyError, TypeError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=['gate', 'freeze', 'wait', 'inputs', 'cache-open', 'cache-seal', 'status', 'public-paths', 'publish-models'])
    parser.add_argument('--mode', default='auto', choices=['auto', 'postclose', 'daily', 'prepare', 'predict', 'latest', 'validate'])
    parser.add_argument('--schedule', default='')
    parser.add_argument('--sqlite')
    parser.add_argument('--us-csv')
    parser.add_argument('--reason', default='completed')
    parser.add_argument('--kr-outcome', default='skipped')
    parser.add_argument('--us-outcome', default='skipped')
    args = parser.parse_args()
    now = datetime.now(UTC)
    mode = args.mode if args.mode != 'auto' else 'postclose'
    protocol = 'after_close_daily_v1' if mode == 'postclose' else 'premarket_v1'
    decision = state.operation_decision(now, mode)
    private = ROOT / 'data/processed'
    frozen = private / 'prepared' / (protocol + '-' + decision['kst_date'] if mode == 'postclose' else decision['kst_date'])
    if mode == 'postclose':
        # Each retry gets a new actual snapshot; completed market targets are skipped independently.
        if args.phase == 'freeze':
            run_key = os.environ.get('GITHUB_RUN_ID', 'local')
            attempt_key = os.environ.get('GITHUB_RUN_ATTEMPT', '1')
            frozen = frozen / f'{run_key}-{attempt_key}-{now.strftime("%H%M%S%f")}'
        pointer = private / 'postclose-prepared.json'
        if args.phase != 'freeze' and pointer.is_file():
            frozen = Path(json.loads(pointer.read_text())['directory'])
    encrypted = ROOT / '.forecast-cache/state.enc'
    status_file = ROOT / 'docs/data/automation_status.json'
    previous = json.loads(status_file.read_text()) if status_file.is_file() else {}
    if args.phase == 'gate':
        if mode == 'daily' and (previous.get('daily_completed_date') == decision['kst_date']
                               or completed_publication_date(previous) == decision['kst_date']):
            decision.update(allowed=False, reason='daily_already_completed')
        emit({**decision, 'collect': decision['allowed'] and (mode in {'prepare', 'latest', 'postclose'}
                  or mode == 'daily' and now.astimezone(state.KST).hour < 8),
              'forecast_requested': decision['allowed'] and mode in {'daily', 'predict', 'latest', 'postclose'}})
        return
    if args.phase == 'public-paths':
        if decision['allowed'] and mode != 'validate':
            for path in state.public_payload_paths(ROOT / 'docs/data'):
                print(path.relative_to(ROOT))
        return
    if not decision['allowed']:
        emit(decision)
        return
    if args.phase == 'wait':
        # Missing or failed preparation must abstain immediately, not consume
        # the job's bounded forecast budget waiting for inputs that cannot arrive.
        if not (frozen / 'manifest.json').is_file() or not (frozen / 'completion.json').is_file():
            emit({'status': 'not_ready', 'reason': 'missing_prepared_inputs'})
        else:
            emit(state.wait_for_daily_cutoff())
        return
    if args.phase.startswith('cache-'):
        key = os.environ.get('FORECAST_CACHE_KEY', '')
        if not key:
            emit({'status': 'not_ready', 'reason': 'encrypted_cache_key_unavailable', 'cache_ready': False})
            return
        if args.phase == 'cache-open':
            try:
                state.open_private_cache(encrypted, private, key=key)
                emit({'status': 'restored', 'cache_ready': True, **restored_cache_diagnostics(private)})
            except ValueError:
                emit({'status': 'not_ready', 'reason': 'encrypted_cache_unavailable', 'cache_ready': not encrypted.exists()})
        else:
            # Keep only the latest preparation day and latest dated US panel.
            import shutil
            for path in (private / 'prepared').glob('*'):
                if path.name not in {decision['kst_date'], 'after_close_daily_v1-' + decision['kst_date']} and path.is_dir():
                    shutil.rmtree(path)
            csvs = sorted(p for p in private.glob('us_market_observations_v2_*.csv') if p.stem.removeprefix('us_market_observations_v2_')[:8].isdigit())
            for path in csvs[:-1]:
                path.unlink()
            import tempfile
            with tempfile.TemporaryDirectory(prefix='accuracy-cache-') as temporary:
                bundle = Path(temporary)
                permitted = [private / 'index_forecast.sqlite', private / 'postclose-prepared.json', *(csvs[-1:] or [private / 'us_market_observations_v2_unavailable.csv'])]
                for directory in ('prepared', 'economic-calendar', 'accuracy-state'):
                    permitted.extend(p for p in (private / directory).rglob('*') if p.is_file() and p.suffix in {'.sqlite', '.csv', '.json'})
                for source in permitted:
                    if source.is_file() and not source.is_symlink():
                        target = bundle / source.relative_to(private)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if source.suffix == '.sqlite' and source.relative_to(private).parts[0] != 'prepared':
                            state.sqlite_snapshot(source, target)
                        else:
                            shutil.copy2(source, target)
                emit({'status': 'sealed', **state.seal_private_cache(bundle, encrypted, key=key)})
        return
    if args.phase == 'freeze':
        if mode == 'latest':
            emit({'status': 'latest_uses_actual_receipts_not_0800_snapshot'})
            return
        try:
            from roboquant.index_forecast.economic_calendar import EconomicCalendarStore
            economic_db = private / 'economic-calendar/economic.sqlite'
            EconomicCalendarStore(economic_db)
            model_dir = 'fit_models_postclose' if mode == 'postclose' else 'fit_models'
            files = {'sqlite': args.sqlite, 'us_csv': args.us_csv,
                'economic_sqlite': economic_db, 'fit_models_kr': ROOT / 'docs/data' / model_dir,
                'fit_models_us': ROOT / 'docs/data/us' / model_dir}
            if mode == 'postclose':
                files['market_closes'] = ROOT / 'docs/data/market_closes.json'
                files['market_closes_receipt'] = ROOT / 'docs/data/market_closes_publication.json'
            result = state.freeze_inputs(files, frozen, code_hash=state.source_fingerprint(), issuance_protocol=protocol)
            if mode == 'postclose':
                state.atomic_json(private / 'postclose-prepared.json', {'directory': str(frozen)})
            emit({'status': 'prepared', 'prepared_at': result['prepared_at'], 'manifest_hash': result['manifest_hash']})
        except (OSError, ValueError, TypeError) as error:
            emit({'status': 'not_ready', 'reason': str(error)})
        return
    if args.phase == 'inputs':
        paths = {'sqlite': Path(args.sqlite), 'us_csv': Path(args.us_csv)} if mode == 'latest' and args.sqlite and args.us_csv else {}
        restored = {'status': 'latest_actual_receipts'}
        if mode in {'predict', 'daily', 'postclose'}:
            restored = state.restore_inputs(frozen, now=now, code_hash=state.source_fingerprint(), issuance_protocol=protocol)
            paths = restored['paths']
            if restored['status'] == 'ready':
                if set(paths) != (state.POSTCLOSE_PREPARED_NAMES if mode == 'postclose' else state.PREPARED_NAMES):
                    restored = {'status': 'not_ready', 'reason': 'incomplete_prepared_input_bundle'}
                    paths = {}
                else:
                    runtime = private / 'accuracy-runtime' / now.strftime('%Y%m%dT%H%M%S%f')
                    paths = state.stage_prepared_runtime(restored, runtime)
        result = {'issuance_protocol': protocol, 'kr': False, 'us': False, 'status': restored['status'], 'reason': restored.get('reason', '')}
        if mode in {'predict', 'daily', 'postclose'} and restored.get('status') == 'ready':
            result.update(prepared_manifest=str(frozen / 'manifest.json'),
                          prepared_manifest_hash=restored['manifest']['manifest_hash'],
                          prepared_at=restored['manifest']['prepared_at'])
        if paths:
            if mode == 'latest':
                paths.update(economic_sqlite=private / 'economic-calendar/economic.sqlite',
                             fit_models_kr=ROOT / 'docs/data/fit_models', fit_models_us=ROOT / 'docs/data/us/fit_models')
            import sqlite3

            import pandas as pd

            from roboquant.index_forecast.us_market_inputs import us_close_schedule
            result.update({key: str(value) for key, value in paths.items()})
            if paths.get('sqlite') and paths['sqlite'].is_file():
                with sqlite3.connect(f"file:{paths['sqlite'].resolve()}?mode=ro", uri=True) as db:
                    dates = db.execute("SELECT index_code, MAX(session_date) FROM index_eod WHERE index_code IN ('KOSPI','KOSDAQ') GROUP BY index_code").fetchall()
                result.update(kr_input_diagnostics(dates, now=restored['manifest']['prepared_at'] if mode == 'postclose' else now, mode=mode))
            if paths.get('us_csv') and paths['us_csv'].is_file():
                schedule = us_close_schedule(pd.Timestamp(now) - pd.Timedelta(days=10), pd.Timestamp(now) + pd.Timedelta(days=10))
                completed = schedule.loc[pd.to_datetime(schedule.session_close_at, utc=True) <= now]
                following = schedule.loc[schedule.session_date > completed.iloc[-1].session_date] if not completed.empty else schedule.iloc[:0]
                result['us'] = not following.empty and now < pd.Timestamp(following.iloc[0].session_open_at)
                if mode in {'predict', 'daily'} and not result['kr']:
                    result['us'] = False
                result['us_reason'] = 'eligible_actual_context' if result['us'] else 'current_us_session_open_or_unavailable'
        if mode == 'postclose' and paths:
            from roboquant.index_forecast.postclose import pending_markets
            completed_path = ROOT / 'docs/data/postclose_completions.json'
            completed = json.loads(completed_path.read_text()).get('records', []) if completed_path.is_file() else []
            targets = {item['index_code']: item['target_date'] for item in result.get('kr_sessions', []) if item['status'] == 'ready'}
            if result['us']:
                targets.update({market: following.iloc[0].session_date for market in ('US_SPX', 'US_NASDAQ')})
            pending = pending_markets(targets, completed)
            result.update(kr=any(m in pending for m in ('KOSPI', 'KOSDAQ')), us=any(m in pending for m in ('US_SPX', 'US_NASDAQ')),
                          markets=','.join(pending), issuance_protocol=protocol)
        state.atomic_json(private / 'accuracy-input-status.json', result)
        emit(result)
        return
    if args.phase == 'publish-models':
        status_path = private / 'accuracy-input-status.json'
        runtime = json.loads(status_path.read_text()) if status_path.is_file() else {}
        state.publish_prepared_models(runtime, ROOT / 'docs/data')
        emit({'status': 'persisted_derived_model_state'})
        return
    if args.phase == 'status':
        from write_automation_status import verified_publication
        status_path = private / 'accuracy-input-status.json'
        result = {**decision, 'checked_at': now.isoformat(), 'reason': args.reason,
                  'run_url': f"https://github.com/{os.environ.get('GITHUB_REPOSITORY', 'junhyuk/stock_pred')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}",
                  'korea': verified_publication(ROOT / 'docs/data'), 'us': verified_publication(ROOT / 'docs/data/us'),
                  'inputs': json.loads(status_path.read_text()) if status_path.is_file() else None,
                  'next_scheduled_cutoff': '08:00 Asia/Seoul; one daily run starts Monday-Friday07:50 KST',
                  'prepared_input_policy': 'Actual receipt before08:00, encrypted cache, same-day immutable manifest. Missing state abstains.'}
        if mode == 'postclose':
            from roboquant.index_forecast.postclose import verified_completion_records
            completions = ROOT / 'docs/data/postclose_completions.json'
            old = json.loads(completions.read_text()).get('records', []) if completions.is_file() else []
            new = verified_completion_records(ROOT / 'docs/data')
            records = {(r['issuance_protocol'], r['market'], r['target_date']): r for r in old}
            for record in new:
                records.setdefault((record['issuance_protocol'], record['market'], record['target_date']), record)
            state.atomic_json(completions, {'schema_version': 'postclose_completions_v1', 'records': list(records.values())})
            outcomes = {'kr': args.kr_outcome, 'us': args.us_outcome}
            failed = [market for market, outcome in outcomes.items() if outcome == 'failure']
            prepared_at = (result.get('inputs') or {}).get('prepared_at')
            fresh = [row['market'] for row in new if prepared_at and state._utc(row['publication_checked_at']) >= state._utc(prepared_at)]
            result.update(forecast_step_outcomes=outcomes, new_usable_forecast_markets=fresh,
                reason='partial_forecast_failure:' + ','.join(failed) if failed else 'new_research_forecasts_published' if fresh else 'no_new_usable_forecast; current_closes_independent')
            result.update(next_scheduled_cutoff='18:00 Asia/Seoul; Monday-Friday',
                          prepared_input_policy='Actual completed collection snapshot cutoff; independent market targets; immutable receipts.')
        result['daily_completed_date'] = (completed_publication_date(result)
            or previous.get('daily_completed_date') or completed_publication_date(previous))
        state.atomic_json(ROOT / 'docs/data/automation_status.json', result)
        emit(result)


if __name__ == '__main__':
    main()

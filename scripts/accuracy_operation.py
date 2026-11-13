#!/usr/bin/env python3
"""Actual-clock orchestration for prepare, predict, latest and validation modes."""
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

    contexts = [(market, resolve_forecast_context(day, now=now)) for market, day in dates]
    eligible = len(contexts) == 2 and all(
        context['status'] == 'ready'
        and (mode != 'predict' or context['stage'] == 'premarket_final')
        for _, context in contexts
    )
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', required=True, choices=['gate', 'freeze', 'inputs', 'cache-open', 'cache-seal', 'status', 'public-paths', 'publish-models'])
    parser.add_argument('--mode', default='auto', choices=['auto', 'prepare', 'predict', 'latest', 'validate'])
    parser.add_argument('--schedule', default='')
    parser.add_argument('--sqlite')
    parser.add_argument('--us-csv')
    parser.add_argument('--reason', default='completed')
    args = parser.parse_args()
    now = datetime.now(UTC)
    mode = args.mode if args.mode != 'auto' else 'predict' if args.schedule == '0 23 * * 0-5' else 'prepare'
    decision = state.operation_decision(now, mode)
    private = ROOT / 'data/processed'
    frozen = private / 'prepared' / decision['kst_date']
    encrypted = ROOT / '.forecast-cache/state.enc'
    if args.phase == 'gate':
        emit({**decision, 'collect': decision['allowed'] and mode in {'prepare', 'latest'},
              'forecast_requested': decision['allowed'] and mode in {'predict', 'latest'}})
        return
    if args.phase == 'public-paths':
        for path in state.public_payload_paths(ROOT / 'docs/data'):
            print(path.relative_to(ROOT))
        return
    if args.phase.startswith('cache-'):
        key = os.environ.get('FORECAST_CACHE_KEY', '')
        if not key:
            emit({'status': 'not_ready', 'reason': 'encrypted_cache_key_unavailable'})
            return
        if args.phase == 'cache-open':
            try:
                state.open_private_cache(encrypted, private, key=key)
                emit({'status': 'restored', **restored_cache_diagnostics(private)})
            except ValueError:
                emit({'status': 'not_ready', 'reason': 'encrypted_cache_unavailable'})
        else:
            # Keep only the latest preparation day and latest dated US panel.
            import shutil
            for path in (private / 'prepared').glob('*'):
                if path.name != decision['kst_date'] and path.is_dir():
                    shutil.rmtree(path)
            csvs = sorted(private.glob('us_market_observations_v2_*.csv'))
            for path in csvs[:-1]:
                path.unlink()
            import tempfile
            with tempfile.TemporaryDirectory(prefix='accuracy-cache-') as temporary:
                bundle = Path(temporary)
                permitted = [private / 'index_forecast.sqlite', *sorted(private.glob('us_market_observations_v2_*.csv'))[-1:]]
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
    if not decision['allowed']:
        emit(decision)
        return
    if args.phase == 'freeze':
        if mode == 'latest':
            emit({'status': 'latest_uses_actual_receipts_not_0800_snapshot'})
            return
        try:
            from roboquant.index_forecast.economic_calendar import EconomicCalendarStore
            economic_db = private / 'economic-calendar/economic.sqlite'
            EconomicCalendarStore(economic_db)
            result = state.freeze_inputs({'sqlite': args.sqlite, 'us_csv': args.us_csv,
                'economic_sqlite': economic_db, 'fit_models_kr': ROOT / 'docs/data/fit_models',
                'fit_models_us': ROOT / 'docs/data/us/fit_models'}, frozen,
                                         code_hash=state.source_fingerprint())
            emit({'status': 'prepared', 'prepared_at': result['prepared_at'], 'manifest_hash': result['manifest_hash']})
        except (OSError, ValueError, TypeError) as error:
            emit({'status': 'not_ready', 'reason': str(error)})
        return
    if args.phase == 'inputs':
        paths = {'sqlite': Path(args.sqlite), 'us_csv': Path(args.us_csv)} if mode == 'latest' and args.sqlite and args.us_csv else {}
        restored = {'status': 'latest_actual_receipts'}
        if mode == 'predict':
            restored = state.restore_inputs(frozen, now=now, code_hash=state.source_fingerprint())
            paths = restored['paths']
            if restored['status'] == 'ready':
                if set(paths) != state.PREPARED_NAMES:
                    restored = {'status': 'not_ready', 'reason': 'incomplete_prepared_input_bundle'}
                    paths = {}
                else:
                    runtime = private / 'accuracy-runtime' / now.strftime('%Y%m%dT%H%M%S%f')
                    paths = state.stage_prepared_runtime(restored, runtime)
        result = {'kr': False, 'us': False, 'status': restored['status'], 'reason': restored.get('reason', '')}
        if mode == 'predict' and restored.get('status') == 'ready':
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
                result.update(kr_input_diagnostics(dates, now=now, mode=mode))
            if paths.get('us_csv') and paths['us_csv'].is_file():
                schedule = us_close_schedule(pd.Timestamp(now) - pd.Timedelta(days=10), pd.Timestamp(now) + pd.Timedelta(days=10))
                completed = schedule.loc[pd.to_datetime(schedule.session_close_at, utc=True) <= now]
                following = schedule.loc[schedule.session_date > completed.iloc[-1].session_date] if not completed.empty else schedule.iloc[:0]
                result['us'] = not following.empty and now < pd.Timestamp(following.iloc[0].session_open_at)
                if mode == 'predict' and not result['kr']:
                    result['us'] = False
                result['us_reason'] = 'eligible_actual_context' if result['us'] else 'current_us_session_open_or_unavailable'
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
                  'next_scheduled_cutoff': '08:00 Asia/Seoul on the next scheduled non-Sunday XKRX session',
                  'prepared_input_policy': 'Actual receipt before08:00, encrypted cache, same-day immutable manifest. Missing state abstains.'}
        state.atomic_json(ROOT / 'docs/data/automation_status.json', result)
        emit(result)


if __name__ == '__main__':
    main()

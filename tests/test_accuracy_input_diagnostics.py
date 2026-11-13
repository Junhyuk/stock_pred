"""Production-safe diagnostics for unchanged KR trading-day guards."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('accuracy_operation_audit', ROOT / 'scripts' / 'accuracy_operation.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
NOW = datetime(2026, 11, 14, 1, 30, tzinfo=ZoneInfo('Asia/Seoul'))
ALLOWED_FIELDS = {'index_code', 'asof_date', 'status', 'expected_asof_date', 'target_date', 'stage', 'reasons'}


def test_valid_latest_sessions_are_accepted_with_only_safe_metadata():
    report = MODULE.kr_input_diagnostics([('KOSPI', '2026-11-13'), ('KOSDAQ', '2026-11-13')], now=NOW, mode='latest')
    assert report['kr'] is True
    assert report['kr_reason'] == 'eligible_actual_context'
    assert report['kr_missing_markets'] == []
    assert {row['index_code'] for row in report['kr_sessions']} == {'KOSPI', 'KOSDAQ'}
    assert all(set(row) == ALLOWED_FIELDS for row in report['kr_sessions'])
    assert all(row['asof_date'] == '2026-11-13' and row['expected_asof_date'] == '2026-11-13'
               and row['target_date'] == '2026-11-16' and row['status'] == 'ready' for row in report['kr_sessions'])
    assert 'close' not in json.dumps(report).lower()


def test_one_stale_market_retains_reason_and_blocks_prediction():
    report = MODULE.kr_input_diagnostics([('KOSPI', '2026-11-13'), ('KOSDAQ', '2026-11-12')], now=NOW, mode='latest')
    assert report['kr'] is False
    assert report['kr_reason'] == 'no_valid_next_kr_session'
    rows = {row['index_code']: row for row in report['kr_sessions']}
    assert rows['KOSPI']['status'] == 'ready'
    assert rows['KOSDAQ']['status'] == 'blocked_stale_input'
    assert rows['KOSDAQ']['asof_date'] == '2026-11-12'
    assert rows['KOSDAQ']['expected_asof_date'] == '2026-11-13'
    assert rows['KOSDAQ']['reasons']


def test_missing_market_and_preliminary_predict_remain_blocked():
    missing = MODULE.kr_input_diagnostics([('KOSPI', '2026-11-13')], now=NOW, mode='latest')
    assert missing['kr'] is False
    assert missing['kr_missing_markets'] == ['KOSDAQ']
    assert all(set(row) == ALLOWED_FIELDS for row in missing['kr_sessions'])
    preliminary = MODULE.kr_input_diagnostics([('KOSPI', '2026-11-13'), ('KOSDAQ', '2026-11-13')], now=NOW, mode='predict')
    assert preliminary['kr'] is False
    assert preliminary['kr_reason'] == 'no_valid_next_kr_session'


def test_restored_cache_exposes_only_safe_latest_dates_and_missing_state(tmp_path):
    import sqlite3

    empty = MODULE.restored_cache_diagnostics(tmp_path)
    assert empty == {'restored_latest_index_dates': {}, 'restored_index_metadata_status': 'missing'}
    database = tmp_path / 'index_forecast.sqlite'
    with sqlite3.connect(database) as db:
        db.execute('CREATE TABLE index_eod (index_code TEXT, session_date TEXT, close REAL)')
        db.executemany('INSERT INTO index_eod VALUES (?, ?, ?)', [
            ('KOSPI', '2026-11-12', 991234.0), ('KOSPI', '2026-11-13', 991235.0),
            ('KOSDAQ', '2026-11-12', 991236.0)])
    report = MODULE.restored_cache_diagnostics(tmp_path)
    assert report == {'restored_latest_index_dates': {'KOSPI': '2026-11-13', 'KOSDAQ': '2026-11-12'},
                      'restored_index_metadata_status': 'available'}
    assert '99123' not in json.dumps(report)
    database.write_bytes(b'corrupt, not a sqlite database')
    corrupt = MODULE.restored_cache_diagnostics(tmp_path)
    assert corrupt == {'restored_latest_index_dates': {}, 'restored_index_metadata_status': 'unavailable'}


def test_cache_open_phase_emits_dates_from_decrypted_snapshot(tmp_path, monkeypatch):
    import base64
    import sqlite3
    import sys

    root = tmp_path / 'workspace'
    source = tmp_path / 'encrypted-input'
    source.mkdir()
    with sqlite3.connect(source / 'index_forecast.sqlite') as db:
        db.execute('CREATE TABLE index_eod (index_code TEXT, session_date TEXT, close REAL)')
        db.execute("INSERT INTO index_eod VALUES ('KOSPI', '2026-11-13', 991234.0)")
    key = base64.urlsafe_b64encode(b'k' * 32).decode().rstrip('=')
    MODULE.state.seal_private_cache(source, root / '.forecast-cache/state.enc', key=key)
    emitted = []
    monkeypatch.setattr(MODULE, 'ROOT', root)
    monkeypatch.setattr(MODULE, 'emit', emitted.append)
    monkeypatch.setattr(sys, 'argv', ['accuracy_operation.py', '--phase', 'cache-open', '--mode', 'latest'])
    monkeypatch.setenv('FORECAST_CACHE_KEY', key)
    MODULE.main()
    assert emitted == [{'status': 'restored', 'restored_latest_index_dates': {'KOSPI': '2026-11-13'},
                        'restored_index_metadata_status': 'available'}]
    assert '991234' not in json.dumps(emitted)

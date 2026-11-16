"""Point-in-time vendor observations of completed Korean index closes.

Vendor history is retrospective: its original publication time is unknown. A
receipt is evidence only from its actual capture onward, never before it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from urllib.request import Request, urlopen

import pandas as pd

from .reliable_inputs import KST, session_schedule, utc_timestamp
from .sqlite_store import connect_index_forecast

MARKETS = {'KOSPI': '^KS11', 'KOSDAQ': '^KQ11'}
COLUMNS = ['index_code', 'session_date', 'open', 'high', 'low', 'close', 'volume',
           'turnover', 'source', 'published_at', 'ingested_at']
SCHEMA = '''CREATE TABLE IF NOT EXISTS market_close_observation (
 id INTEGER PRIMARY KEY AUTOINCREMENT, index_code TEXT NOT NULL, session_date TEXT NOT NULL,
 open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL,
 source TEXT NOT NULL, published_at TEXT, received_at TEXT NOT NULL, status TEXT NOT NULL
)'''


def _number(value):
    try:
        value = float(str(value).replace(',', ''))
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def _rounded(value):
    try:
        return Decimal(str(value)).quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def _normalize_naver(row):
    if not isinstance(row, dict):
        return None
    day = row.get('localTradedAt')
    try:
        if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
            return None
    except ValueError:
        return None
    result = {'session_date': day}
    for key, original in [('open', 'openPrice'), ('high', 'highPrice'), ('low', 'lowPrice'), ('close', 'closePrice')]:
        result[key] = _number(row.get(original))
    if any(result[key] is None for key in ('open', 'high', 'low', 'close')):
        return None
    if not (result['low'] <= min(result['open'], result['close']) <= max(result['open'], result['close']) <= result['high']):
        return None
    return result


def fetch_naver(market):
    url = f'https://m.stock.naver.com/api/index/{market}/price?pageSize=10&page=1'
    with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}), timeout=10) as response:
        data = json.load(response)
    if not isinstance(data, list):
        raise ValueError('invalid Naver response')
    return data


def fetch_yahoo(market):
    import yfinance as yf
    frame = yf.download(MARKETS[market], period='10d', auto_adjust=False, progress=False, threads=False)
    if frame is None or frame.empty:
        return []
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    return [{'session_date': str(date)[:10], 'close': row.get('Close')} for date, row in frame.iterrows()]


def _ensure(conn):
    conn.execute(SCHEMA)
    conn.execute('CREATE INDEX IF NOT EXISTS idx_market_close_receipt ON market_close_observation(index_code, session_date, received_at, id)')


def record_observation(conn, row):
    _ensure(conn)
    cols = ['index_code', 'session_date', 'open', 'high', 'low', 'close', 'volume', 'turnover',
            'source', 'published_at', 'received_at', 'status']
    conn.execute(f"INSERT INTO market_close_observation ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", [row.get(c) for c in cols])
    conn.commit()


def effective_index_eod_frame(db_path: Path | str, *, cutoff: datetime | str) -> pd.DataFrame:
    """Read original first-writer history; expose received revisions separately.

    Old rows remain the original first-writer values so a later correction cannot
    rewrite OOF feature history. New vendor-only rows are unavailable before receipt.
    Conflicting latest observations remove that market/date from the effective set.
    """
    stamp = utc_timestamp(cutoff)
    if pd.isna(stamp):
        raise ValueError('cutoff required')
    conn = sqlite3.connect(str(db_path))
    try:
        base = pd.read_sql_query('SELECT * FROM index_eod', conn)
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_close_observation'").fetchone()
        if not exists:
            return base
        obs = pd.read_sql_query('SELECT * FROM market_close_observation ORDER BY id', conn)
    finally:
        conn.close()
    if obs.empty:
        return base
    obs['receipt'] = pd.to_datetime(obs['received_at'], utc=True, errors='coerce')
    # First writer rows inserted by this collector cannot exist before their receipt.
    first = obs.groupby(['index_code', 'session_date'], sort=False).first().reset_index()
    captured = {(r.index_code, r.session_date): r.receipt for r in first.itertuples()}
    if not base.empty:
        mask = []
        for row in base.itertuples():
            key = (row.index_code, row.session_date)
            ingested = pd.to_datetime(row.ingested_at, utc=True, errors='coerce')
            first_receipt = captured.get(key)
            collector_row = str(row.source).startswith(('Naver index API', 'Yahoo index API'))
            mask.append(not collector_row or (pd.notna(ingested) and ingested <= stamp and pd.notna(first_receipt) and first_receipt <= stamp))
        base = base.loc[mask].copy()
    available = obs.loc[obs['receipt'].notna() & (obs['receipt'] <= stamp)].copy()
    if available.empty:
        return base.reset_index(drop=True)
    revisions = available.sort_values(['receipt', 'id']).drop_duplicates(['index_code', 'session_date'], keep='last')
    live_revisions = revisions.to_dict('records')
    conflicts = [row for row in live_revisions if row['status'] == 'conflict']
    day = stamp.tz_convert(KST).strftime('%Y-%m-%d')
    # The latest completed observation may be from before today (holidays and
    # before the next close). A conflict must not become a usable last close.
    for row in conflicts:
        market = row['index_code']
        dates = base.loc[(base['index_code'] == market) & (base['session_date'] <= day), 'session_date']
        if not dates.empty and row['session_date'] == dates.max():
            base = base.loc[~((base['index_code'] == market) & (base['session_date'] == row['session_date']))]
    base = base.reset_index(drop=True)
    base.attrs['live_revisions'] = live_revisions
    base.attrs['live_conflicts'] = [(row['index_code'], row['session_date']) for row in conflicts]
    return base


def apply_live_revisions(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply receipt-eligible revisions only for current, not historical, features."""
    result = frame.copy()
    for row in frame.attrs.get('live_revisions', []):
        if row.get('status') == 'conflict':
            mask = (result['index_code'] == row['index_code']) & (result['session_date'] == row['session_date'])
            result.loc[mask, 'close'] = float('nan')
            continue
        if row.get('status') not in ('verified', 'single_source'):
            continue
        mask = (result['index_code'] == row['index_code']) & (result['session_date'] == row['session_date'])
        if not mask.any():
            continue
        for column in COLUMNS:
            value = row.get('received_at') if column == 'ingested_at' else row.get(column)
            if value is not None and pd.notna(value):
                result.loc[mask, column] = value
    return result


def _expected_session(now, sessions):
    fetched = utc_timestamp(now)
    if pd.isna(fetched):
        raise ValueError('actual time required')
    day = fetched.tz_convert(KST).normalize().tz_localize(None)
    schedule, _ = session_schedule(day - timedelta(days=14), day + timedelta(days=1), sessions)
    eligible = schedule.loc[schedule['close_at'].notna() & (schedule['close_at'] <= fetched)].sort_values('date')
    return None if eligible.empty else str(eligible.iloc[-1]['date'])[:10]


def collect_market_closes(db_path, *, now, fetch_naver=fetch_naver, fetch_yahoo=fetch_yahoo, sessions=None, receipt_clock=None):
    expected = _expected_session(now, sessions)
    conn = connect_index_forecast(db_path)
    _ensure(conn)
    result = {'schema_version': 'market_closes_v1', 'run_id': utc_timestamp(now).isoformat(),
              'checked_at': utc_timestamp(now).isoformat(), 'markets': {}}
    try:
        for market in MARKETS:
            raw = []
            for attempt in range(2):
                try:
                    raw = fetch_naver(market)
                    break
                except OSError as error:
                    # One immediate retry for transport errors, 429 and 5xx.
                    status = getattr(error, 'code', None)
                    if attempt or (status is not None and status != 429 and status < 500):
                        break
                except (ValueError, TypeError):
                    break
            naver = [item for r in raw if (item := _normalize_naver(r)) is not None] if isinstance(raw, list) else []
            try:
                yahoo = fetch_yahoo(market)
            except Exception:
                yahoo = []
            receipt = receipt_clock() if receipt_clock else now
            receipt = pd.Timestamp(receipt).isoformat()
            # Retrospective bars are added as immutable base rows, with actual receipt.
            for bar in naver:
                if not expected or bar['session_date'] > expected:
                    continue
                try:
                    schedule, _ = session_schedule(bar['session_date'], bar['session_date'], sessions)
                except (ValueError, TypeError, OverflowError):
                    continue
                if schedule.empty or pd.isna(schedule.iloc[0]['close_at']) or schedule.iloc[0]['close_at'] > utc_timestamp(now):
                    continue
                comparisons = [_number(r.get('close')) for r in yahoo if str(r.get('session_date', ''))[:10] == bar['session_date']]
                comparisons = [v for v in comparisons if v is not None]
                status = 'conflict' if any(_rounded(v) != _rounded(bar['close']) for v in comparisons) else ('verified' if comparisons else 'single_source')
                previous = conn.execute('SELECT status FROM market_close_observation WHERE index_code=? AND session_date=? ORDER BY id DESC LIMIT 1', (market, bar['session_date'])).fetchone()
                if status == 'single_source' and previous and previous['status'] == 'conflict':
                    status = 'conflict'
                row = {'index_code': market, **bar, 'source': 'Naver index API; original publication time unknown',
                       'published_at': None, 'received_at': receipt, 'status': status}
                record_observation(conn, row)
                if status != 'conflict':
                    vals = [row.get(c) if c != 'ingested_at' else receipt for c in COLUMNS]
                    conn.execute(f"INSERT OR IGNORE INTO index_eod ({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)})", vals)
                    conn.commit()
            latest = next((b for b in naver if b['session_date'] == expected), None)
            if latest is None and expected:
                ybar = next((r for r in yahoo if str(r.get('session_date', ''))[:10] == expected and _number(r.get('close')) is not None), None)
                if ybar:
                    previous = conn.execute('SELECT status FROM market_close_observation WHERE index_code=? AND session_date=? ORDER BY id DESC LIMIT 1', (market, expected)).fetchone()
                    status = 'conflict' if previous and previous['status'] == 'conflict' else 'single_source'
                    row = {'index_code': market, 'session_date': expected, 'close': _number(ybar['close']),
                           'source': 'Yahoo index API; original publication time unknown', 'published_at': None,
                           'received_at': receipt, 'status': status}
                    record_observation(conn, row)
                    if status != 'conflict':
                        vals = [row.get(c) if c != 'ingested_at' else receipt for c in COLUMNS]
                        conn.execute(f"INSERT OR IGNORE INTO index_eod ({','.join(COLUMNS)}) VALUES ({','.join('?' for _ in COLUMNS)})", vals)
                        conn.commit()
                    latest = row
            if latest:
                row = conn.execute('SELECT * FROM market_close_observation WHERE index_code=? AND session_date=? ORDER BY id DESC LIMIT 1', (market, expected)).fetchone()
                if row:
                    result['markets'][market] = {'session_date': expected, 'close': row['close'] if row['status'] != 'conflict' else None,
                        'source': row['source'], 'received_at': row['received_at'], 'published_at': None, 'status': row['status']}
                    continue
            prior = conn.execute("SELECT * FROM market_close_observation WHERE index_code=? AND session_date<=? ORDER BY session_date DESC,id DESC LIMIT 1", (market, expected)).fetchone()
            if prior and prior['status'] == 'conflict':
                result['markets'][market] = {'session_date': prior['session_date'], 'close': None,
                    'source': prior['source'], 'received_at': prior['received_at'], 'published_at': None, 'status': 'conflict'}
                continue
            result['markets'][market] = ({'session_date': prior['session_date'], 'close': prior['close'], 'source': prior['source'],
                'received_at': prior['received_at'], 'published_at': None, 'status': 'stale'} if prior else
                {'session_date': None, 'close': None, 'source': None, 'received_at': None, 'published_at': None, 'status': 'unavailable'})
    finally:
        conn.close()
    return result


def _atomic_bytes(path: Path, payload: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def preserve_previous_closes(data, output):
    """Keep only verified prior dated values on a failed fresh collection."""
    output = Path(output)
    try:
        raw = output.read_bytes()
        old = json.loads(raw)
        receipt = json.loads(output.with_name('market_closes_publication.json').read_bytes())
        if (receipt.get('schema_version') != 'market_closes_publication_v1' or
                old.get('schema_version') != 'market_closes_v1' or
                receipt.get('run_id') != old.get('run_id') or not receipt.get('publication_checked_at') or
                receipt.get('payload_checksums', {}).get('market_closes.json') != hashlib.sha256(raw).hexdigest()):
            return data
        result = {**data, 'markets': dict(data['markets'])}
        for market, current in data['markets'].items():
            previous = old.get('markets', {}).get(market, {})
            if (previous.get('status') == 'conflict' and previous.get('session_date') and
                    current.get('status') in ('single_source', 'stale', 'unavailable') and
                    (not current.get('session_date') or current['session_date'] <= previous['session_date'])):
                result['markets'][market] = {**previous, 'close': None, 'status': 'conflict'}
                continue
            if (current.get('status') in ('stale', 'unavailable') and previous.get('session_date') and
                    _number(previous.get('close')) and
                    (not current.get('session_date') or previous['session_date'] > current['session_date'])):
                result['markets'][market] = {**previous, 'status': 'stale'}
        return result
    except (OSError, ValueError, TypeError, KeyError):
        return data


def write_market_closes(data, output, *, published_at):
    output = Path(output)
    raw = (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8')
    _atomic_bytes(output, raw)
    # Re-read the bytes that will actually be served, rather than hashing an in-memory object.
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    checked_at = published_at() if callable(published_at) else published_at
    receipt = {'schema_version': 'market_closes_publication_v1', 'run_id': data['run_id'],
               'payload_checksums': {'market_closes.json': checksum},
               'publication_checked_at': pd.Timestamp(checked_at).isoformat()}
    _atomic_bytes(output.with_name('market_closes_publication.json'),
                  (json.dumps(receipt, sort_keys=True, separators=(',', ':')) + '\n').encode('utf-8'))
    return receipt

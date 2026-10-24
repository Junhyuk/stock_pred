"""SQLite schema, provenance, and safe idempotent write helpers."""
from __future__ import annotations
import contextlib
from datetime import datetime, timezone
from pathlib import Path
import sqlite3

SCHEMA = Path(__file__).with_name('schema.sql')
DEFAULT_DB = Path(__file__).resolve().parent.parent / 'data' / 'smartmoney.sqlite'

SOURCES = [
 ('KRX_API','KRX','KRX OPEN API','https://openapi.krx.co.kr/','official_api','Register API; honor license, quotas and redistribution terms','daily'),
 ('KRX_CSV','KRX','KRX Market Data CSV','https://data.krx.co.kr/','manual_csv','Download from your own account; license check before automation/reuse','daily'),
 ('DART','FSS','OpenDART Large Shareholders','https://opendart.fss.or.kr/','official_api','Obtain API key; publication timestamp may be later than receipt date','event'),
 ('SEC13F','SEC','SEC 13F Bulk Files','https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets','official_zip','Declare User-Agent; obey SEC fair access; quarterly holdings only','quarterly'),
 ('FINRA_REGSHO','FINRA','FINRA Daily Short Sale Volume','https://www.finra.org/finra-data/browse-catalog/short-sale-volume-data','public_file','Daily short sale volume is NOT outstanding short interest','daily'),
 ('CFTC_TFF','CFTC','CFTC TFF Commitments of Traders','https://www.cftc.gov/MarketReports/CommitmentsofTraders/HistoricalCompressed/index.htm','official_zip','Futures-market investor categories; not single-stock positions','weekly'),
 ('WEB_TABLE','user_authorized','Public HTML table','https://example.org/','manual_allowlist','Only user-authorized HTTPS domains, public pages, TOS/robots compliant','variable'),
 ('MANUAL','user','User supplied normalized CSV','https://example.org/','manual_csv','Provenance/date must be supplied by user; no claimed historical publication','variable'),
]
INSTRUMENTS = [
 ('KR','005930','삼성전자','KRW','KOSPI'),
 ('KR','000660','SK하이닉스','KRW','KOSPI'),
 ('US','AAPL','Apple','USD','NASDAQ'),
 ('US','NVDA','NVIDIA','USD','NASDAQ'),
]

def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f'DB not found: {p}. Run init-db --db ... first')
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    c.execute('PRAGMA busy_timeout=10000')
    c.execute('PRAGMA journal_mode=WAL')
    return c


def initialize(path: str | Path = DEFAULT_DB) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True,exist_ok=True)
    c = sqlite3.connect(p)
    c.executescript(SCHEMA.read_text(encoding='utf-8'))
    t = utcnow()
    c.execute('INSERT OR IGNORE INTO schema_version(version,installed_at) VALUES(1,?)',(t,))
    c.executemany('INSERT OR IGNORE INTO sources(code,provider,display_name,home_url,method,legal_note,cadence) VALUES(?,?,?,?,?,?,?)', SOURCES)
    c.executemany('INSERT OR IGNORE INTO instruments(market,symbol,name,currency,exchange,created_at) VALUES(?,?,?,?,?,?)', [(a,b,c_,d,e,t) for a,b,c_,d,e in INSTRUMENTS])
    c.commit(); c.close()


def upsert_instrument(conn, market, symbol, name=None):
    market = market.upper().strip()
    symbol = symbol.strip().upper() if market == 'US' else symbol.strip().zfill(6)
    if market not in ('KR','US') or not symbol:
        raise ValueError('market must be KR/US and symbol required')
    conn.execute('INSERT OR IGNORE INTO instruments(market,symbol,name,currency,created_at) VALUES(?,?,?,?,?)',
                 (market,symbol,name or symbol,'KRW' if market=='KR' else 'USD',utcnow()))
    if name and name.strip() and name != symbol:
        conn.execute('UPDATE instruments SET name=? WHERE market=? AND symbol=? AND name=symbol',(name,market,symbol))
    return symbol


def start_run(conn, source, source_url=None, sha256=None):
    cur=conn.execute('INSERT INTO ingest_runs(source_code,source_url,content_sha256,started_at,status) VALUES(?,?,?,?,?)',
                     (source,source_url,sha256,utcnow(),'running'))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, id_, status='ok', seen=0, written=0, message=''):
    conn.execute('UPDATE ingest_runs SET finished_at=?,status=?,rows_seen=?,rows_written=?,message=? WHERE id=?',
                 (utcnow(),status,seen,written,message,id_))
    conn.commit()


def record_issue(conn, source, category, detail, run_id=None):
    conn.execute('INSERT INTO quality_issues(ingest_run_id,source_code,category,detail,created_at) VALUES(?,?,?,?,?)',
                 (run_id,source,category,str(detail)[:2000],utcnow()))


def insert_row(conn, table, obj, key_cols):
    """Updates on identical natural keys; stable for repeated imports."""
    keys=list(obj)
    values=[obj[k] for k in keys]
    updates=[k for k in keys if k not in key_cols]
    sql=f"INSERT INTO {table} ({','.join(keys)}) VALUES ({','.join(['?']*len(keys))})"
    if updates:
        sql+=' ON CONFLICT('+','.join(key_cols)+') DO UPDATE SET '+','.join(f'{k}=excluded.{k}' for k in updates)
    conn.execute(sql,values)

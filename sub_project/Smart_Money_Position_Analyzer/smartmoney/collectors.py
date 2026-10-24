"""Verified official HTTP source collectors. Never scrape hidden JSON endpoints."""
from __future__ import annotations
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urlparse, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .db import utcnow, upsert_instrument, insert_row, start_run, finish_run, record_issue
from .normalize import day, number, parse_available
from .importers import import_text, _rows

class SecureRedirect(HTTPRedirectHandler):
    def __init__(self, allow_hosts): super().__init__(); self.allow_hosts=set(allow_hosts)
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_url(newurl,self.allow_hosts)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def _validate_url(url, allowed_hosts):
    p=urlparse(url)
    if p.scheme != 'https' or p.hostname not in allowed_hosts or p.username or p.password or p.port:
        raise ValueError('HTTPS URL host not allowlisted or URL contains unsupported credentials/port')


def fetch_bytes(url, allowed_hosts, *, user_agent='SmartMoneyResearch/0.1 (personal analysis)',
                headers=None, max_bytes=35_000_000, timeout=30):
    _validate_url(url, set(allowed_hosts))
    h={'User-Agent':user_agent,'Accept':'application/json,text/plain,text/html,application/zip,*/*'}
    h.update(headers or {})
    req=Request(url,headers=h)
    with build_opener(SecureRedirect(allowed_hosts)).open(req,timeout=timeout) as resp:
        chunks=[]; received=0
        while True:
            buf=resp.read(min(1024*512,max_bytes-received+1))
            if not buf: break
            received+=len(buf)
            if received>max_bytes: raise ValueError('Response exceeds configured size limit')
            chunks.append(buf)
        return b''.join(chunks)


def fetch_to_file(url, dest, allowed_hosts, user_agent=None, max_bytes=250_000_000):
    """Streaming download: SEC archives are ~100MB. No download if destination already exists."""
    _validate_url(url,set(allowed_hosts))
    dest=Path(dest)
    dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists(): raise FileExistsError(f'Already exists: {dest}')
    partial=dest.with_name(dest.name+'.part')
    req=Request(url,headers={'User-Agent':user_agent or 'SmartMoneyResearch/0.1 (research use)'})
    size=0
    try:
        with build_opener(SecureRedirect(allowed_hosts)).open(req,timeout=120) as resp,partial.open('wb') as out:
            while chunk:=resp.read(512*1024):
                size+=len(chunk)
                if size>max_bytes: raise ValueError('Download too large')
                out.write(chunk)
        partial.rename(dest)
    finally:
        if partial.exists(): partial.unlink()
    return str(dest)


def fetch_krx_prices(conn, trade_date, exchange, auth_key):
    if not auth_key: raise ValueError('KRX_AUTH_KEY environment variable is missing')
    api={'kospi':'stk_bydd_trd','kosdaq':'ksq_bydd_trd'}
    exchange=exchange.lower()
    if exchange not in api: raise ValueError('market must be kospi or kosdaq')
    d=day(trade_date)
    url=f'https://data-dbg.krx.co.kr/svc/apis/sto/{api[exchange]}?{urlencode({"basDd":d.replace("-","")})}'
    raw=fetch_bytes(url,{'data-dbg.krx.co.kr'},headers={'AUTH_KEY':auth_key})
    payload=json.loads(raw)
    if not isinstance(payload,dict) or not isinstance(payload.get('OutBlock_1'),list):
        raise ValueError(f'Unexpected KRX response; check registration/key/contract: {str(payload)[:400]}')
    run=start_run(conn,'KRX_API',url,hashlib.sha256(raw).hexdigest())
    seen=written=0
    try:
        with conn:
            for rec in payload['OutBlock_1']:
                seen+=1
                symbol=upsert_instrument(conn,'KR',str(rec.get('ISU_CD','')),
                                         str(rec.get('ISU_NM') or ''))
                trd=day(rec.get('BAS_DD') or d)
                insert_row(conn,'prices',dict(market='KR',symbol=symbol,trade_date=trd,
                    close=number(rec.get('TDD_CLSPRC'),required=True),
                    open=number(rec.get('TDD_OPNPRC')),high=number(rec.get('TDD_HGPRC')),
                    low=number(rec.get('TDD_LWPRC')),volume=number(rec.get('ACC_TRDVOL')),
                    traded_value=number(rec.get('ACC_TRDVAL')),listed_shares=number(rec.get('LIST_SHRS')),
                    source_code='KRX_API',source_url=url,available_at=utcnow(),ingested_at=utcnow(),
                    ingest_run_id=run),['market','symbol','trade_date','source_code'])
                written+=1
        finish_run(conn,run,'ok',seen,written)
    except Exception as e:
        conn.rollback();record_issue(conn,'KRX_API','FETCH_ERROR',str(e),run)
        finish_run(conn,run,'failed',seen,0,str(e));raise
    return {'rows_seen':seen,'rows_written':written,'run_id':run,'as_of':d,'source':url}


def fetch_dart_major(conn, corp_code, symbol, api_key):
    corp_code=str(corp_code)
    if not re.fullmatch(r'\d{8}',corp_code): raise ValueError('DART corp_code must be 8 digits, not 6 digit ticker')
    if not api_key: raise ValueError('DART_API_KEY env variable missing')
    symbol=upsert_instrument(conn,'KR',symbol)
    url='https://opendart.fss.or.kr/api/majorstock.json'
    request_url=f'{url}?{urlencode({"crtfc_key":api_key,"corp_code":corp_code})}'
    # NEVER store URL with token: all provenance uses a sanitized URL.
    raw=fetch_bytes(request_url,{'opendart.fss.or.kr'})
    obj=json.loads(raw)
    status=obj.get('status')
    if status not in ('000','013'):
        raise ValueError(f'DART API status={status}; message={obj.get("message")}')
    conn.execute('UPDATE instruments SET dart_corp_code=? WHERE market=? AND symbol=?',(corp_code,'KR',symbol))
    rows=obj.get('list',[]) if status=='000' else []
    run=start_run(conn,'DART',f'{url}?corp_code={corp_code}',hashlib.sha256(raw).hexdigest())
    count=0
    try:
        with conn:
            for row in rows:
                rno=str(row.get('rcept_no') or '')
                if not rno: raise ValueError('missing DART receipt number')
                insert_row(conn,'dart_major_holdings',dict(receipt_no=rno,corp_code=corp_code,
                    symbol=symbol,reporter=str(row.get('repror') or 'UNKNOWN'),
                    report_date=day(row['rcept_dt']),shares=number(row.get('stkqy')),
                    shares_change=number(row.get('stkqy_irds')),
                    ownership_pct=number(row.get('stkrt')),
                    ownership_pct_change=number(row.get('stkrt_irds')),
                    report_reason=row.get('report_resn'),source_code='DART',
                    source_url=f'https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}',
                    available_at=utcnow(),ingested_at=utcnow(),ingest_run_id=run),
                    ['receipt_no','corp_code','reporter'])
                count+=1
        finish_run(conn,run,'ok',len(rows),count)
    except Exception as e:
        conn.rollback();record_issue(conn,'DART','IMPORT_ERROR',str(e),run)
        finish_run(conn,run,'failed',len(rows),0,str(e));raise
    return {'rows_seen':len(rows),'rows_written':count,'status':status,'run_id':run}


def fetch_finra_short_volume(conn, trade_date, symbols=None):
    date8=day(trade_date).replace('-','')
    url=f'https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date8}.txt'
    raw=fetch_bytes(url,{'cdn.finra.org'},max_bytes=30_000_000)
    text=raw.decode('utf-8-sig')
    if 'ShortVolume' not in text.splitlines()[0]:
        raise ValueError('FINRA file does not have expected ShortVolume header')
    if symbols:
        wanted={x.strip().upper() for x in symbols}
        r=list(_rows(text))
        r=[x for x in r if str(x.get('Symbol','')).upper() in wanted]
        if r:
            out=io.StringIO()
            w=csv.DictWriter(out,fieldnames=list(r[0]),delimiter='|')
            w.writeheader();w.writerows(r);text=out.getvalue()
        else:
            text='Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n'
    return import_text(conn,'us-short-volume',text,source='FINRA_REGSHO',source_url=url,
                       market='US',sha256=hashlib.sha256(raw).hexdigest())

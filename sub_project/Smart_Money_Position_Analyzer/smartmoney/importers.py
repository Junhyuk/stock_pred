"""Manual CSV/FINRA text ingestion into auditable SQLite tables."""
from __future__ import annotations
import csv
import hashlib
import io
import json
from pathlib import Path
from .db import utcnow, upsert_instrument, insert_row, start_run, finish_run, record_issue
from .normalize import number, day, parse_available

ALIASES={
 'symbol':['symbol','ticker','종목코드','단축코드','ISU_CD','Symbol'],
 'trade_date':['trade_date','date','Date','일자','기준일','일시','BAS_DD'],
 'close':['close','종가','TDD_CLSPRC'], 'open':['open','시가'],
 'high':['high','고가'], 'low':['low','저가'],
 'volume':['volume','거래량','ACC_TRDVOL'],
 'traded_value':['traded_value','거래대금','ACC_TRDVAL'],
 'listed_shares':['listed_shares','상장주식수','LIST_SHRS'],
 'investor_type':['investor_type','투자자구분','투자자','investor'],
 'buy_qty':['buy_qty','매수거래량','매수수량'],
 'sell_qty':['sell_qty','매도거래량','매도수량'],
 'net_qty':['net_qty','순매수거래량','순매수수량','순매수량'],
 'buy_value':['buy_value','매수거래대금','매수금액'],
 'sell_value':['sell_value','매도거래대금','매도금액'],
 'net_value':['net_value','순매수거래대금','순매수금액'],
 'held_shares':['held_shares','foreign_held_shares','외국인보유수량','외국인 보유수량'],
 'ownership_pct':['ownership_pct','foreign_ownership_pct','외국인지분율','외국인 지분율','외국인보유비율'],
 'settlement_date':['settlement_date','기준일','date'],
 'short_interest_shares':['short_interest_shares','short_interest','공매도잔고수량'],
 'days_to_cover':['days_to_cover'],
 'reporting_facility':['reporting_facility','facility'],
 'listing_market':['listing_market','Market'],
 'short_volume':['short_volume','ShortVolume','Short Volume'],
 'short_exempt_volume':['short_exempt_volume','ShortExemptVolume'],
 'total_volume':['total_volume','TotalVolume','Total Volume'],
 'cusip':['cusip','CUSIP'],
 'valid_from':['valid_from'], 'valid_to':['valid_to'],
 'mapping_source':['mapping_source'],
 'available_at':['available_at','published_at'],
 'name':['name','종목명','ISU_NM'],
}
INV={'FOREIGN':'FOREIGN','외국인':'FOREIGN','외국인합계':'FOREIGN',
     'INSTITUTION':'INSTITUTION','기관':'INSTITUTION','기관합계':'INSTITUTION','기관계':'INSTITUTION',
     '개인':'RETAIL','RETAIL':'RETAIL','기타':'OTHER','OTHER':'OTHER'}


def _get(row, canonical, mapping=None, default=None):
    mapping=mapping or {}
    mapped=mapping.get(canonical)
    if mapped is not None:
        if mapped not in row: raise ValueError(f'mapping {canonical} -> {mapped} not found in CSV headers: {list(row)}')
        return row[mapped]
    for candidate in ALIASES.get(canonical,[canonical]):
        if candidate in row: return row[candidate]
    return default


def _read_file(path):
    data=Path(path).read_bytes()
    for codec in ('utf-8-sig','cp949'):
        try: text=data.decode(codec); break
        except UnicodeDecodeError: continue
    else: raise ValueError('CSV must be utf-8-sig or cp949')
    return text,hashlib.sha256(data).hexdigest()


def _rows(text):
    first=text[:4096]
    try: dialect=csv.Sniffer().sniff(first,delimiters=',\t|;')
    except csv.Error: dialect=csv.excel
    r=csv.DictReader(io.StringIO(text),dialect=dialect)
    if not r.fieldnames: raise ValueError('Missing CSV header')
    for row in r:
        if None in row: raise ValueError('Incorrect column count in CSV')
        if not any(str(v or '').strip() for v in row.values()): continue
        # FINRA trailers and comments are not observations
        first_val=next(iter(row.values()),'') or ''
        if str(first_val).startswith('File Creation Time') or str(first_val).startswith('Record Count'):
            continue
        yield row


def _num(row,key,mapping,required=False):
    return number(_get(row,key,mapping),required=required)


def _normalize_row(conn, kind, row, mapping, source, url, default_market, default_investor, pub, run_id):
    ts=utcnow()
    av=parse_available(_get(row,'available_at',mapping) or pub)
    metadata=dict(source_code=source,source_url=url,available_at=av,ingested_at=ts,ingest_run_id=run_id)
    if kind=='cusip-map':
        cusip=str(_get(row,'cusip',mapping) or '').strip().upper()
        symbol=str(_get(row,'symbol',mapping) or '').strip().upper()
        if not cusip or not symbol: raise ValueError('cusip and symbol required')
        upsert_instrument(conn,'US',symbol)
        return 'cusip_symbol_map',dict(cusip=cusip,symbol=symbol,valid_from=day(_get(row,'valid_from',mapping) or '1900-01-01'),
            valid_to=day(_get(row,'valid_to',mapping)) if _get(row,'valid_to',mapping) else None,
            verified_at=ts,mapping_source=str(_get(row,'mapping_source',mapping) or url or 'user_verified')),['cusip','symbol','valid_from']
    market=('US' if kind=='us-short-volume' else default_market).upper()
    symbol=_get(row,'symbol',mapping)
    if not symbol: raise ValueError('symbol missing')
    symbol=upsert_instrument(conn,market,str(symbol),_get(row,'name',mapping))
    dt=day(_get(row,'trade_date',mapping) or _get(row,'settlement_date',mapping))
    if kind=='price':
        return 'prices',dict(market=market,symbol=symbol,trade_date=dt,open=_num(row,'open',mapping),
            high=_num(row,'high',mapping),low=_num(row,'low',mapping),close=_num(row,'close',mapping,True),
            volume=_num(row,'volume',mapping),traded_value=_num(row,'traded_value',mapping),
            listed_shares=_num(row,'listed_shares',mapping),**metadata),['market','symbol','trade_date','source_code']
    if kind=='kr-flow':
        if market!='KR': raise ValueError('kr-flow requires KR market')
        inv=str(_get(row,'investor_type',mapping) or default_investor or '').strip()
        inv=INV.get(inv.upper()) or INV.get(inv)
        if not inv: raise ValueError('unrecognized investor_type; use --investor FOREIGN/INSTITUTION or investor_type column')
        buy,sell,net=(_num(row,k,mapping) for k in ('buy_qty','sell_qty','net_qty'))
        buy_v,sell_v,net_v=(_num(row,k,mapping) for k in ('buy_value','sell_value','net_value'))
        if net is None and buy is not None and sell is not None: net=buy-sell
        if net_v is None and buy_v is not None and sell_v is not None: net_v=buy_v-sell_v
        if net is None and net_v is None: raise ValueError('net_qty or net_value must be present; cannot infer share quantity from KRW')
        if buy is not None and sell is not None and net is not None and abs((buy-sell)-net)>max(1e-6,1e-8*abs(net)):
            raise ValueError('net_qty != buy_qty - sell_qty')
        return 'kr_investor_flows',dict(symbol=symbol,trade_date=dt,investor_type=inv,
            buy_qty=buy,sell_qty=sell,net_qty=net,
            buy_value=buy_v,sell_value=sell_v,net_value=net_v,**metadata),['symbol','trade_date','investor_type','source_code']
    if kind=='kr-ownership':
        if market!='KR': raise ValueError('kr-ownership requires KR market')
        return 'kr_foreign_ownership',dict(symbol=symbol,trade_date=dt,held_shares=_num(row,'held_shares',mapping),
            ownership_pct=_num(row,'ownership_pct',mapping,True),**metadata),['symbol','trade_date','source_code']
    if kind=='short-interest':
        return 'short_interest',dict(market=market,symbol=symbol,settlement_date=dt,
            shares=_num(row,'short_interest_shares',mapping,True),
            days_to_cover=_num(row,'days_to_cover',mapping),**metadata),['market','symbol','settlement_date','source_code']
    if kind=='us-short-volume':
        return 'us_short_volume',dict(symbol=symbol,trade_date=dt,
            reporting_facility=str(_get(row,'reporting_facility',mapping) or ('CNMS' if 'CNMSshvol' in str(url) else 'UNKNOWN')),
            listing_market=_get(row,'listing_market',mapping),
            short_volume=_num(row,'short_volume',mapping,True),
            short_exempt_volume=_num(row,'short_exempt_volume',mapping),
            total_volume=_num(row,'total_volume',mapping,True),**metadata),['symbol','trade_date','reporting_facility','source_code']
    raise ValueError(f'unknown import kind: {kind}')


def import_text(conn, kind, text, *, source=None, source_url=None, market='KR', investor=None,
                mapping=None, published_at=None, sha256=None):
    defaults={'price':'MANUAL','kr-flow':'KRX_CSV','kr-ownership':'KRX_CSV',
              'short-interest':'MANUAL','us-short-volume':'FINRA_REGSHO','cusip-map':'MANUAL'}
    if kind not in defaults: raise ValueError(f'unsupported kind: {kind}')
    source=source or defaults[kind]
    run_id=start_run(conn,source,source_url,sha256)
    seen=written=0
    try:
        with conn:
            for seen,row in enumerate(_rows(text),start=1):
                table,obj,key=_normalize_row(conn,kind,row,mapping,source,source_url,market,investor,published_at,run_id)
                insert_row(conn,table,obj,key); written+=1
        finish_run(conn,run_id,'ok',seen,written)
    except Exception as e:
        conn.rollback()
        record_issue(conn,source,'IMPORT_ERROR',str(e),run_id)
        finish_run(conn,run_id,'failed',seen,0,str(e))
        raise
    return {'run_id':run_id,'rows_seen':seen,'rows_written':written,'source':source}


def import_file(conn,kind,path,**kwargs):
    text,sha=_read_file(path)
    return import_text(conn,kind,text,sha256=sha,source_url=kwargs.pop('source_url',None) or Path(path).resolve().as_uri(),**kwargs)

"""Streaming SEC Form 13F TSV and CFTC TFF archival ZIP readers."""
from __future__ import annotations
import csv
from datetime import date
import hashlib
import io
from pathlib import Path
import re
import zipfile
from .db import utcnow, insert_row, start_run, finish_run, record_issue
from .normalize import day,number,published_from_sec_date,parse_available
from .collectors import fetch_bytes


def _find(z, basename):
    found=[n for n in z.namelist() if Path(n).name.upper()==basename.upper()]
    if len(found)!=1: raise ValueError(f'SEC ZIP expected exactly one {basename} file; found {found}')
    return found[0]


def _tsv_reader(z, basename):
    name=_find(z,basename)
    with z.open(name,'r') as binary:
        with io.TextIOWrapper(binary,encoding='utf-8-sig',newline='') as f:
            for row in csv.DictReader(f,delimiter='\t'):
                if None in row: raise ValueError(f'malformed TSV {basename}')
                yield row


def _hash_file(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        while b:=f.read(1024*1024): h.update(b)
    return h.hexdigest()


def import_sec13f_zip(conn, zip_path, *, manager_cik=None, cusip=None, source_url=None):
    """Require at least one filter to avoid silently filling local DB with entire universe.

    Original accessions, quarter dates and amendments are retained; never infer a ticker from CUSIP.
    """
    if not manager_cik and not cusip:
        raise ValueError('Use --manager-cik or --cusip to limit huge SEC 13F archive')
    cik_filter=str(manager_cik).lstrip('0') if manager_cik else None
    cusip_filter=str(cusip).strip().upper() if cusip else None
    path=Path(zip_path)
    if not zipfile.is_zipfile(path): raise ValueError('Not a valid SEC ZIP archive')
    origin=source_url or path.resolve().as_uri()
    run=start_run(conn,'SEC13F',origin,_hash_file(path))
    seen=written=0
    try:
        with zipfile.ZipFile(path) as z:
            submissions={}
            for r in _tsv_reader(z,'SUBMISSION.tsv'):
                acc=r['ACCESSION_NUMBER']
                cik=str(r.get('CIK','')).lstrip('0')
                if cik_filter and cik != cik_filter: continue
                if r.get('SUBMISSIONTYPE','').upper() not in ('13F-HR','13F-HR/A'): continue
                submissions[acc]=r
            cover={}
            for r in _tsv_reader(z,'COVERPAGE.tsv'):
                if r.get('ACCESSION_NUMBER') in submissions:
                    cover[r['ACCESSION_NUMBER']]=r
            with conn:
                for r in _tsv_reader(z,'INFOTABLE.tsv'):
                    acc=r['ACCESSION_NUMBER']
                    sub=submissions.get(acc)
                    if sub is None or (cusip_filter and r.get('CUSIP','').upper()!=cusip_filter):
                        continue
                    seen+=1
                    report_date=day(sub['PERIODOFREPORT'])
                    filed=day(sub['FILING_DATE'])
                    cov=cover.get(acc,{})
                    filing_type=sub['SUBMISSIONTYPE']
                    # Filing availability conservatively next UTC date (not the report period).
                    insert_row(conn,'sec13f_filings',dict(accession=acc,manager_cik=str(sub['CIK']).lstrip('0'),
                        manager_name=cov.get('FILINGMANAGER_NAME'),report_period=report_date,
                        filing_date=filed,filing_type=filing_type,
                        amendment_type=cov.get('AMENDMENTTYPE') or None,
                        is_amendment=1 if filing_type.endswith('/A') else 0,
                        source_code='SEC13F',source_url=origin,
                        available_at=published_from_sec_date(filed),ingested_at=utcnow(),ingest_run_id=run),
                        ['accession'])
                    insert_row(conn,'sec13f_holdings',dict(accession=acc,info_sk=str(r['INFOTABLE_SK']),
                        cusip=r['CUSIP'].strip().upper(),issuer=r.get('NAMEOFISSUER'),
                        security_class=r.get('TITLEOFCLASS'),shares=number(r.get('SSHPRNAMT')),
                        amount_type=r.get('SSHPRNAMTTYPE'),
                        reported_value_raw=number(r.get('VALUE')),
                        reported_value_usd=(number(r.get('VALUE')) * (1000 if filed < '2023-01-03' else 1)) if number(r.get('VALUE')) is not None else None,
                        value_unit='USD_THOUSANDS' if filed < '2023-01-03' else 'USD',
                        put_call=(r.get('PUTCALL') or None),
                        investment_discretion=r.get('INVESTMENTDISCRETION'),
                        other_manager=r.get('OTHERMANAGER')),['accession','info_sk'])
                    written+=1
        finish_run(conn,run,'ok',seen,written)
    except Exception as e:
        conn.rollback();record_issue(conn,'SEC13F','ARCHIVE_PARSE_ERROR',str(e),run)
        finish_run(conn,run,'failed',seen,0,str(e));raise
    return {'run_id':run,'rows_seen':seen,'holdings_written':written,'source':origin}


def _tff_key(row, *keys):
    for key in keys:
        if key in row: return row[key]
    return None


def import_cftc_tff_zip(conn, zip_path, *, source_url=None, contract_contains=None, published_at=None):
    """Annual CFTC TFF futures-only files. Historical release dates are NOT guessed.

    If source lacks per-observation actual publication dates, default to ingest time;
    for historical PIT backtests supply independently verified --published-at.
    """
    path=Path(zip_path)
    if not zipfile.is_zipfile(path): raise ValueError('Not ZIP')
    origin=source_url or path.resolve().as_uri()
    run=start_run(conn,'CFTC_TFF',origin,_hash_file(path))
    seen=written=0
    try:
        with zipfile.ZipFile(path) as z:
            candidates=[x for x in z.infolist() if x.filename.lower().endswith(('.csv','.txt')) and not x.is_dir()]
            if not candidates: raise ValueError('No TFF csv/txt member found')
            pick=max(candidates,key=lambda x:x.file_size)
            with z.open(pick) as binary,io.TextIOWrapper(binary,encoding='utf-8-sig',newline='') as fp:
                reader=csv.DictReader(fp)
                if not reader.fieldnames or 'Asset_Mgr_Positions_Long_All' not in reader.fieldnames:
                    raise ValueError('Not a CFTC TFF futures-only CSV; field mapping unknown')
                with conn:
                    for r in reader:
                        seen+=1
                        market=str(_tff_key(r,'Market_and_Exchange_Names','CONTRACT_MARKET_NAME') or '').strip()
                        if contract_contains and contract_contains.upper() not in market.upper(): continue
                        code=str(_tff_key(r,'CFTC_Contract_Market_Code','cftc_contract_market_code') or '').strip()
                        if not market or not code: raise ValueError('missing CFTC contract name/code')
                        d=_tff_key(r,'Report_Date_as_YYYY-MM-DD','Report_Date_as_YYYY_MM_DD','report_date_as_yyyy_mm_dd')
                        if not d:
                            d=_tff_key(r,'Report_Date_as_MM_DD_YYYY')
                            if not d: raise ValueError('unknown CFTC date column')
                            from datetime import datetime
                            d=datetime.strptime(d,'%m/%d/%Y').date().isoformat()
                        d=day(str(d)[:10])
                        insert_row(conn,'cftc_tff',dict(report_date=d,contract_code=code,market_name=market,
                            open_interest=number(r.get('Open_Interest_All')),
                            asset_mgr_long=number(r.get('Asset_Mgr_Positions_Long_All')),
                            asset_mgr_short=number(r.get('Asset_Mgr_Positions_Short_All')),
                            leveraged_long=number(r.get('Lev_Money_Positions_Long_All')),
                            leveraged_short=number(r.get('Lev_Money_Positions_Short_All')),
                            source_code='CFTC_TFF',source_url=origin,
                            available_at=parse_available(published_at),ingested_at=utcnow(),ingest_run_id=run),
                            ['report_date','contract_code','source_code'])
                        written+=1
        finish_run(conn,run,'ok',seen,written)
    except Exception as e:
        conn.rollback();record_issue(conn,'CFTC_TFF','ARCHIVE_PARSE_ERROR',str(e),run)
        finish_run(conn,run,'failed',seen,0,str(e));raise
    return {'rows_seen':seen,'rows_written':written,'run_id':run,'source':origin}


def fetch_cftc_tff(conn,year,*,contract_contains=None,published_at=None):
    if not re.fullmatch(r'20\d{2}',str(year)):
        raise ValueError('Use 20xx year')
    url=f'https://www.cftc.gov/files/dea/history/fut_fin_txt_{year}.zip'
    from tempfile import NamedTemporaryFile
    data=fetch_bytes(url,{'www.cftc.gov'},max_bytes=50_000_000)
    with NamedTemporaryFile(suffix='.zip') as f:
        f.write(data);f.flush()
        return import_cftc_tff_zip(conn,f.name,source_url=url,
            contract_contains=contract_contains,published_at=published_at)

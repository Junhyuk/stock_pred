"""Operational CLI, stdlib-only. Example: python -m smartmoney status."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
from .db import initialize,connect,DEFAULT_DB
from .importers import import_file
from .collectors import fetch_krx_prices,fetch_dart_major,fetch_finra_short_volume,fetch_to_file
from .archives import import_sec13f_zip,import_cftc_tff_zip,fetch_cftc_tff
from .webparse import public_html_table,import_web_table
from .signals import analyze


def parser():
    p=argparse.ArgumentParser(prog='smartmoney',description='Auditable KR/US public money-flow research; not brokerage trading')
    p.add_argument('--db',default=str(DEFAULT_DB),help='SQLite path (default: data/smartmoney.sqlite)')
    sub=p.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db');sub.add_parser('status');sub.add_parser('doctor')
    imp=sub.add_parser('import-csv');imp.add_argument('kind',choices=['price','kr-flow','kr-ownership','short-interest','us-short-volume','cusip-map']);imp.add_argument('path')
    imp.add_argument('--market',choices=['KR','US'],default='KR');imp.add_argument('--investor',choices=['FOREIGN','INSTITUTION','RETAIL','OTHER'])
    imp.add_argument('--published-at',help='Verified ISO8601+timezone; omit to use actual import time (not safe for historical backtests)')
    imp.add_argument('--mapping',help='JSON file mapping internal canonical names to CSV column headings')
    krx=sub.add_parser('fetch-krx');krx.add_argument('--date',required=True);krx.add_argument('--exchange',choices=['kospi','kosdaq'],required=True)
    dart=sub.add_parser('fetch-dart');dart.add_argument('--corp-code',required=True);dart.add_argument('--symbol',required=True)
    fin=sub.add_parser('fetch-finra');fin.add_argument('--date',required=True);fin.add_argument('--symbols',help='Comma separated, empty = all entries')
    sec=sub.add_parser('import-sec13f');sec.add_argument('zipfile');sec.add_argument('--manager-cik');sec.add_argument('--cusip');sec.add_argument('--source-url')
    dl=sub.add_parser('download-sec13f');dl.add_argument('url',help='Copy the ZIP link from official SEC Form 13F data sets page')
    dl.add_argument('--out',required=True)
    cftc=sub.add_parser('import-cftc');cftc.add_argument('zipfile');cftc.add_argument('--contract-contains');cftc.add_argument('--published-at')
    cftc.add_argument('--source-url')
    cftc_fetch=sub.add_parser('fetch-cftc');cftc_fetch.add_argument('--year',required=True,type=int)
    cftc_fetch.add_argument('--contract-contains');cftc_fetch.add_argument('--published-at')
    web=sub.add_parser('web-table');web.add_argument('url');web.add_argument('--allow-host',required=True)
    web.add_argument('--confirm-rights',action='store_true',help='Affirm site TOS allows automated download and use')
    web.add_argument('--table-index',type=int,default=0);web.add_argument('--out',required=True,help='CSV output; no DB import without explicit choice')
    wb=sub.add_parser('import-web-table');wb.add_argument('url');wb.add_argument('--allow-host',required=True)
    wb.add_argument('--confirm-rights',action='store_true');wb.add_argument('--table-index',type=int,default=0)
    wb.add_argument('--kind',choices=['price','kr-flow','kr-ownership','short-interest','us-short-volume'],required=True)
    wb.add_argument('--mapping',required=True);wb.add_argument('--market',default='KR',choices=['KR','US']);wb.add_argument('--investor');wb.add_argument('--published-at')
    sig=sub.add_parser('analyze');sig.add_argument('--market',choices=['KR','US'],required=True);sig.add_argument('--symbol',required=True)
    sig.add_argument('--as-of',help='PIT publication cutoff; YYYY-MM-DD or offset ISO 8601');sig.add_argument('--no-save',action='store_true')
    return p


def status(c):
    tables=['instruments','watchlist','prices','kr_investor_flows','kr_foreign_ownership','dart_major_holdings',
            'sec13f_filings','sec13f_holdings','cusip_symbol_map','us_short_volume','short_interest','cftc_tff','signal_snapshots','ingest_runs','quality_issues']
    return {'tables':{table:c.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in tables},
            'last_runs':[dict(r) for r in c.execute('SELECT id,source_code,finished_at,status,rows_written,message FROM ingest_runs ORDER BY id DESC LIMIT 5')]}


def main(argv=None):
    args=parser().parse_args(argv)
    try:
        if args.command=='init-db':
            initialize(args.db)
            print(json.dumps({'created_or_checked':str(args.db),'price_rows_seeded':0},ensure_ascii=False,indent=2))
            return 0
        if args.command=='doctor':
            out={'python':sys.version.split()[0], 'db_exists':Path(args.db).is_file(),
                 'api_keys_configured':{key:bool(os.environ.get(key)) for key in ['KRX_AUTH_KEY','DART_API_KEY','SEC_USER_AGENT']},
                 'notes':['API secrets are never written to the database','SEC requires identifiable User-Agent before download',
                          'No automatic scraping of browser-private endpoints']}
            print(json.dumps(out,ensure_ascii=False,indent=2));return 0
        if args.command=='download-sec13f':
            if not args.url.startswith('https://www.sec.gov/files/') or not args.url.lower().endswith('.zip'):
                raise ValueError('Use an exact https://www.sec.gov/files/*.zip URL copied from SEC data page')
            agent=os.environ.get('SEC_USER_AGENT')
            if not agent or '@' not in agent: raise ValueError('Set SEC_USER_AGENT with an identifiable name and contact email')
            ret={'downloaded':fetch_to_file(args.url,args.out,{'www.sec.gov'},user_agent=agent)}
        elif args.command=='web-table':
            output,meta=public_html_table(args.url,args.allow_host,args.table_index,confirm_rights=args.confirm_rights)
            out=Path(args.out)
            if out.exists():raise FileExistsError('Refusing to overwrite existing CSV file')
            out.parent.mkdir(parents=True,exist_ok=True);out.write_text(output,encoding='utf-8-sig')
            ret={**meta,'saved_csv':str(out)}
        else:
            c=connect(args.db)
            try:
                if args.command=='status':ret=status(c)
                elif args.command=='import-csv':
                    mp=json.loads(Path(args.mapping).read_text(encoding='utf-8')) if args.mapping else None
                    ret=import_file(c,args.kind,args.path,market=args.market,investor=args.investor,
                                    mapping=mp,published_at=args.published_at)
                elif args.command=='fetch-krx':ret=fetch_krx_prices(c,args.date,args.exchange,os.environ.get('KRX_AUTH_KEY'))
                elif args.command=='fetch-dart':ret=fetch_dart_major(c,args.corp_code,args.symbol,os.environ.get('DART_API_KEY'))
                elif args.command=='fetch-finra':ret=fetch_finra_short_volume(c,args.date,args.symbols.split(',') if args.symbols else None)
                elif args.command=='import-sec13f':ret=import_sec13f_zip(c,args.zipfile,manager_cik=args.manager_cik,cusip=args.cusip,source_url=args.source_url)
                elif args.command=='import-cftc':ret=import_cftc_tff_zip(c,args.zipfile,source_url=args.source_url,
                                contract_contains=args.contract_contains,published_at=args.published_at)
                elif args.command=='fetch-cftc':ret=fetch_cftc_tff(c,args.year,contract_contains=args.contract_contains,published_at=args.published_at)
                elif args.command=='import-web-table':
                    mp=json.loads(Path(args.mapping).read_text(encoding='utf-8'))
                    ret=import_web_table(c,args.url,args.allow_host,args.kind,mapping=mp,market=args.market,investor=args.investor,
                             published_at=args.published_at,table_index=args.table_index,confirm_rights=args.confirm_rights)
                elif args.command=='analyze':ret=analyze(c,args.market,args.symbol,as_of=args.as_of,save=not args.no_save)
                else:raise ValueError(f'unsupported command: {args.command}')
            finally:c.close()
        print(json.dumps(ret,ensure_ascii=False,indent=2,default=str))
        return 0
    except (FileNotFoundError,PermissionError,ValueError,KeyError,OSError,RuntimeError, __import__('sqlite3').Error) as e:
        # Errors from authenticated URLs may contain sensitive query values; don't print raw message for network failures.
        typ=type(e).__name__
        msg='Remote API failure; check endpoint/authorization without exposing credentials' if ('crtfc_key=' in str(e) or 'AUTH_KEY' in str(e) and 'missing' not in str(e)) else str(e)
        print(json.dumps({'error_type':typ,'error':msg},ensure_ascii=False),file=sys.stderr)
        return 2

if __name__=='__main__':raise SystemExit(main())

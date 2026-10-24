"""Offline synthetic fixtures only. No test fixture is real market history."""
import csv
import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import date,timedelta
from pathlib import Path
from smartmoney.db import connect,initialize,utcnow
from smartmoney.importers import import_text
from smartmoney.archives import import_sec13f_zip,import_cftc_tff_zip
from smartmoney.signals import analyze
from smartmoney.webparse import TableParser

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/'trial.sqlite'
        initialize(self.db)
        self.c=connect(self.db)
    def tearDown(self):
        self.c.close();self.tmp.cleanup()
    def count(self,t):
        return self.c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]
    def test_seed_contains_no_real_market_data(self):
        for table in ['prices','kr_investor_flows','kr_foreign_ownership','sec13f_holdings','us_short_volume','cftc_tff']:
            self.assertEqual(self.count(table),0)
        self.assertGreater(self.count('sources'),0)
        self.assertEqual(analyze(self.c,'KR','005930',save=False)['label'],'불확실')
    def test_flow_import_is_idempotent_and_requires_usable_units(self):
        t='symbol,trade_date,investor_type,buy_qty,sell_qty,net_qty\n005930,2026-10-21,FOREIGN,100,75,25\n'
        import_text(self.c,'kr-flow',t)
        import_text(self.c,'kr-flow',t)
        self.assertEqual(self.count('kr_investor_flows'),1)
        r=self.c.execute('SELECT net_qty FROM kr_investor_flows').fetchone()
        self.assertEqual(r['net_qty'],25)
        with self.assertRaisesRegex(ValueError,'net_qty'):
            import_text(self.c,'kr-flow',t.replace(',25\n',',30\n'))
        self.assertEqual(self.count('kr_investor_flows'),1)
        self.assertEqual(self.count('quality_issues'),1)
    def test_invalid_date_rejected(self):
        with self.assertRaises(ValueError):
            import_text(self.c,'price','symbol,trade_date,close\n005930,2026-14-70,123\n')
        self.assertEqual(self.count('prices'),0)
    def test_pit_cutoff_excludes_late_import(self):
        import_text(self.c,'price','symbol,trade_date,close\n005930,2022-01-01,100\n')
        r=analyze(self.c,'KR','005930',as_of='2022-01-02',save=False)
        self.assertIsNone(r['as_of'])
    def test_kr_20_day_hypothesis(self):
        day0=date(2026,9,1)
        dates=[(day0+timedelta(days=i)).isoformat() for i in range(21)]
        p='symbol,trade_date,close\n'+''.join(f'005930,{d},{100+i}\n' for i,d in enumerate(dates))
        f='symbol,trade_date,investor_type,net_qty\n'+''.join(f'005930,{d},FOREIGN,10\n' for d in dates[-20:])
        o='symbol,trade_date,ownership_pct\n'+f'005930,{dates[0]},51\n005930,{dates[-1]},52\n'
        import_text(self.c,'price',p,source='MANUAL');import_text(self.c,'kr-flow',f);import_text(self.c,'kr-ownership',o)
        r=analyze(self.c,'KR','005930',save=False)
        self.assertEqual(r['label'],'매집 정황')
        self.assertEqual(r['metrics']['foreign_net_shares_20d'],200)
        self.assertEqual(r['metrics']['ownership_change_pp'],1)
        self.assertEqual(r['confidence'],'MEDIUM')
    def _sec_zip(self,old_date='2022-12-31',new_date='2026-08-01'):
        z=Path(self.tmp.name)/'sample_SEC_synthetic.zip'
        def tsv(fields,records):
            f=io.StringIO();w=csv.DictWriter(f,fieldnames=fields,delimiter='\t');w.writeheader();w.writerows(records);return f.getvalue()
        submissions=[];cover=[];hold=[]
        for i in range(3):
            for j in (0,1):
                acc=f'000000{i}-26-{j:06}'
                submissions.append({'ACCESSION_NUMBER':acc,'CIK':str(100+i),'SUBMISSIONTYPE':'13F-HR','PERIODOFREPORT':'2026-03-31' if j==0 else '2026-06-30', 'FILING_DATE':'2026-05-01' if j==0 else '2026-08-01'})
                cover.append({'ACCESSION_NUMBER':acc,'FILINGMANAGER_NAME':f'SYNTHETIC_FUND_{i}'})
                hold.append({'ACCESSION_NUMBER':acc,'INFOTABLE_SK':str(i*100+j),'CUSIP':'123456789','NAMEOFISSUER':'SYNTHETIC','TITLEOFCLASS':'COM', 'SSHPRNAMT':str(100+j*10),'SSHPRNAMTTYPE':'SH','VALUE':str(10000+j*1000),'PUTCALL':'','INVESTMENTDISCRETION':'SOLE','OTHERMANAGER':''})
        with zipfile.ZipFile(z,'w') as f:
            f.writestr('SUBMISSION.tsv',tsv(list(submissions[0]),submissions))
            f.writestr('COVERPAGE.tsv',tsv(list(cover[0]),cover))
            f.writestr('INFOTABLE.tsv',tsv(list(hold[0]),hold))
        return z
    def test_sec_zip_parser_usd_post_2023_and_signal(self):
        z=self._sec_zip()
        got=import_sec13f_zip(self.c,z,cusip='123456789')
        self.assertEqual(got['holdings_written'],6)
        self.assertEqual(self.count('sec13f_filings'),6)
        rec=self.c.execute('SELECT * FROM sec13f_holdings LIMIT 1').fetchone()
        self.assertEqual(rec['reported_value_usd'],10000)
        self.assertEqual(rec['value_unit'],'USD')
        import_text(self.c,'cusip-map','cusip,symbol,valid_from,mapping_source\n123456789,AAPL,2020-01-01,SYNTHETIC_VERIFICATION\n')
        r=analyze(self.c,'US','AAPL',save=False)
        self.assertEqual(r['label'],'기관 보유 확대 정황')
        self.assertEqual(r['metrics']['comparable_managers'],3)
    def test_sec_old_thousand_usd(self):
        z=Path(self.tmp.name)/'old.zip'
        def tsv(fields,records):
            s=io.StringIO();w=csv.DictWriter(s,fieldnames=fields,delimiter='\t');w.writeheader();w.writerows(records);return s.getvalue()
        acc='test-old'
        with zipfile.ZipFile(z,'w') as f:
            f.writestr('SUBMISSION.tsv',tsv(['ACCESSION_NUMBER','CIK','SUBMISSIONTYPE','PERIODOFREPORT','FILING_DATE'],
                [dict(ACCESSION_NUMBER=acc,CIK='123',SUBMISSIONTYPE='13F-HR',PERIODOFREPORT='2022-09-30',FILING_DATE='2022-11-01')]))
            f.writestr('COVERPAGE.tsv',tsv(['ACCESSION_NUMBER','FILINGMANAGER_NAME'],
                [dict(ACCESSION_NUMBER=acc,FILINGMANAGER_NAME='SYNTHETIC')]))
            f.writestr('INFOTABLE.tsv',tsv(['ACCESSION_NUMBER','INFOTABLE_SK','CUSIP','SSHPRNAMT','SSHPRNAMTTYPE','VALUE'],
                [dict(ACCESSION_NUMBER=acc,INFOTABLE_SK='1',CUSIP='123456789',SSHPRNAMT='5',SSHPRNAMTTYPE='SH',VALUE='15')]))
        import_sec13f_zip(self.c,z,manager_cik='123')
        r=self.c.execute('SELECT reported_value_usd,value_unit FROM sec13f_holdings').fetchone()
        self.assertEqual(r['reported_value_usd'],15000)
        self.assertEqual(r['value_unit'],'USD_THOUSANDS')
    def test_cftc_tff_zip_parser(self):
        z=Path(self.tmp.name)/'synthetic_tff.zip'
        d='Market_and_Exchange_Names,Report_Date_as_MM_DD_YYYY,CFTC_Contract_Market_Code,Open_Interest_All,Asset_Mgr_Positions_Long_All,Asset_Mgr_Positions_Short_All,Lev_Money_Positions_Long_All,Lev_Money_Positions_Short_All\nSYNTHETIC STOCK INDEX,10/06/2026,888888,1000,400,100,200,250\n'
        with zipfile.ZipFile(z,'w') as f:f.writestr('sample.csv',d)
        out=import_cftc_tff_zip(self.c,z)
        self.assertEqual(out['rows_written'],1)
        self.assertEqual(self.c.execute('SELECT asset_mgr_long FROM cftc_tff').fetchone()[0],400)
    def test_html_parser_static(self):
        t=TableParser();t.feed('<table><tr><th>Symbol</th><th>Net</th></tr><tr><td>AAPL</td><td>300</td></tr></table>')
        self.assertEqual(t.tables[0][1],['AAPL','300'])
        with self.assertRaises(ValueError):
            t2=TableParser();t2.feed('<table><tr><th colspan="2">bad</th></tr></table>')

if __name__=='__main__':unittest.main()

class ShortSaleTests(unittest.TestCase):
    def test_finra_listing_market_not_mistaken_for_reporting_facility(self):
        from smartmoney.importers import import_text
        from smartmoney.db import initialize,connect
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'finra.sqlite';initialize(p);c=connect(p)
            try:
                sample='Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20261021|NVDA|100|2|320|Q\n'
                import_text(c,'us-short-volume',sample,source='FINRA_REGSHO',
                            source_url='https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261021.txt')
                r=c.execute('SELECT reporting_facility,listing_market,short_volume,total_volume FROM us_short_volume').fetchone()
                self.assertEqual(r['reporting_facility'],'CNMS');self.assertEqual(r['listing_market'],'Q')
                self.assertEqual(r['short_volume'],100);self.assertEqual(r['total_volume'],320)
            finally:c.close()

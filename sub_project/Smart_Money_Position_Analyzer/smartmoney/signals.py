"""Explainable position evidence; no ML accuracy/probability promises.

All queries are point-in-time: observed trade dates and publication timestamps must
precede the requested cutoff. Precedence is deterministic when multiple sources exist.
"""
from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from .db import utcnow
from .normalize import parse_available


def _cutoff(v):
    if v is None: return utcnow()
    s = str(v).strip()
    if len(s)==10 and s[4]=='-' and s[7]=='-':
        return (datetime.fromisoformat(s).replace(tzinfo=timezone.utc) + timedelta(days=1) - timedelta(microseconds=1)).isoformat()
    return parse_available(s)


def _pick(conn, table, market, symbol, cutoff, date_field, source_preference):
    where='market=? AND ' if market else ''
    args=([market] if market else []) + [symbol,cutoff,cutoff[:10]]
    rows=conn.execute(f'''SELECT * FROM {table} WHERE {where}symbol=?
                   AND available_at<=? AND {date_field}<=? ORDER BY {date_field} DESC,available_at DESC''',args).fetchall()
    out={}
    for r in rows:
        d=r[date_field]
        if d not in out or source_preference.get(r['source_code'],0) > source_preference.get(out[d]['source_code'],0):
            out[d]=dict(r)
    return sorted(out.values(),key=lambda r:r[date_field])


def _kr_evidence(conn, symbol, cutoff):
    price=_pick(conn,'prices','KR',symbol,cutoff,'trade_date',{'KRX_API':3,'KRX_CSV':2,'MANUAL':1,'WEB_TABLE':0})
    price=price[-61:]
    latest=price[-1]['trade_date'] if price else None
    flows=conn.execute('''SELECT * FROM kr_investor_flows WHERE symbol=? AND investor_type='FOREIGN'
         AND available_at<=? AND trade_date<=? ORDER BY trade_date DESC,available_at DESC''',(symbol,cutoff,cutoff[:10])).fetchall()
    flowmap={}
    for r in flows:
        d=r['trade_date']
        if d not in flowmap or (r['source_code']=='KRX_CSV' and flowmap[d]['source_code']!='KRX_CSV'):
            flowmap[d]=dict(r)
    own=_pick(conn,'kr_foreign_ownership',None,symbol,cutoff,'trade_date',{'KRX_CSV':2,'WEB_TABLE':1})
    valid_dates=[p['trade_date'] for p in price[-20:]]
    fv=[flowmap[d]['net_qty'] for d in valid_dates if d in flowmap and flowmap[d]['net_qty'] is not None]
    covered=len(fv)
    pos=sum(1 for v in fv if v>0)
    net=sum(fv) if fv else None
    metrics={'price_sessions':len(price),'latest_price_date':latest,'foreign_flow_sessions_20d':covered,
             'foreign_net_shares_20d':net,'foreign_buy_days_20d':pos if fv else None,
             'foreign_positive_day_ratio':pos/covered if covered else None,
             'latest_foreign_ownership_pct':own[-1]['ownership_pct'] if own else None,
             'ownership_change_pp':None,'price_return_20d':None}
    # Require 20 intervals (21 prices) for 20d returns and 2 adequately separated ownership snapshots.
    if len(price)>=21 and price[-21]['close'] and price[-1]['close'] is not None:
        metrics['price_return_20d']=(price[-1]['close']/price[-21]['close']-1)*100
    if len(own)>=2 and len(price)>=21:
        cutoff_base=price[-21]['trade_date']
        old=[r for r in own if r['trade_date']<=cutoff_base]
        if old:
            metrics['ownership_change_pp']=own[-1]['ownership_pct']-old[-1]['ownership_pct']
    evidence=[]; contradictions=[]; missing=[]
    if len(price)<21: missing.append('가격 21거래일(20일 수익률) 자료 부족')
    if covered<16: missing.append('20거래일 중 외국인 순매수 수량 자료 16일 미만')
    if net is None: missing.append('외국인 순매수 수량 미확보(금액만으로 주수 추정 금지)')
    if metrics['ownership_change_pp'] is None: missing.append('20거래일 기준 외국인 보유비율 비교자료 부족')
    label='불확실';confidence='LOW'
    ow=metrics['ownership_change_pp']
    if covered>=16 and ow is not None and net is not None:
        if net>0 and pos/covered>=.60 and ow>0:
            label='매집 정황';confidence='MEDIUM'
            evidence=['20일 구간 외국인 순매수 수량 양수','순매수 거래일 비중 60% 이상','외국인 보유비율 상승']
        elif net<0 and pos/covered<=.40 and ow<0:
            label='매도/이탈 정황';confidence='MEDIUM'
            evidence=['20일 구간 외국인 순매수 수량 음수','순매수 거래일 비중 40% 이하','외국인 보유비율 하락']
        else:
            evidence=['수급·보유율 방향의 합치 불충분']
            contradictions.append('외국인 수급과 보유율 변화가 일관되지 않음')
    if label=='매집 정황' and metrics['price_return_20d'] is not None and metrics['price_return_20d']<0:
        contradictions.append('20거래일 주가가 하락하여 추가 검증 필요')
    if label=='매도/이탈 정황' and metrics['price_return_20d'] is not None and metrics['price_return_20d']>0:
        contradictions.append('20거래일 주가가 상승하여 추가 검증 필요')
    if own and latest and own[-1]['trade_date']<latest:
        missing.append('최신 외국인 보유비율 기준일이 마지막 가격 기준일보다 과거')
    return dict(market='KR',symbol=symbol,as_of=latest,label=label,confidence=confidence,
                metrics=metrics,evidence=evidence,contradictions=contradictions,missing=missing,
                limitations=['공개 집계는 투자자 개별 계좌·선물 헤지·실제 평균 매입단가를 알 수 없음'])


def _us_evidence(conn,symbol,cutoff):
    mappings=conn.execute('''SELECT * FROM cusip_symbol_map WHERE symbol=?
          AND verified_at<=? ORDER BY verified_at DESC''',(symbol,cutoff)).fetchall()
    cusips={r['cusip'] for r in mappings if r['valid_from']<=cutoff[:10] and (not r['valid_to'] or r['valid_to']>=cutoff[:10])}
    metrics={'mapped_cusips':sorted(cusips),'comparable_managers':0,'managers_increased':0,
             'managers_decreased':0,'managers_unchanged':0,'latest_report_period':None,
             'last_filing_date':None,'amendment_detected':False}
    missing=[]; evidence=[]; contradictions=[]
    if not cusips: missing.append('검증된 CUSIP↔티커 매핑 없음: SEC 기관보유와 연결 불가')
    if not cusips:
        return dict(market='US',symbol=symbol,as_of=None,label='불확실',confidence='LOW',
                    metrics=metrics,evidence=evidence,contradictions=contradictions,missing=missing,
                    limitations=['13F에는 공매도 및 전체 파생상품 순노출이 포함되지 않음'])
    placeholders=','.join('?'*len(cusips))
    rows=conn.execute(f'''SELECT f.manager_cik,f.report_period,f.filing_date,f.accession,
        f.is_amendment,h.cusip,h.shares,h.amount_type,h.put_call
        FROM sec13f_filings f JOIN sec13f_holdings h ON h.accession=f.accession
        WHERE h.cusip IN ({placeholders}) AND f.available_at<=? AND f.report_period<=?
        ORDER BY f.manager_cik,f.report_period,f.filing_date DESC,f.accession DESC''',
        (*sorted(cusips),cutoff,cutoff[:10])).fetchall()
    by_manager={}
    for r in rows:
        if (r['amount_type'] or '').upper() not in ('SH','SHS','SHARES','SHRS'): continue
        if r['put_call']: continue
        manager=by_manager.setdefault(r['manager_cik'],{})
        period=manager.setdefault(r['report_period'],{})
        # Original and amendments MUST NOT be silently combined as a complete filing.
        key=r['cusip']; old=period.get(key)
        if old is None or (r['filing_date'],r['accession'])>(old['filing_date'],old['accession']):
            period[key]=dict(r)
    increased=decreased=unchanged=0; comparisons=0
    latest_periods=[];last_dates=[];amended=False
    for manager,periods in by_manager.items():
        pts=sorted(periods)
        if len(pts)<2: continue
        prev,latest=pts[-2:]
        if latest[:7]==prev[:7]: continue
        # Aggregate across CUSIP classes only when user explicitly mapped them; avoids ticker guesses.
        left=periods[prev];right=periods[latest]
        all_rows=list(left.values())+list(right.values())
        if any(x['is_amendment'] for x in all_rows):
            amended=True;continue
        if any(x['shares'] is None for x in all_rows):continue
        a=sum(x['shares'] for x in left.values());b=sum(x['shares'] for x in right.values())
        comparisons+=1;latest_periods.append(latest);last_dates.extend(x['filing_date'] for x in right.values())
        if b>a:increased+=1
        elif b<a:decreased+=1
        else:unchanged+=1
    metrics.update(comparable_managers=comparisons,managers_increased=increased,
                   managers_decreased=decreased,managers_unchanged=unchanged,
                   latest_report_period=max(latest_periods) if latest_periods else None,
                   last_filing_date=max(last_dates) if last_dates else None,amendment_detected=amended)
    if comparisons<3: missing.append('비교 가능한 13F 기관이 3곳 미만: 방향성 판단 보류')
    if amended:missing.append('13F 정정공시 혼재: 원문 및 정정 범위 검토 전 해당 기관 집계 제외')
    label='불확실';confidence='LOW'
    if comparisons>=3:
        if increased/comparisons>=.65:
            label='기관 보유 확대 정황';confidence='MEDIUM';evidence.append('분기말 보유주수 증가 기관 비중 65% 이상')
        elif decreased/comparisons>=.65:
            label='기관 보유 축소 정황';confidence='MEDIUM';evidence.append('분기말 보유주수 감소 기관 비중 65% 이상')
        else: contradictions.append('13F 기관별 보유 방향이 엇갈림')
    return dict(market='US',symbol=symbol,as_of=metrics['latest_report_period'],label=label,
                confidence=confidence,metrics=metrics,evidence=evidence,contradictions=contradictions,missing=missing,
                limitations=['분기말 13F 보유량은 공시 후 공개되어 최신 거래내역이나 숏·헤지를 보여주지 않음',
                             '비교에 주식분할 조정 미적용: 기간 중 기업행동 확인 전 거래량으로 해석 금지',
                             '동일 보고기관의 정정·부분 공시는 별도 검토 필요'])


def analyze(conn,market,symbol,*,as_of=None,save=True):
    market=market.upper();symbol=symbol.upper().strip() if market=='US' else symbol.strip().zfill(6)
    if market not in ('KR','US'):raise ValueError('Use KR or US')
    cutoff=_cutoff(as_of)
    res=(_kr_evidence(conn,symbol,cutoff) if market=='KR' else _us_evidence(conn,symbol,cutoff))
    res['data_cutoff']=cutoff
    res['notice']='간접적인 공개 데이터 분석이며 특정 큰손의 계좌 포지션·매입단가나 미래 수익률 예측이 아닙니다.'
    if save:
        with conn:
            conn.execute('''INSERT INTO signal_snapshots
            (market,symbol,evaluated_at,data_cutoff,label,confidence,metrics_json,evidence_json,contradictions_json,missing_json)
            VALUES(?,?,?,?,?,?,?,?,?,?)''',(
                market,symbol,utcnow(),cutoff,res['label'],res['confidence'],
                json.dumps(res['metrics'],ensure_ascii=False),json.dumps(res['evidence'],ensure_ascii=False),
                json.dumps(res['contradictions'],ensure_ascii=False),json.dumps(res['missing'],ensure_ascii=False)))
    return res

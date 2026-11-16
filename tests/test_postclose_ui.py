import json
import subprocess
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / 'docs/reliable-index-forecast.html'


def run_ui(expression):
    html = HTML.read_text()
    script = html.split('<script>', 1)[1].split('</script>', 1)[0]
    definitions = script.split("fetch('data/automation_status.json'", 1)[0]
    program = definitions + '\n' + expression
    return subprocess.run(['node', '-e', program], capture_output=True, text=True, check=True).stdout


def test_not_ready_renders_calibrated_reference_direction_and_distinct_close_dates():
    item = {'market': 'KOSPI', 'context': {'asof_date': '2026-11-16', 'target_date': '2026-11-17',
             'issued_at': '2026-11-16T09:01:00Z'},
            'forecast': {'status': 'not_ready', 'latest_close': 5100.50},
            'incumbent_reference': {'calibration_ready': True, 'status': 'rejected',
              'production_weight': 0, 'expected_index': 5120.0, 'expected_return': 0.0038,
              'p_up': 0.63, 'p_down': 0.37, 'asof_date': '2026-11-16', 'target_date': '2026-11-17'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));" )
    for text in ('63.00%', '37.00%', '5,120', '2026-11-16', '2026-11-17', '회고', '검증 미통과', 'production_weight=0', '상승'):
        assert text in out
    assert 'undefined' not in out


def test_expired_target_and_equal_probabilities_are_not_presented_as_today_up():
    item = {'market': 'KOSPI', 'context': {'asof_date': '2020-01-02', 'target_date': '2020-01-03'},
            'forecast': {'status': 'not_ready'}, 'incumbent_reference': {'calibration_ready': True,
             'p_up': 0.5, 'p_down': 0.5, 'expected_return': 0, 'status': 'rejected'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));" )
    assert '이미 종료' in out
    assert '중립' in out
    assert 'undefined' not in out


def test_close_card_reports_source_received_session_and_conflict():
    item = {'session_date': '2026-11-16', 'close': 5100.5, 'status': 'single_source',
            'source': 'Naver index API', 'received_at': '2026-11-16T09:05:00Z'}
    out = run_ui('console.log(renderMarketClose("KOSPI", ' + json.dumps(item) + '));')
    for text in ('5,100.5', '2026-11-16', 'Naver', '수신', '단일'):
        assert text in out
    assert 'undefined' not in out


def test_target_expiry_uses_exchange_close_time_for_same_day_kr_and_us():
    result = run_ui('console.log(JSON.stringify([targetEnded({target_date:"2020-11-16",target_close_at:"2020-11-16T06:30:00Z"}, Date.parse("2020-11-16T09:00:00Z")),targetEnded({target_date:"2020-11-16",target_close_at:"2020-11-16T21:00:00Z"}, Date.parse("2020-11-16T09:00:00Z"))]));')
    assert json.loads(result) == [True, False]


def test_independent_close_receipt_rejects_tampered_payload():
    program = '''
const {webcrypto} = require('node:crypto'); globalThis.crypto = webcrypto;
const body = JSON.stringify({schema_version:'market_closes_v1',run_id:'a',markets:{}});
globalThis.fetch = async (url) => url.endsWith('market_closes.json') ? {ok:true,text:async()=>body} :
 {ok:true,json:async()=>({schema_version:'market_closes_publication_v1',run_id:'a',publication_checked_at:'x',payload_checksums:{'market_closes.json':'bad'}})};
loadMarketCloses().then(()=>console.log('accepted')).catch(()=>console.log('rejected'));
'''
    assert run_ui(program).strip() == 'rejected'


def test_calibrated_reference_appears_in_primary_probability_boxes_when_main_not_ready():
    item = {'market': 'KOSPI', 'context': {'asof_date': '2026-11-16', 'target_date': '2026-11-17'},
            'forecast': {'status': 'not_ready', 'latest_close': 5100.50},
            'incumbent_reference': {'calibration_ready': True, 'p_up': 0.63, 'p_down': 0.37,
                'expected_return': 0.0038, 'expected_index': 5120.0, 'status': 'rejected'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));" )
    hero = out.split('<div class="prob-row">', 1)[1].split('<div class="detail-line">', 1)[0]
    assert '63.00%' in hero and '37.00%' in hero
    assert '연구용' in hero


def test_loader_preserves_verified_carried_card_publication_time():
    program = '''
const {webcrypto, createHash} = require('node:crypto'); globalThis.crypto = webcrypto;
const body = JSON.stringify({run_id:'new',status:'unpublished',markets:[
 {market:'KOSPI',carried_forward:true,context:{issued_at:'2026-11-16T09:01:00Z'},forecast:{issued_at:'2026-11-16T09:01:00Z'}},
 {market:'KOSDAQ',context:{issued_at:null},forecast:{issued_at:null}}]});
const digest = createHash('sha256').update(body).digest('hex');
globalThis.fetch = async url => url.endsWith('reliable_index_forecast.json') ? {ok:true,text:async()=>body} :
 {ok:true,json:async()=>({schema_version:'reliable_publication_v1',run_id:'new',publication_checked_at:'2026-11-16T10:00:00Z',payload_checksums:{'reliable_index_forecast.json':digest}})};
loadPublishedRun().then(run => console.log(JSON.stringify(run.markets.map(m=>[m.context.issued_at,m.forecast.issued_at]))));
'''
    assert json.loads(run_ui(program)) == [['2026-11-16T09:01:00Z','2026-11-16T09:01:00Z'],
                                            ['2026-11-16T10:00:00Z','2026-11-16T10:00:00Z']]

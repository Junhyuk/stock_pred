import json
import re
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
    for text in ('63.00%', '37.00%', '5,120', '2026-11-16', '2026-11-17', '상승'):
        assert text in out
    assert '검증 미통과' not in out
    assert '품질 기준 미통과' not in out
    assert 'undefined' not in out


def test_expired_target_and_equal_probabilities_are_not_presented_as_today_up():
    item = {'market': 'KOSPI', 'context': {'asof_date': '2020-01-02', 'target_date': '2020-01-03'},
            'forecast': {'status': 'not_ready'}, 'incumbent_reference': {'calibration_ready': True,
             'p_up': 0.5, 'p_down': 0.5, 'expected_return': 0, 'status': 'rejected'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));" )
    assert '이미 종료' in out
    assert '검증 미통과' not in out
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
    assert '상승 확률' in hero and '하락 확률' in hero
    assert '연구용' not in hero


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


def test_kst_clock_and_relative_dates_ignore_browser_timezone():
    program = '''
const now = Date.parse('2026-11-15T15:30:00Z');
console.log(JSON.stringify([formatKstClock(now), relativeKstDate('2026-11-16', now), relativeKstDate('2026-11-17', now), relativeKstDate('2026-11-18', now)]));
'''
    clock, today, tomorrow, later = json.loads(run_ui(program))
    assert '2026-11-16' in clock and '00:30' in clock
    assert [today, tomorrow, later] == ['오늘', '내일', '']


def test_korean_relative_dates_and_weekend_do_not_mislabel_next_session():
    item = {'market': 'KOSPI', 'context': {'asof_date': '2026-11-16', 'target_date': '2026-11-17'}, 'forecast': {'status': 'not_ready'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only', Date.parse('2026-11-16T03:00:00Z')));")
    assert '오늘' in out and '내일' in out
    item['context'] = {'asof_date': '2026-11-13', 'target_date': '2026-11-16'}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only', Date.parse('2026-11-13T03:00:00Z')));")
    assert '오늘' in out and '내일' not in out


def test_us_card_preserves_exchange_session_dates_and_kst_open_close():
    item = {'market': 'US_SPX', 'context': {'asof_date': '2026-11-13', 'target_date': '2026-11-16', 'issuance_deadline': '2026-11-16T14:30:00Z', 'target_close_at': '2026-11-16T21:00:00Z'}, 'forecast': {'status': 'not_ready'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only', Date.parse('2026-11-16T03:00:00Z')));")
    for text in ('미국 거래일', '2026-11-13', '2026-11-16', '2026-11-17', '23:30', '06:00', '한국 시각'):
        assert text in out
    assert '내일' not in out


def test_probability_labels_and_market_cards_follow_display_order():
    item = {'market': 'KOSPI', 'context': {}, 'forecast': {'status': 'not_ready'}, 'incumbent_reference': {'calibration_ready': True, 'status': 'rejected', 'p_up': 0.63, 'p_down': 0.37}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));")
    assert '상승 확률' in out
    assert '하락 확률' in out
    assert '(연구용)' not in out
    entries = [{'item': {'market': m, 'context': {}, 'forecast': {'status': 'not_ready'}}, 'runStatus': 'research_only'} for m in ['US_NASDAQ', 'KOSDAQ', 'US_SPX', 'KOSPI']]
    grid = run_ui('console.log(renderForecastGrid(' + json.dumps(entries) + ", ['KOSPI', 'KOSDAQ']));")
    assert grid.index('코스피') < grid.index('코스닥')
    assert 'S&amp;P 500' not in grid


def test_missing_live_anchor_explains_saved_model_not_missing_prices_and_shows_carry_forward():
    item = {'market': 'KOSPI', 'carried_forward': True, 'context': {'asof_date': '2026-11-13', 'target_date': '2026-11-16'}, 'forecast': {'status': 'not_ready', 'gate_reasons': ['missing_live_anchor_snapshot']}, 'incumbent_reference': {'calibration_ready': True, 'status': 'rejected', 'p_up': 0.63, 'p_down': 0.37}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));")
    assert '63.00%' in out and '이전 공개 결과' in out
    assert '검증 미통과' not in out
    assert '품질 기준 미통과' not in out
    assert 'missing_live_anchor_snapshot' not in out


def test_clock_refresh_updates_relative_dates_and_expiry_without_fetch():
    program = '''
const clock = {}, asof = {dataset:{kstDate:'2026-11-16',kstKind:'asof'}}, target = {dataset:{kstDate:'2026-11-17',kstKind:'target'}};
const expiry = {dataset:{targetDate:'2026-11-17',targetClose:'2026-11-17T06:30:00Z',krSession:''}};
const rangeExpiry = {dataset:{targetDate:'2026-11-17',krSession:''}};
const unknownUSExpiry = {dataset:{targetDate:'2026-11-17'}};
const rangePublication = {dataset:{targetDate:'2026-11-17'},hidden:false};
globalThis.document = {getElementById:()=>clock, querySelectorAll: selector => ({
  '[data-kst-date]': [asof,target], '[data-target-date]': [expiry,rangePublication],
  '.session-ended': [expiry,rangeExpiry,unknownUSExpiry]
})[selector]};
globalThis.fetch = () => { throw Error('unexpected data fetch'); };
refreshKstLabels(Date.parse('2026-11-16T15:30:00Z'));
console.log(JSON.stringify([clock.textContent,asof.textContent,target.textContent,expiry.hidden]));
refreshKstLabels(Date.parse('2026-11-17T06:31:00Z'));
console.log(JSON.stringify([expiry.hidden,rangeExpiry.hidden,unknownUSExpiry.hidden,rangePublication.hidden]));
'''
    lines = run_ui(program).splitlines()
    clock, asof, target, hidden = json.loads(lines[0])
    assert '2026-11-17 00:30' in clock and asof == '' and target == ' (오늘)' and hidden
    assert json.loads(lines[1]) == [False, False, True, False]


def test_us_next_refit_date_is_identified_as_exchange_date_not_completion_date():
    item = {'market': 'US_SPX', 'context': {}, 'forecast': {'status': 'not_ready'}, 'fit_state': {'next_refit_asof': '2026-12-09'}}
    out = run_ui('console.log(renderCard(' + json.dumps(item) + ", 'research_only'));")
    assert 'S&amp;P 500' in out
    assert '다음 고정 학습' not in out
    assert '검증이 필요' not in out


def test_us_missing_or_invalid_session_times_do_not_become_current_time_or_epoch():
    program = '''
const now = Date.parse('2026-11-16T03:00:00Z');
for (const value of [undefined, null, '', 'invalid']) {
  const item = {market:'US_SPX', context:{target_date:'2026-11-16', issuance_deadline:value, target_close_at:value}, forecast:{status:'not_ready'}};
  console.log(renderCard(item, 'research_only', now).match(/한국 시각 · 개장 [^<]+/)[0]);
}
console.log(formatKstClock(now));
'''
    lines = run_ui(program).splitlines()
    assert lines[:4] == ['한국 시각 · 개장 확인 불가 · 종료 확인 불가'] * 4
    assert lines[4] == '2026-11-16 12:00'


def test_page_places_automatic_korea_and_us_in_a_then_separate_research_models():
    html = HTML.read_text()
    assert 'href="#model-a"' in html
    assert html.index('id="model-a"') < html.index('id="korea-forecast"') < html.index('id="content"')
    assert html.index('id="content"') < html.index('id="model-b"') < html.index('id="model-c"')
    assert html.index('id="model-c"') < html.index('id="model-bc-range"') < html.index('id="model-compare"')
    assert 'assets/forecast-ranges.js' in html
    assert '수동 예측' in (HTML.parent / 'assets/model-b.js').read_text()


def test_context_charts_immediately_follow_the_live_forecasts():
    sections = re.findall(r'^  <section(?: id="([^"]+)")?[ >]', HTML.read_text(), re.MULTILINE)
    first = sections.index('model-a')
    assert sections[first:first + 6] == [
        'model-a', 'model-b', 'model-c', 'kospi-flow', 'overseas-market-charts', 'margin-credit',
    ]

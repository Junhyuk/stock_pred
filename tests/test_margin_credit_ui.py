import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'docs/assets/margin-credit.js'
PAGE = ROOT / 'docs/reliable-index-forecast.html'


def render(payload, *, failed=False):
    assert SCRIPT.exists(), 'margin credit chart script is missing'
    program = r'''
const vm = require('vm');
const fs = require('fs');
const payload = JSON.parse(process.argv[2]);
const nodes = {
  'margin-credit-status': {textContent: ''},
  'margin-credit-content': {innerHTML: ''},
};
const context = {
  document: {getElementById: id => nodes[id]},
  fetch: async url => {
    if (url !== 'data/margin_credit_chart.json' || process.argv[3] === 'true') throw Error('network');
    return {ok: true, json: async () => payload};
  },
  Intl, Date, Number, Math, Array, Object, String, Promise, console,
};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
setTimeout(() => console.log(JSON.stringify(nodes)), 10);
'''
    result = subprocess.run(['node', '-e', program, str(SCRIPT), json.dumps(payload), str(failed).lower()],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def fixture():
    return {'schema_version': 'margin_credit_chart_v1', 'unit': 'KRW',
            'source': '금융투자협회 FreeSIS',
            'source_url': 'https://freesis.kofia.or.kr/stat/FreeSIS.do?parentDivId=MSIS10000000000000&serviceId=STATSCU0100000140',
            'snapshot_received_at': '2026-11-19T12:17:37.109257+00:00',
            'markets': {'KOSPI': [{'date': '2026-11-17', 'balance_krw': 20384958000000},
                                  {'date': '2026-11-18', 'balance_krw': 20323672000000}],
                        'KOSDAQ': [{'date': '2026-11-17', 'balance_krw': 5948897000000},
                                   {'date': '2026-11-18', 'balance_krw': 5942694000000}]}}


def test_credit_graph_displays_market_dates_units_receipt_and_text_alternative():
    html = PAGE.read_text()
    assert 'assets/margin-credit.js' in html
    assert 'margin-credit-content' in html
    nodes = render(fixture())
    content = nodes['margin-credit-content']['innerHTML']
    for expected in ('KOSPI', 'KOSDAQ', '20.324', '5.943', '2026-11-18', '조 원', '<svg', '<table', 'aria-label'):
        assert expected in content
    assert '2026-11-19' in nodes['margin-credit-status']['textContent']
    assert '21:17' in nodes['margin-credit-status']['textContent']


def test_missing_data_is_explicit_and_network_failure_does_not_fake_refresh():
    data = fixture()
    data['markets']['KOSDAQ'] = []
    nodes = render(data)
    assert '자료가 없습니다' in nodes['margin-credit-content']['innerHTML']
    nodes = render(data, failed=True)
    assert '불러오지 못' in nodes['margin-credit-status']['textContent']
    assert '2026-11-18' not in nodes['margin-credit-content']['innerHTML']


def test_invalid_series_does_not_draw_misleading_chart():
    data = fixture()
    data['markets']['KOSPI'][1]['date'] = '2026-11-16'
    nodes = render(data)
    content = nodes['margin-credit-content']['innerHTML']
    assert '자료가 없습니다' in content
    assert '5.943' in content
    assert '20.324' not in content


def test_invalid_source_does_not_label_data_as_freesis():
    data = fixture()
    data['source'] = 'unverified'
    nodes = render(data)
    assert '불러오지 못' in nodes['margin-credit-status']['textContent']
    assert nodes['margin-credit-content']['innerHTML'] == ''

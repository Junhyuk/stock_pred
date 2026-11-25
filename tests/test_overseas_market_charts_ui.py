import json
import subprocess
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "docs/assets/overseas-market-charts.js"


def render(prices, flow, selected_range=None, holdings=None):
    program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const [prices, flow, range, holdings] = JSON.parse(process.argv[2]);
const ids = ['overseas-market-charts', 'overseas-chart-range', 'overseas-price-status',
  'overseas-price-SKHY', 'overseas-price-KORU', 'sk-hynix-flow-content', 'overseas-holdings-content'];
const nodes = Object.fromEntries(ids.map(id => [id, {
  textContent: '', innerHTML: '', value: '30', disabled: true,
  querySelector: () => null, querySelectorAll: () => [],
  setAttribute(name, value) { this[name] = value; },
  addEventListener(event, handler) { this[event] = handler; },
}]));
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: id => nodes[id]},
  window: {addEventListener() {}},
  URL,
  fetch: async url => {
    const payload = {
      'data/overseas-market-prices.json': prices,
      'data/sk-hynix-investor-flow.json': flow,
      'data/overseas-institutional-holdings.json': holdings,
    }[url];
    return {ok: payload != null, json: async () => payload};
  },
});
setImmediate(() => {
  if (range) {
    nodes['overseas-chart-range'].value = range;
    nodes['overseas-chart-range'].change();
  }
  console.log(JSON.stringify(nodes));
});
"""
    result = subprocess.run(
        ["node", "-e", program, str(SCRIPT), json.dumps([prices, flow, selected_range, holdings])],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def price_payload():
    return {
        "schema_version": 1,
        "status": "partial",
        "generated_at": "2026-11-25T02:05:00Z",
        "instruments": {
            "SKHY": {
                "symbol": "SKHY",
                "label": "SK하이닉스 ADR",
                "market": "NASDAQ",
                "currency": "USD",
                "status": "unavailable",
                "source": {"label": "Nasdaq"},
                "last_session": None,
                "points": [],
            },
            "KORU": {
                "symbol": "KORU",
                "label": "Direxion Daily MSCI South Korea Bull 3X Shares",
                "market": "NYSE Arca",
                "currency": "USD",
                "status": "stale",
                "source": {"label": "Yahoo Finance", "url": "https://finance.yahoo.com/quote/KORU/"},
                "last_session": "2026-11-24",
                "points": [
                    {"date": "2026-11-23", "close": 10.0},
                    {"date": "2026-11-24", "close": 8.5},
                ],
                "previous_close": 10.0,
            },
        },
    }


def flow_payload(unit="shares"):
    return {
        "schema_version": 1,
        "status": "ready",
        "generated_at": "2026-11-25T01:30:00Z",
        "issuer": "SK하이닉스",
        "symbol": "000660",
        "market": "KRX",
        "unit": unit,
        "source": {"label": "KRX", "url": "https://data.krx.co.kr/"},
        "series": [
            {"date": "2026-11-23", "foreign": 1500, "institution": -1530, "individual": None},
            {"date": "2026-11-24", "foreign": 0, "institution": None, "individual": -30},
        ],
    }


def test_adr_missing_state_keeps_independent_stale_koru_prices_and_verified_dates():
    nodes = render(price_payload(), None)
    missing = nodes["overseas-price-SKHY"]["innerHTML"]
    koru = nodes["overseas-price-KORU"]["innerHTML"]
    assert "종가 자료가 없습니다" in missing
    assert "<canvas" not in missing
    for expected in ("$8.50", "−15.00%", "2026-11-24", "지난 자료", "https://finance.yahoo.com", "<canvas", "<table"):
        assert expected in koru
    assert "11:05" in nodes["overseas-price-status"]["textContent"]
    assert "불러오지 못" in nodes["sk-hynix-flow-content"]["innerHTML"]


def test_domestic_flow_keeps_negative_zero_and_missing_observations_distinct():
    nodes = render(None, flow_payload(unit="KRW"))
    flow = nodes["sk-hynix-flow-content"]["innerHTML"]
    for expected in ("외국인", "기관", "개인", "순매수 (원)", "2026-11-24", "10:30", "https://data.krx.co.kr", "+1,500", "−1,530", "자료 없음"):
        assert expected in flow
    assert "<canvas" in flow
    assert ">0</td>" in flow
    assert "종가 자료를 불러오지 못" in nodes["overseas-price-status"]["textContent"]


def test_range_switch_limits_each_table_to_saved_sessions():
    payload = flow_payload()
    payload["series"] = [
        {"date": (date(2026, 9, 1) + timedelta(days=offset)).isoformat(), "foreign": offset,
         "institution": -offset, "individual": 0}
        for offset in range(35)
    ]
    default = render(None, payload)["sk-hynix-flow-content"]["innerHTML"]
    expanded = render(None, payload, "90")["sk-hynix-flow-content"]["innerHTML"]
    assert "2026-09-01" not in default
    assert "2026-09-01" in expanded
    assert "순매수 (주)" in expanded
    assert "35거래일" in expanded


def test_price_chart_shows_history_without_claiming_unverified_previous_session_return():
    data = price_payload()
    del data["instruments"]["KORU"]["previous_close"]
    price = render(data, None)["overseas-price-KORU"]["innerHTML"]
    assert "전 거래일 대비 자료 없음" in price
    assert "−15.00%" not in price
    assert "$10.00" in price and "$8.50" in price


def test_holdings_links_to_selected_quarter_end_positions_with_sec_filing_dates():
    filing = {
        "manager": "Quarter & Company",
        "shares": 70311,
        "market_value_usd": 12947771,
        "period_end": "2026-09-30",
        "filed_at": "2026-11-16T23:31:06-05:00",
        "accession": "0000000000-26-000001",
        "table_url": "https://www.sec.gov/Archives/edgar/data/example.xml",
    }
    data = {
        "schema_version": 1,
        "status": "ready",
        "as_of": "2026-11-25",
        "coverage": "selected_filings",
        "securities": {"SKHY": {"cusip": "78393W107", "filings": [
            filing,
            {**filing, "manager": "Untrusted", "table_url": "https://www.sec.gov.example.com/"},
        ]}},
    }
    holdings = render(None, None, holdings=data)["overseas-holdings-content"]["innerHTML"]
    for expected in ("SKHY", "Quarter &amp; Company", "2026-09-30", "70,311", "2026-11-16", filing["table_url"]):
        assert expected in holdings
    assert "Untrusted" not in holdings

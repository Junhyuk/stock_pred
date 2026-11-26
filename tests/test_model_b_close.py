import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from roboquant.models.model_b_close import expected_index, next_close_returns, period_close_mae

ROOT = Path(__file__).resolve().parents[1]
MODEL_B_JS = ROOT / "docs" / "assets" / "model-b.js"
# Pinned copy: the published file is rewritten by every manual Model B run.
PINNED_FORECAST = ROOT / "tests" / "fixtures" / "model_b_forecast_sample.json"


def test_expected_index_equals_close_times_one_plus_return():
    close = 4708.54
    expected_return = -0.00321
    assert expected_index(close, expected_return) == close * (1 + expected_return)
    assert expected_index(635.45, 0.0) == 635.45


def test_last_session_without_next_close_is_not_a_label():
    closes = np.array([100.0, 101.0, 103.0])
    returns = next_close_returns(closes)
    assert np.isnan(returns[-1])
    assert np.isfinite(returns).tolist() == [True, True, False]
    assert returns[0] == pytest.approx(0.01)
    assert returns[1] == pytest.approx(103.0 / 101.0 - 1.0)


def test_close_mae_is_mean_absolute_index_error():
    scored = period_close_mae(
        [100.0, 200.0, 300.0],
        [0.01, np.nan, 0.02],
        [0.03, 0.01, -0.01],
        ["2025-12-30", "2025-12-31", "2026-01-02"],
        end="2026-01-01",
    )
    assert scored["n"] == 1
    assert scored["close_mae"] == pytest.approx(abs(100.0 * 1.01 - 100.0 * 1.03))
    assert scored["start"] == "2025-12-30"


def test_model_b_js_passes_expected_index_and_keeps_manual_note():
    source = MODEL_B_JS.read_text()
    assert "expected_index: row.expected_index" in source
    assert "expected_return: row.expected_return" in source
    assert "수동 예측" in source
    program = r"""
const source = SOURCE;
const close = 4708.54;
const expectedReturn = 0.001;
const kosdaqClose = 635.45;
const kosdaqReturn = -0.002;
const payload = {
  schema_version: 'model_b_forecast_v1',
  generated_at: '2026-11-23T14:16:33.522385+00:00',
  markets: [
    {market: 'KOSPI', asof_date: '2026-11-23', target_date: '2026-11-24', close,
      p_up: 0.4685, p_down: 0.5315, trained_through: '2026-11-20', variant: '8층 미세조정',
      expected_return: expectedReturn, expected_index: close * (1 + expectedReturn)},
    {market: 'KOSDAQ', asof_date: '2026-11-23', target_date: '2026-11-24', close: kosdaqClose,
      p_up: 0.5214, p_down: 0.4786, trained_through: '2026-11-20',
      expected_return: kosdaqReturn, expected_index: kosdaqClose * (1 + kosdaqReturn)}
  ]
};
const seen = [];
const notes = [];
const status = {textContent: ''};
const document = {
  getElementById(id) {
    if (id === 'manual-model-b-cards') return {
      replaceChildren() {},
      appendChild(node) { notes.push(node.innerHTML); }
    };
    return status;
  },
  createElement() { return {className: '', innerHTML: ''}; },
};
const fetch = async () => ({ok: true, json: async () => payload});
const renderCard = (item, status) => {
  seen.push({market: item.market, status, forecast: item.forecast});
  return '<article></article>';
};
new Function('document', 'fetch', 'renderCard', 'escapeHtml', source)(document, fetch, renderCard, value => value);
setTimeout(() => {
  if (seen.length !== 2) {
    console.error(JSON.stringify(seen));
    process.exit(1);
  }
  console.log(JSON.stringify({seen, notes, status: status.textContent}));
}, 200);
""".replace("SOURCE", json.dumps(source))
    import subprocess
    completed = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    payload = json.loads(completed.stdout)
    forecasts = {row["market"]: row for row in payload["seen"]}
    assert forecasts["KOSPI"]["status"] == "research_only"
    kospi = forecasts["KOSPI"]["forecast"]
    assert kospi["expected_index"] == pytest.approx(4708.54 * (1 + 0.001))
    assert kospi["expected_return"] == pytest.approx(0.001)
    assert kospi["p_up"] == pytest.approx(0.4685)
    kosdaq = forecasts["KOSDAQ"]["forecast"]
    assert kosdaq["expected_index"] == pytest.approx(635.45 * (1 - 0.002))
    assert kosdaq["expected_return"] == pytest.approx(-0.002)
    rendered = "\n".join(payload["notes"])
    assert "수동 예측" in payload["status"]
    assert "8층 미세조정" in rendered


def test_postclose_b_displays_the_labeled_close_baseline():
    import subprocess

    program = r"""
const source = SOURCE;
const status = {};
const cards = {items: [], replaceChildren() {}, appendChild(node) { this.items.push(node); }};
const document = {getElementById: id => id === 'model-b-status' ? status : cards, createElement: () => ({})};
const payload = {schema_version: 'model_b_forecast_v1', issue_stage: 'postclose',
  target_date: '2026-11-26', target_close_at: '2026-11-26T06:30:00Z',
  generated_at: '2026-11-25T09:14:00Z', markets: [
  {market: 'KOSPI', target_date: '2026-11-26', close: 4700, p_up: 0.55, expected_index: 4700, expected_return: 0,
   interval: {level: .8, low: 4450, high: 4950}, variant: '장후 검증 기준선 · 종가 유지'}
]};
const fetch = async () => ({ok: true, json: async () => payload});
const renderCard = item => {
  console.log(JSON.stringify(item));
  return '<article></article>';
};
new Function('document', 'fetch', 'renderCard', 'escapeHtml', source)(document, fetch, renderCard, value => value);
setImmediate(() => console.log(JSON.stringify({status: status.textContent, card: cards.items[0].innerHTML})));
""".replace("SOURCE", json.dumps(MODEL_B_JS.read_text()))
    completed = subprocess.run(["node", "-e", program], capture_output=True, text=True, check=True)
    item, page = map(json.loads, completed.stdout.splitlines())
    assert item["forecast"]["expected_index"] == 4700
    assert item["forecast"]["expected_return"] == 0
    assert item["forecast"]["index_range_low"] == 4450
    assert item["forecast"]["index_range_high"] == 4950
    assert item["context"]["target_close_at"] == "2026-11-26T06:30:00Z"
    assert "검증 기준선" in page["status"]
    assert "종가 유지" in page["card"]


def check_return_formula(payload):
    assert payload["schema_version"] == "model_b_forecast_v1"
    assert payload["model_name"] == "모델 B"
    assert payload["production_weight"] == 0
    markets = {row["market"]: row for row in payload["markets"]}
    assert set(markets) == {"KOSPI", "KOSDAQ"}
    for row in markets.values():
        trained = date.fromisoformat(row["trained_through"])
        asof = date.fromisoformat(row["asof_date"])
        target = date.fromisoformat(row["target_date"])
        assert trained <= asof < target
        assert 0.0 <= row["p_up"] <= 1.0
        assert row["expected_index"] == pytest.approx(
            expected_index(row["close"], row["expected_return"]), abs=1e-6
        )
    return markets


def test_published_model_b_forecast_matches_return_formula():
    # Hermetic: reads the pinned copy, so a newer manual Model B run cannot change the outcome.
    markets = check_return_formula(json.loads(PINNED_FORECAST.read_text()))
    assert markets["KOSPI"]["variant"] == "8층 미세조정"
    assert "variant" not in markets["KOSDAQ"]

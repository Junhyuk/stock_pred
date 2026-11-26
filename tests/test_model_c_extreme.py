import importlib.util
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_model_c():
    path = ROOT / "scripts" / "run_model_c_extreme.py"
    spec = importlib.util.spec_from_file_location("run_model_c_extreme", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(day, probability, actual, close=1000.0, expected=None):
    expected = actual if expected is None else expected
    return {
        "session_date": day,
        "p_up": probability,
        "actual_return": actual,
        "expected_return": expected,
        "close": close,
        "actual_close": close * (1.0 + actual),
        "predicted_close": close * (1.0 + expected),
    }


def test_two_point_nine_percent_is_excluded_and_three_percent_is_included():
    model_c = load_model_c()
    assert model_c.is_extreme_return(0.029) is False
    assert model_c.is_extreme_return(-0.029) is False
    assert model_c.is_extreme_return(0.03) is True
    assert model_c.is_extreme_return(-0.03) is True
    assert model_c.is_extreme_return(0.030) is True
    assert model_c.is_extreme_return(float("nan")) is False
    assert model_c.is_extreme_return(None) is False


def test_setting_selector_does_not_use_2026_rows():
    model_c = load_model_c()
    kept = model_c.rows_for_setting_selection([
        {"session_date": "2025-12-31"},
        {"session_date": "2026-01-01"},
        {"session_date": "2026-11-20"},
    ])
    assert [row["session_date"] for row in kept] == ["2025-12-31"]

    poor_before = [
        _row("2025-11-03", 0.2, 0.04),
        _row("2025-12-01", 0.8, -0.04),
    ]
    strong_before = [
        _row("2025-11-03", 0.9, 0.04),
        _row("2025-12-01", 0.1, -0.04),
    ]
    chosen = model_c.choose_learning_rate([
        {"learning_rate": 1e-5, "rows": poor_before + [_row("2026-03-04", 0.95, 0.08), _row("2026-06-08", 0.05, -0.08)]},
        {"learning_rate": 5e-5, "rows": strong_before + [_row("2026-03-04", 0.05, 0.08), _row("2026-06-08", 0.95, -0.08)]},
    ])
    assert chosen == 5e-5

    tied = model_c.choose_learning_rate([
        {"learning_rate": 1e-5, "rows": strong_before + [_row("2026-03-04", 0.05, 0.08)]},
        {"learning_rate": 5e-5, "rows": strong_before + [_row("2026-03-04", 0.95, 0.08)]},
    ])
    assert tied == 1e-5


def test_manual_model_c_is_separate_from_main_model_c():
    html = (ROOT / "docs" / "reliable-index-forecast.html").read_text()
    assert html.count('id="model-c"') == 1
    assert html.count("assets/model-c.js") == 1
    assert html.index('id="model-a"') < html.index('id="model-c"') < html.index('<details id="model-compare"')
    assert html.index('id="model-c"') < html.index('id="model-c-cards"') < html.index('<details id="model-compare"')
    assert "assets/model-b.js" in html
    script = (ROOT / "docs" / "assets" / "model-c.js").read_text()
    assert "급등·급락 전용" in script
    assert "3%" in script
    page = html
    assert 'id="model-a"' in page and 'id="model-b"' in page


def test_model_c_displays_triggered_close_estimates_during_and_after_target_session():
    script = ROOT / "docs/assets/model-c.js"
    program = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = fs.readFileSync(process.argv[1], 'utf8');
const row = {market: 'KOSPI', asof_date: '2026-11-25', target_date: '2026-11-26',
  close: 4697.51, p_up: .2, p_normal: .53, p_down: .27,
  expected_index: 4650.53, expected_return: -.01, interval: {level: .8, low: 4380, high: 4995}};
const good = {schema_version: 'model_c_forecast_v1', status: 'ready', probability_mode: 'three_way',
  issue_stage: 'postclose', production_weight: 0, threshold: .03, asof_date: '2026-11-25',
  target_date: '2026-11-26', target_close_at: '2026-11-26T15:30:00+09:00',
  trigger_returns: {KOSPI: .03}, markets: [row]};
async function run(payload, now = '2026-11-26T06:29:59Z', fail = false) {
  const ms = Date.parse(now);
  class Clock extends Date { static now() { return ms; } }
  const nodes = {
    'model-c': {hidden: true}, 'model-c-nav': {hidden: true},
    'model-c-status': {textContent: ''},
    'model-c-cards': {items: [], replaceChildren() { this.items = []; }, appendChild(item) { this.items.push(item); }}
  };
  vm.runInNewContext(code, {
    Date: Clock, Intl, setTimeout() {}, targetEnded: context => Date.parse(context.target_close_at) <= ms,
    document: {getElementById: id => nodes[id], createElement: () => ({}), addEventListener() {}},
    fetch: async () => { if (fail) throw Error(); return {ok: true, json: async () => payload}; }
  });
  await new Promise(resolve => setImmediate(resolve));
  return {nodes};
}
(async () => {
  for (const move of [.03, -.03]) {
    const result = await run({...good, trigger_returns: {KOSDAQ: move}});
    assert.equal(result.nodes['model-c'].hidden, false);
    assert.equal(result.nodes['model-c-nav'].hidden, false);
    assert.match(result.nodes['model-c-status'].textContent, /코스닥/);
    assert.match(result.nodes['model-c-status'].textContent, /2026-11-25/);
    assert.match(result.nodes['model-c-status'].textContent, move > 0 ? /\+3.00%/ : /-3.00%/);
    assert.match(result.nodes['model-c-cards'].items[0].innerHTML, /중립<\/span><strong>53.00%/);
    const card = result.nodes['model-c-cards'].items[0].innerHTML;
    assert.match(card, /예상 종가<\/span><strong>4,650.53/);
    assert.match(card, /80% 지수 구간 .*<strong>4,380.00 ~ 4,995.00/);
    assert.match(card, /data-target-close="2026-11-26T15:30:00\+09:00"[^>]* hidden>/);
    const closed = await run(good, '2026-11-26T06:30:00Z');
    assert.equal(closed.nodes['model-c'].hidden, false);
    assert.equal(closed.nodes['model-c-nav'].hidden, false);
    assert.doesNotMatch(closed.nodes['model-c-cards'].items[0].innerHTML, /hidden>이미 종료/);
  }
  const invalid = [
    {...good, status: 'not_triggered'}, {...good, status: 'not_ready'},
    {...good, status: undefined}, {...good, trigger_returns: {KOSPI: .0299}},
    {...good, trigger_returns: {KOSPI: '0.03'}}, {...good, trigger_returns: {KOSPI: NaN}},
    {...good, target_close_at: '2026-11-26T15:30:00'},
    {...good, target_close_at: '2026-11-27T15:30:00+09:00'},
    {...good, asof_date: '2026-02-30'},
    {...good, markets: [{...row, asof_date: '2026-11-24'}]},
    {...good, markets: [{...row, target_date: '2026-11-27'}]},
    {...good, markets: [{...row, p_normal: .9}]}, {...good, markets: []}
  ];
  for (const payload of invalid) {
    const result = await run(payload);
    assert.equal(result.nodes['model-c'].hidden, true);
    assert.equal(result.nodes['model-c-nav'].hidden, true);
  }
  const legacy = await run({...good, markets: [{...row, interval: undefined, expected_index: undefined}]});
  assert.equal(legacy.nodes['model-c'].hidden, false);
  assert.match(legacy.nodes['model-c-cards'].items[0].innerHTML, /종가 추정 자료 없음/);
  const failed = await run(good, undefined, true);
  assert.equal(failed.nodes['model-c'].hidden, true);
  assert.equal(failed.nodes['model-c-nav'].hidden, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(["node", "-e", program, str(script)], check=True)


def test_model_c_is_hidden_before_javascript_runs():
    html = (ROOT / "docs/reliable-index-forecast.html").read_text()
    import re
    assert re.search(r'<a\b(?=[^>]*id="model-c-nav")(?=[^>]*\bhidden\b)[^>]*>', html)
    assert re.search(r'<section\b(?=[^>]*id="model-c")(?=[^>]*\bhidden\b)[^>]*>', html)

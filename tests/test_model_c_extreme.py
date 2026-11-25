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


def test_three_way_card_displays_neutral_without_a_close_forecast():
    script = ROOT / "docs/assets/model-c.js"
    program = """
const fs = require('node:fs');
const vm = require('node:vm');
const status = {textContent: ''};
const cards = {items: [], replaceChildren() {}, appendChild(item) { this.items.push(item); }};
const kospi = {market: 'KOSPI', asof_date: '2026-11-25', target_date: '2026-11-26',
  close: 4697.51, p_up: .2, p_normal: .53, p_down: .27};
const payload = {schema_version: 'model_c_forecast_v1', probability_mode: 'three_way',
  issue_stage: 'postclose', production_weight: 0, markets: [kospi, {...kospi, market: 'KOSDAQ', p_normal: .8}]};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: id => id === 'model-c-status' ? status : cards, createElement: () => ({})},
  fetch: async () => ({ok: true, json: async () => payload})
});
setImmediate(() => console.log(status.textContent, ...cards.items.map(item => item.innerHTML)));
"""
    output = subprocess.check_output(["node", "-e", program, str(script)], text=True)
    assert "장후 예측" in output
    assert "2026-11-26" in output
    assert "중립</span><strong>53.00%" in output
    assert "3% 이상 하락</span><strong>27.00%" in output
    assert "예상 지수" not in output
    assert "모델 C 결과가 없습니다." in output

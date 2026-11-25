import importlib.util
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
    assert html.index('<details id="model-compare"') < html.index('id="model-c-cards"')
    assert "assets/model-b.js" in html
    script = (ROOT / "docs" / "assets" / "model-c.js").read_text()
    assert "급등·급락 전용" in script
    assert "3%" in script
    page = html
    assert 'id="model-a"' in page and 'id="model-b"' in page

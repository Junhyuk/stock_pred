import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_applied_model_inputs", ROOT / "scripts" / "collect_applied_model_inputs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_naver_index_parser_rejects_a_broken_range(monkeypatch):
    payload = [
        {"localTradedAt": "2026-11-23", "openPrice": "4,734.90", "highPrice": "4,759.88",
         "lowPrice": "4,586.80", "closePrice": "4,708.54"},
        {"localTradedAt": "2026-11-20", "openPrice": "10", "highPrice": "9", "lowPrice": "8", "closePrice": "9"},
    ]

    def fake_fetch(url, timeout=40):
        assert "KOSPI" in url
        return json.dumps(payload).encode()

    monkeypatch.setattr(MODULE, "fetch", fake_fetch)
    bars = MODULE.naver_index_bars("KOSPI")
    assert bars["2026-11-23"]["close"] == 4708.54
    assert "2026-11-20" not in bars

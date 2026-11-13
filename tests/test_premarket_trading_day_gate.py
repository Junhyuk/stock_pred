"""07:50 KST trading-day gate, including a non-session exit of 0."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "premarket_trading_day_gate.py"
SPEC = importlib.util.spec_from_file_location("premarket_trading_day_gate", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)

KST = ZoneInfo("Asia/Seoul")


def test_explicit_calendar_skips_a_closed_date_and_runs_on_a_session():
    closed = gate.trading_day_decision(datetime(2026, 11, 8, 7, 50, tzinfo=KST), sessions=["2026-11-09"])
    assert closed["run"] is False
    assert closed["reason"] == "not_xkrx_session"
    assert closed["kst_date"] == "2026-11-08"
    assert gate.emit_decision(closed) == 0
    open_day = gate.trading_day_decision(datetime(2026, 11, 9, 7, 50, tzinfo=KST), sessions=["2026-11-09"])
    assert open_day["run"] is True
    assert open_day["reason"] == "xkrx_session"
    assert gate.emit_decision(open_day) == 0


def test_xkrx_calendar_matches_the_seoul_date():
    sunday = gate.trading_day_decision(datetime(2026, 11, 8, 7, 50, tzinfo=KST))
    monday = gate.trading_day_decision(datetime(2026, 11, 9, 7, 50, tzinfo=KST))
    assert sunday["run"] is False and sunday["reason"] == "not_xkrx_session"
    assert monday["run"] is True and monday["reason"] == "xkrx_session"


def test_unavailable_calendar_fails_closed(tmp_path, monkeypatch):
    decision = gate.trading_day_decision(datetime(2026, 11, 9, 7, 50, tzinfo=KST), sessions=object())
    assert decision["reason"] == "calendar_unavailable"
    output = tmp_path / "github_output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert gate.emit_decision(decision) == 1
    assert "run=false" in output.read_text(encoding="utf-8")


def test_gate_import_does_not_load_the_model_stack():
    probe = r"""
import importlib.util
import sys
from pathlib import Path
root = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("gate_check", root / "scripts" / "premarket_trading_day_gate.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
from datetime import datetime
from zoneinfo import ZoneInfo
decision = module.trading_day_decision(datetime(2026, 11, 9, 7, 50, tzinfo=ZoneInfo("Asia/Seoul")))
if not decision["run"]:
    raise SystemExit("thin calendar runtime unavailable: " + repr(decision))
banned = [
    name for name in sys.modules
    if name == "sklearn" or name.startswith("sklearn.")
    or name == "lightgbm" or name.startswith("lightgbm.")
    or name in {"yfinance", "streamlit", "fastapi", "uvicorn", "torch", "plotly", "feedparser"}
]
if banned:
    raise SystemExit("imported " + ",".join(sorted(banned)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(ROOT)],
        check=False, capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout

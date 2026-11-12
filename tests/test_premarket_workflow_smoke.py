"""The weekday forecast workflow stays a single 07:50 KST job."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "premarket-research-forecast.yml"


def test_forecast_workflow_runs_once_each_weekday_morning():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count('cron: "50 22 * * 0-4"') == 1
    assert "workflow_dispatch:" in text
    assert text.index("Install trading-day calendar") < text.index("Trading-day gate")
    assert "exchange-calendars" in text
    assert "scripts/run_reliable_index_forecast.py" in text
    assert "scripts/run_us_index_forecast.py" in text
    assert "docs/reliable-index-forecast.html" not in text.split("git add", 1)[-1]
    assert "git push" in text

"""The forecast workflow runs daily and only forecasts on trading days."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "premarket-research-forecast.yml"


def test_forecast_workflow_runs_once_each_morning():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count('cron: "50 22 * * *"') == 1
    assert "workflow_dispatch:" in text
    assert text.index("Install trading-day calendar") < text.index("Trading-day gate")
    assert "exchange-calendars" in text
    assert "scripts/run_reliable_index_forecast.py" in text
    assert "scripts/run_us_index_forecast.py" in text
    assert "docs/reliable-index-forecast.html" not in text.split("git add", 1)[-1]
    assert "git push" in text


def test_pages_deploy_follows_successful_default_branch_push():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/master'" in text
    assert "DEFAULT_BRANCH: master" in text
    assert "          ref: master" in text
    assert 'git push origin "HEAD:$DEFAULT_BRANCH"' in text
    assert "needs.forecast.outputs.published == 'true'" in text
    assert "actions/deploy-pages@v4" in text
    assert "docs/data/automation_status.json" in text
    assert "--news-context" not in text

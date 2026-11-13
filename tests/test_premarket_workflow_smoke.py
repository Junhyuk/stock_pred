"""Preparation and final issuance are separate, excluding KST Sunday."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "premarket-research-forecast.yml"


def test_forecast_workflow_prepares_then_issues_on_kst_monday_to_saturday():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count('cron: "50 22 * * 0-5"') == 1
    assert text.count('cron: "0 23 * * 0-5"') == 1
    assert "workflow_dispatch:" in text
    assert text.index("Resolve actual-clock operation") < text.index("Install forecast and verification stack")
    assert "--phase freeze" in text and "--phase inputs" in text
    assert "scripts/run_reliable_index_forecast.py" in text
    assert "scripts/run_us_index_forecast.py" in text
    assert "docs/reliable-index-forecast.html" not in text.split("git add", 1)[-1]
    assert "git push" in text


def test_pages_deploy_follows_successful_default_branch_push():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/master'" in text
    assert "          ref: master" in text
    assert 'git push origin HEAD:master' in text
    assert text.index('git push origin HEAD:master') < text.index("echo 'published=true'")
    assert "needs.forecast.outputs.published == 'true'" in text
    assert "actions/deploy-pages@v4" in text
    assert "--phase status" in text
    assert "Stage only tracked reviewed Pages files" in text
    assert "--news-context" not in text

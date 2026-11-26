"""The scheduled morning run owns preparation, issuance, and publication."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "premarket-research-forecast.yml"
PUSH_FORECAST = ROOT / "scripts" / "push_forecast_commit.sh"


def test_forecast_workflow_runs_once_at_0800_kst_monday_through_saturday():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert text.count('cron: "0 23 * * 0-5"') == 1
    assert text.count('cron:') == 1
    # Sunday-Friday at 23:00 UTC is Monday-Saturday at 08:00 in Seoul.
    sunday = datetime(2026, 11, 22, 23, tzinfo=UTC)
    local = [(sunday + timedelta(days=day)).astimezone(ZoneInfo("Asia/Seoul")) for day in range(6)]
    assert [stamp.weekday() for stamp in local] == [0, 1, 2, 3, 4, 5]
    assert {(stamp.hour, stamp.minute) for stamp in local} == {(8, 0)}
    assert 'schedule:' not in (ROOT / '.github/workflows/premarket-smoke.yml').read_text()
    assert text.index('--phase freeze') < text.index('--phase wait') < text.index('--phase inputs')
    assert "steps.operation.outputs.mode == 'daily'\n        run: python scripts/accuracy_operation.py --phase wait --mode daily" in text
    assert 'test_premarket_trading_day_gate.py tests/test_premarket_workflow_smoke.py' in text
    assert "workflow_dispatch:" in text
    assert text.index("Resolve actual-clock operation") < text.index("Install forecast and verification stack")
    assert "--phase freeze" in text and "--phase inputs" in text
    assert "scripts/collect_market_indicators.py" in text
    assert "scripts/run_reliable_index_forecast.py" in text
    assert "scripts/run_us_index_forecast.py" in text
    assert "docs/reliable-index-forecast.html" not in text.split("git add", 1)[-1]


def test_frozen_forecast_modes_share_the_safe_snapshot_and_cpu_path():
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert "morning" in workflow[True]["workflow_dispatch"]["inputs"]["mode"]["options"]
    steps = workflow["jobs"]["forecast"]["steps"]
    by_name = {step["name"]: step for step in steps}
    refresh = by_name["Refresh prices"]["run"]
    assert "steps.operation.outputs.frozen_forecast" in refresh
    assert "scripts/run_postclose_prices.py --morning" in refresh
    for name in ("Freeze actual preparation inputs", "Seal preparation checkpoint before waiting",
                 "Resolve immutable forecast inputs", "Persist derived model state from prepared inputs",
                 "Seal private cache", "Forecast Model B and conditionally Model C on CPU"):
        assert "steps.operation.outputs.frozen_forecast" in by_name[name]["if"], name
    for name in ("Korea research forecast", "US research forecast"):
        assert "steps.operation.outputs.frozen_forecast" in by_name[name]["continue-on-error"]
        assert steps.index(by_name["Forecast Model B and conditionally Model C on CPU"]) < steps.index(by_name[name])
    assert steps.index(by_name["Resolve immutable forecast inputs"]) < steps.index(
        by_name["Forecast Model B and conditionally Model C on CPU"]
    )


def test_pages_deploy_follows_successful_default_branch_push():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/master'" in text
    assert "          ref: master" in text
    assert text.index("bash scripts/push_forecast_commit.sh") < text.rindex("echo 'published=true'")
    assert "steps.bc_cpu.outputs.redeploy" in text
    assert "needs.forecast.outputs.published == 'true'" in text
    assert "actions/deploy-pages@v4" in text
    assert "--phase status" in text
    assert "Stage only tracked reviewed Pages files" in text
    assert "--news-context" not in text


@pytest.mark.parametrize("overlap", [False, True])
def test_publish_retries_a_moved_master_without_overwriting_a_conflict(tmp_path, overlap):
    remote, writer, runner = (tmp_path / name for name in ("origin", "writer", "runner"))

    def git(directory, *args):
        return subprocess.check_output(["git", "-C", str(directory), *args], text=True).strip()

    def configure(directory):
        git(directory, "config", "user.name", "Forecast Test")
        git(directory, "config", "user.email", "forecast@example.invalid")
        git(directory, "config", "commit.gpgSign", "false")

    git(tmp_path, "init", "--bare", "--initial-branch=master", str(remote))
    git(tmp_path, "clone", remote.as_uri(), str(writer))
    configure(writer)
    for name in ("forecast.json", "ledger.json", "unreviewed.json"):
        (writer / name).write_text("base\n")
    git(writer, "add", ".")
    git(writer, "commit", "-m", "initial")
    git(writer, "push", "origin", "master")
    git(tmp_path, "clone", "--depth=1", remote.as_uri(), str(runner))
    configure(runner)

    (runner / "forecast.json").write_text("morning forecast\n")
    git(runner, "commit", "-am", "publish forecast")
    (runner / "unreviewed.json").write_text("local only\n")
    changed = "forecast.json" if overlap else "ledger.json"
    (writer / changed).write_text("other publisher\n")
    git(writer, "commit", "-am", "advance remote master")
    git(writer, "push", "origin", "master")

    published = subprocess.run(["bash", PUSH_FORECAST], cwd=runner, capture_output=True, text=True)

    assert (runner / "unreviewed.json").read_text() == "local only\n"
    assert git(remote, "show", "master:unreviewed.json") == "base"
    assert git(remote, "show", f"master:{changed}") == "other publisher"
    if overlap:
        assert published.returncode != 0
        assert "CONFLICT" in published.stdout
    else:
        assert published.returncode == 0, published.stderr
        assert git(remote, "show", "master:forecast.json") == "morning forecast"

"""Only reviewed CPU forecasts and collection run on the GitHub runner.

The local GPU experiments stay outside the runner. The daily B/C CPU step
uses the same verified price snapshot and schedule as the after-close job.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {path.name: path.read_text(encoding="utf-8") for path in sorted((ROOT / ".github/workflows").glob("*.yml"))}

# Local GPU / research scripts that must never be executed by a workflow.
LOCAL_ONLY_SCRIPTS = (
    "run_model_b_close_forecast",
    "run_model_c_extreme",
    "run_incremental_index_learner",
    "run_gpu_large_index_model",
    "run_series_token_accuracy",
    "run_finetune_2026_accuracy",
    "run_eight_layer_shadow_search",
    "run_bc_research",
    "run_local_bc_pipeline",
    "compare_20d_timeseries_2026",
    "deep_ml_blend_eval",
    "validate_2026_models",
)
# Model A: the after-close research forecast of the Korea and US indices.
MODEL_A_SCRIPTS = ("run_reliable_index_forecast.py", "run_us_index_forecast.py")
# The one other automatic forecast on the runner, a research shadow with its own forward
# ledger (configs/probability_shadow.yaml). Disable it by removing the schedule and this entry.
OTHER_AUTOMATIC_FORECASTS = {"run_probability_research.py": {"premarket-us-shadow.yml"}}


def test_no_workflow_runs_a_local_gpu_model():
    for name, text in WORKFLOWS.items():
        for script in LOCAL_ONLY_SCRIPTS:
            assert script not in text, f"{name} references local-only model script {script}"


def test_model_a_is_forecast_by_the_after_close_workflow_only():
    forecast = WORKFLOWS["premarket-research-forecast.yml"]
    for script in MODEL_A_SCRIPTS:
        assert script in forecast
    for name, text in WORKFLOWS.items():
        if name != "premarket-research-forecast.yml":
            assert not any(script in text for script in MODEL_A_SCRIPTS), name


def test_other_automatic_forecasts_on_the_runner_are_exactly_the_listed_ones():
    for script, allowed in OTHER_AUTOMATIC_FORECASTS.items():
        assert {name for name, text in WORKFLOWS.items() if script in text} == allowed


def test_cfd_chart_refresh_runs_after_forecasting_and_before_pages_staging():
    workflow = "premarket-research-forecast.yml"
    script = "scripts/refresh_cfd_dashboard.py"
    assert {name for name, text in WORKFLOWS.items() if script in text} == {workflow}
    steps = yaml.safe_load(WORKFLOWS[workflow])["jobs"]["forecast"]["steps"]
    [chart_index] = [i for i, step in enumerate(steps) if script in step.get("run", "")]
    chart = steps[chart_index]
    assert steps[chart_index + 1]["id"] == "publish"
    model_steps = [
        i
        for i, step in enumerate(steps)
        if step.get("id") in {"kr_forecast", "us_forecast", "seal"}
        or "--phase publish-models" in step.get("run", "")
    ]
    assert len(model_steps) == 4
    assert max(model_steps) < chart_index
    assert chart["if"] == "steps.operation.outputs.collect == 'true'"
    assert chart["continue-on-error"] is True
    assert "--output-dir docs/data" in chart["run"]
    assert "vars.CFD_KOFIA_AGGREGATES_APPROVED" in chart["env"]["CFD_KOFIA_AGGREGATES_APPROVED"]


def test_runner_install_excludes_the_gpu_stack():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    assert not any("torch" in requirement.lower() for requirement in extras["forecast"])
    assert any("torch" in requirement.lower() for requirement in extras["gpu"])
    for name, text in WORKFLOWS.items():
        assert "[gpu" not in text, f"{name} installs the gpu extra"
        assert "torch" not in text.lower(), f"{name} mentions torch"


def test_cpu_bc_uses_existing_daily_snapshot_and_bounded_step():
    workflow = "premarket-research-forecast.yml"
    name = "scripts/run_bc_cpu_afterclose.py"
    assert {file for file, text in WORKFLOWS.items() if name in text} == {workflow}
    config = yaml.safe_load(WORKFLOWS[workflow])
    steps = config["jobs"]["forecast"]["steps"]
    cpu = next(step for step in steps if step.get("id") == "bc_cpu")
    assert cpu["timeout-minutes"] == 1
    assert cpu["continue-on-error"] is True
    assert "steps.operation.outputs.mode == 'postclose'" in cpu["if"]
    assert "steps.prices.outputs.status == 'ready'" in cpu["if"]
    assert "steps.prices.outputs.prepared_manifest" in cpu["run"]
    assert steps.index(cpu) < next(i for i, step in enumerate(steps) if step.get("id") == "publish")
    assert config[True]["schedule"] == [{"cron": "0 9 * * 1-5"}]

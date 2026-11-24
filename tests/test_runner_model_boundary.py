"""The GitHub runner forecasts with Model A only and otherwise collects data.

Models B and C are trained and run on the local GPU machine. These checks fail if a
workflow starts referencing them, if the runner's install pulls in the GPU stack, or if
another automatic forecast appears on the runner without being listed here.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

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


def test_runner_install_excludes_the_gpu_stack():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    assert not any("torch" in requirement.lower() for requirement in extras["forecast"])
    assert any("torch" in requirement.lower() for requirement in extras["gpu"])
    for name, text in WORKFLOWS.items():
        assert "[gpu" not in text, f"{name} installs the gpu extra"
        assert "torch" not in text.lower(), f"{name} mentions torch"

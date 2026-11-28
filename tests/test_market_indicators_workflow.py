"""Market context collection shares the clock and keeps validation isolated."""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow():
    path = ROOT / ".github/workflows/premarket-research-forecast.yml"
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 boolean coercion.
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def test_indicators_share_existing_morning_clock_and_private_cache_window():
    config = workflow()
    assert config["on"]["schedule"] == [{"cron": "0 23 * * 0-5"}]
    assert len(config["jobs"]) == 2
    assert config["concurrency"]["cancel-in-progress"] == "false"
    steps = config["jobs"]["forecast"]["steps"]
    names = [step["name"] for step in steps]
    collector = next(step for step in steps if step["name"] == "Collect bounded market indicators")
    assert collector["if"] == "steps.operation.outputs.collect == 'true'"
    assert collector["timeout-minutes"] == "1"
    assert collector["continue-on-error"] == "true"
    assert "--output-dir docs/data" in collector["run"]
    assert "--sqlite data/processed/robo_session_inputs.sqlite" in collector["run"]
    assert names.index("Open private cache") < names.index(collector["name"])
    assert names.index(collector["name"]) < names.index(
        "Seal preparation checkpoint before waiting"
    )


def test_indicator_credentials_are_scoped_to_collection_steps():
    config = workflow()
    credentials = {"BOK_ECOS_API_KEY", "DATA_GO_KR_SERVICE_KEY"}
    job = config["jobs"]["forecast"]
    assert not credentials.intersection(job["env"])
    allowed = {
        "Collect bounded market indicators": ("scripts/collect_market_indicators.py", credentials),
        "Validate market indicators without publication": ("scripts/collect_market_indicators.py", credentials),
        "Refresh exact Korean benchmark observations": ("scripts/run_benchmark_forecasts.py collect", {"DATA_GO_KR_SERVICE_KEY"}),
        "Refresh public market chart history": ("scripts/collect_market_chart_history.py", credentials),
    }
    collectors = set()
    for step in job["steps"]:
        env = step.get("env", {})
        if credentials.intersection(env):
            script, keys = allowed[step["name"]]
            collectors.add(step["name"])
            assert script in step["run"]
            assert credentials.intersection(env) == keys
            for key in keys:
                assert env[key] == "${{ secrets." + key + " }}"
    assert collectors == allowed.keys()


def test_validation_collects_in_temp_paths_and_exposes_code_errors():
    steps = workflow()["jobs"]["forecast"]["steps"]
    collector = next(
        step for step in steps if step["name"] == "Validate market indicators without publication"
    )
    assert collector["if"] == "steps.operation.outputs.mode == 'validate'"
    assert collector["timeout-minutes"] == "1"
    assert collector.get("continue-on-error", "false") == "false"
    assert '--output-dir "$RUNNER_TEMP/market-indicators-validation"' in collector["run"]
    assert '--sqlite "$RUNNER_TEMP/market-indicators-private/indicators.sqlite"' in collector["run"]
    assert "--seed-dir docs/data" in collector["run"]
    assert "--output-dir docs/data" not in collector["run"]
    assert "|| true" not in collector["run"]
    validation = next(step for step in steps if step["name"] == "Validate market indicator contracts")
    for name in (
        "test_market_indicators",
        "test_official_yields",
        "test_official_credit",
        "test_market_indicators_workflow",
    ):
        assert f"tests/{name}.py" in validation["run"]
    upload = next(step for step in steps if step["name"] == "Upload compact validation status")
    assert "market-indicators-validation/market_indicators_status.json" in upload["with"]["path"]
    full = next(step for step in steps if step["name"] == "Validate without publication")
    assert full["if"] == "steps.operation.outputs.mode == 'validate' && !inputs.indicators_only"
    assert steps.index(collector) < steps.index(full)
    manual = workflow()["on"]["workflow_dispatch"]["inputs"]["indicators_only"]
    assert manual["default"] == "false"

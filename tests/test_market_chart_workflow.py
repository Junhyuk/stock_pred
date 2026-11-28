"""Check that chart collection is cached, published and isolated from issuance."""
from pathlib import Path

import yaml

from roboquant.index_forecast.automation_state import public_payload_paths

ROOT = Path(__file__).resolve().parents[1]


def test_daily_chart_pipeline_uses_working_and_frozen_inputs_separately(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/premarket-research-forecast.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["forecast"]["steps"]}
    names = list(steps)
    collect = "Refresh public market chart history"
    export = "Export market chart model inputs from the issued run"
    assert names.index("Freeze actual preparation inputs") < names.index(collect) < names.index("Seal private cache")
    assert steps[collect]["env"]["CHART_SQLITE"] == "${{ steps.refresh.outputs.sqlite }}"
    assert names.index("Korea research forecast") < names.index(export) < names.index("Commit reviewed public payloads")
    assert steps[export]["env"]["PREPARED_MANIFEST"] == "${{ steps.prices.outputs.prepared_manifest }}"
    for step in (collect, export):
        assert steps[step]["timeout-minutes"] <= 2
        assert steps[step]["continue-on-error"] is True
    outputs = {"market-chart-history.json", "market-chart-model-inputs.json"}
    for name in outputs:
        (tmp_path / name).write_text("{}")
    (tmp_path / "history.sqlite").touch()
    assert {path.name for path in public_payload_paths(tmp_path)} == outputs

from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_premarket_global_pipeline.py"
SPEC = importlib.util.spec_from_file_location("run_premarket_global_pipeline", SCRIPT_PATH)
assert SPEC is not None
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)


def test_premarket_pipeline_runs_final_shadow_after_regime() -> None:
    commands = pipeline.build_commands(
        Namespace(
            config="configs/global_market.yaml",
            top50_config="configs/top50_normal.yaml",
            from_date="2022-01-01",
            to_date="latest",
            cutoff="latest",
            prediction_date="latest",
        )
    )
    scripts = [command[1] for command in commands]

    assert scripts == [
        "scripts/collect_global_market_daily.py",
        "scripts/collect_premarket_snapshot.py",
        "scripts/build_market_regime.py",
        "scripts/run_premarket_shadow.py",
    ]
    assert commands[-1][-4:] == [
        "--stage",
        "final_premarket",
        "--date",
        "latest",
    ]
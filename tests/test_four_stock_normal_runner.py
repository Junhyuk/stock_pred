from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_four_stock_normal.py"
SPEC = importlib.util.spec_from_file_location("run_four_stock_normal", SCRIPT_PATH)
assert SPEC is not None
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def test_four_stock_normal_runner_defaults_to_high_quality_default_speed() -> None:
    assert runner.EXECUTION_MODEL == "5.5"
    assert runner.EXECUTION_QUALITY == "high"
    assert runner.EXECUTION_SPEED == "default"

    steps = runner.build_steps(
        Namespace(
            config="configs/two_stock_demo.yaml",
            flow_lookback_days=90,
            skip_collect=False,
            skip_enrichment=False,
        )
    )

    names = [step.name for step in steps]
    assert names[0] == "collect_kospi100"
    assert "collect_market_metrics" in names
    assert "collect_investor_flows" in names
    assert "train_3M" in names
    assert "train_6M" in names
    assert names[-1] == "price_gap_backtest"
    assert [step.name for step in steps if step.optional] == [
        "collect_market_metrics",
        "collect_investor_flows",
        "price_gap_backtest",
    ]

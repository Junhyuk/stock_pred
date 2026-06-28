from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_top50_normal.py"
SPEC = importlib.util.spec_from_file_location("run_top50_normal", SCRIPT_PATH)
assert SPEC is not None
runner = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(runner)


def test_top50_normal_runner_dry_run_order() -> None:
    assert runner.EXECUTION_MODEL == "5.5"
    assert runner.EXECUTION_QUALITY == "high"
    assert runner.EXECUTION_SPEED == "default"

    steps = runner.build_steps(
        Namespace(
            config="configs/top50_normal.yaml",
            universe_config="configs/universe_top50.yaml",
            provider="fdr_poc",
            flow_lookback_days=90,
            skip_refresh=False,
            skip_collect=False,
            skip_enrichment=False,
        )
    )

    names = [step.name for step in steps]
    assert names[:2] == ["refresh_universe", "collect_prediction_universe_prices"]
    assert names[2:5] == ["collect_market_news", "collect_market_metrics", "collect_investor_flows"]
    assert "train_2M" in names
    assert "train_3M" in names
    assert "train_6M" in names
    assert "generate_long_short_predictions" in names
    assert "long_short_backtest_2M" in names
    assert "long_short_backtest_6M" in names
    assert names[-2:] == ["dashboard_snapshot", "market_move_explanations"]
    assert [step.name for step in steps if step.optional] == [
        "collect_market_news",
        "collect_market_metrics",
        "collect_investor_flows",
    ]

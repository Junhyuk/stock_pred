from __future__ import annotations

import importlib.util
import sys
from argparse import Namespace
from pathlib import Path

import pandas as pd

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_us_market_premarket_update.py"
SPEC = importlib.util.spec_from_file_location("run_us_market_premarket_update", SCRIPT_PATH)
assert SPEC is not None
pipeline = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = pipeline
SPEC.loader.exec_module(pipeline)

TOMORROW_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_tomorrow_market_ml.py"
TOMORROW_SPEC = importlib.util.spec_from_file_location("run_tomorrow_market_ml", TOMORROW_SCRIPT_PATH)
assert TOMORROW_SPEC is not None
tomorrow = importlib.util.module_from_spec(TOMORROW_SPEC)
assert TOMORROW_SPEC.loader is not None
sys.modules[TOMORROW_SPEC.name] = tomorrow
TOMORROW_SPEC.loader.exec_module(tomorrow)


def test_focused_premarket_update_refreshes_koru_and_2m_prediction_breadth() -> None:
    steps = pipeline.build_steps(
        Namespace(
            config="configs/top50_normal.yaml",
            global_config="configs/global_market.yaml",
            llm_config="configs/local_llm_news.yaml",
            cutoff="latest",
            target_date="2026-07-24",
            skip_domestic=False,
            dry_run=True,
        )
    )
    names = [step.name for step in steps]
    commands = {step.name: step.command for step in steps}

    assert names.index("global_premarket") < names.index("us_sector_linkage") < names.index("koru_korea_linkage")
    assert names.index("koru_korea_linkage") < names.index("feature_matrix") < names.index("train_2m")
    assert names.index("train_2m") < names.index("predictions_2m") < names.index("market_outlook")
    assert commands["predictions_2m"][-2:] == ["--horizon", "2M"]


def test_today_status_ignores_weekly_legacy_research_row() -> None:
    status, stored_statuses = tomorrow.production_today_status(
        pd.DataFrame(
            [
                {"horizon": "TODAY", "forecast_status": "confirmed"},
                {"horizon": "TODAY", "forecast_status": "confirmed"},
                {"horizon": "WEEK", "forecast_status": "legacy"},
            ]
        )
    )

    assert status == "confirmed"
    assert stored_statuses == ["confirmed"]

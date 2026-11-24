import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "eight_layer_shadow", ROOT / "scripts" / "run_eight_layer_shadow_search.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_2026_blocks_cannot_choose_the_setting():
    blocks = [
        {"end": "2025-12-30", "n": 60, "brier": 0.30, "direction_hit": 0.5, "log_loss": 0.7, "probability_mae": 0.5, "mean_p_up": 0.5},
        {"end": "2026-03-31", "n": 60, "brier": 0.10, "direction_hit": 0.9, "log_loss": 0.2, "probability_mae": 0.2, "mean_p_up": 0.6},
    ]
    selected = MODULE.selection_blocks(blocks)
    assert [block["end"] for block in selected] == ["2025-12-30"]
    assert MODULE.pooled(selected)["brier"] == 0.30


def test_history_window_drops_older_training_rows():
    dates = ["2020-01-01", "2022-01-01", "2024-01-01", "2025-01-01"]
    assert MODULE.history_start(dates, 4, 3) == 1
    assert MODULE.history_start(dates, 4, None) == 0

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "train_models.py"
SPEC = importlib.util.spec_from_file_location("train_models_script", SCRIPT_PATH)
assert SPEC is not None
train_models = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = train_models
SPEC.loader.exec_module(train_models)


def test_long_horizons_exclude_short_horizon_koru_telegram_and_us_sector_features_by_default() -> None:
    columns = [
        "ret_21d",
        "koru_impact_score",
        "telegram_attention_score",
        "telegram_sentiment_score",
        "us_sector_impact_score",
        "us_sector_return_1d",
    ]
    config = {"horizons": {"3M": 63, "6M": 126, "9M": 189, "1Y": 252}}

    assert train_models._feature_columns_for_horizon(config, columns, "3M") == columns

    for horizon in ("6M", "9M", "1Y"):
        selected = train_models._feature_columns_for_horizon(config, columns, horizon)
        assert selected == ["ret_21d"]

from __future__ import annotations

from pathlib import Path

import pandas as pd

from roboquant.models.train import load_model_bundle, predict_with_bundle


def predict_from_model_path(model_path: str | Path, features: pd.DataFrame) -> pd.DataFrame:
    bundle = load_model_bundle(model_path)
    return predict_with_bundle(bundle, features)


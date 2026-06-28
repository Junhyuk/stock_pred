from __future__ import annotations

import pandas as pd


def equal_weight_portfolio(recommendations: pd.DataFrame) -> pd.DataFrame:
    if recommendations.empty:
        return pd.DataFrame(columns=["symbol", "weight"])
    frame = recommendations.copy()
    frame["weight"] = 1.0 / len(frame)
    return frame[["symbol", "weight"]]


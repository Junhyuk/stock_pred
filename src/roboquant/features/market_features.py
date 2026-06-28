from __future__ import annotations

import pandas as pd

from roboquant.utils import safe_rank_pct

MARKET_FEATURE_COLUMNS = [
    "market_cap",
    "per",
    "pbr",
    "eps",
    "bps",
    "dividend_yield",
    "market_cap_score",
    "value_score",
    "quality_score",
]


def compute_market_features(market_metrics: pd.DataFrame) -> pd.DataFrame:
    if market_metrics is None or market_metrics.empty:
        return pd.DataFrame(columns=["date", "symbol", *MARKET_FEATURE_COLUMNS])

    frame = market_metrics.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in ("market_cap", "per", "pbr", "eps", "bps", "dividend_yield"):
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    frame["market_cap_score"] = frame.groupby("date")["market_cap"].transform(safe_rank_pct)

    per_score = frame.groupby("date")["per"].transform(
        lambda series: safe_rank_pct(series.where(series > 0), ascending=False)
    )
    pbr_score = frame.groupby("date")["pbr"].transform(
        lambda series: safe_rank_pct(series.where(series > 0), ascending=False)
    )
    dividend_score = frame.groupby("date")["dividend_yield"].transform(safe_rank_pct)
    eps_score = frame.groupby("date")["eps"].transform(safe_rank_pct)
    bps_score = frame.groupby("date")["bps"].transform(safe_rank_pct)

    frame["value_score"] = pd.concat([per_score, pbr_score, dividend_score], axis=1).mean(axis=1)
    frame["quality_score"] = pd.concat([eps_score, bps_score], axis=1).mean(axis=1)
    frame["value_score"] = frame["value_score"].fillna(0.5).clip(0.0, 1.0)
    frame["quality_score"] = frame["quality_score"].fillna(0.5).clip(0.0, 1.0)

    return frame[["date", "symbol", *MARKET_FEATURE_COLUMNS]].drop_duplicates(["date", "symbol"])

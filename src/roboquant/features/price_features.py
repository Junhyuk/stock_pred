from __future__ import annotations

import numpy as np
import pandas as pd

from roboquant.utils import safe_rank_pct

FEATURE_COLUMNS = [
    "ret_21d",
    "ret_63d",
    "ret_126d",
    "ret_252d",
    "ma_gap_20d",
    "ma_gap_60d",
    "ma_gap_120d",
    "ma_gap_250d",
    "volatility_20d",
    "volatility_60d",
    "volume_ratio_20d",
    "trading_value_ma20",
    "close_to_52w_high",
    "rsi_14",
    "momentum_score",
    "volatility_score",
    "liquidity_score",
    "risk_score",
]


def compute_price_features(prices: pd.DataFrame, horizons: dict[str, int]) -> pd.DataFrame:
    """Build daily price-only features with current-and-past data only."""
    base = _prepare_prices(prices)
    if base.empty:
        return pd.DataFrame(columns=["date", "symbol", "horizon", "horizon_days", *FEATURE_COLUMNS])

    close = base["feature_close"]
    grouped = base.groupby("symbol", group_keys=False)

    for window in (21, 63, 126, 252):
        base[f"ret_{window}d"] = grouped["feature_close"].transform(
            lambda series, w=window: series / series.shift(w) - 1.0
        )

    for window in (20, 60, 120, 250):
        rolling_mean = grouped["feature_close"].transform(
            lambda series, w=window: series.rolling(w, min_periods=w).mean()
        )
        base[f"ma_gap_{window}d"] = close / rolling_mean - 1.0

    for window in (20, 60):
        base[f"volatility_{window}d"] = grouped["feature_close"].transform(
            lambda series, w=window: series.pct_change().rolling(w, min_periods=w).std()
            * np.sqrt(252)
        )

    volume_ma20 = grouped["volume"].transform(lambda series: series.rolling(20, min_periods=20).mean())
    base["volume_ratio_20d"] = base["volume"] / volume_ma20
    base["trading_value_ma20"] = grouped["trading_value"].transform(
        lambda series: series.rolling(20, min_periods=20).mean()
    )

    high_252 = grouped["feature_close"].transform(
        lambda series: series.rolling(252, min_periods=60).max()
    )
    base["close_to_52w_high"] = close / high_252 - 1.0
    base["rsi_14"] = grouped["feature_close"].apply(_rsi_14)

    momentum_rank_cols = []
    for column in ("ret_21d", "ret_63d", "ret_126d", "ret_252d"):
        rank_col = f"{column}_rank"
        base[rank_col] = base.groupby("date")[column].transform(safe_rank_pct)
        momentum_rank_cols.append(rank_col)

    base["momentum_score"] = base[momentum_rank_cols].mean(axis=1)
    base["volatility_score"] = base.groupby("date")["volatility_60d"].transform(
        lambda series: safe_rank_pct(series, ascending=False)
    )
    base["liquidity_score"] = base.groupby("date")["trading_value_ma20"].transform(safe_rank_pct)

    volatility_risk = base.groupby("date")["volatility_60d"].transform(safe_rank_pct)
    illiquidity_risk = 1.0 - base["liquidity_score"]
    overbought_risk = ((base["rsi_14"] - 70.0) / 30.0).clip(lower=0.0, upper=1.0)
    base["risk_score"] = pd.concat(
        [volatility_risk, illiquidity_risk, overbought_risk], axis=1
    ).mean(axis=1)

    base = base[["date", "symbol", *FEATURE_COLUMNS]]
    horizon_frames: list[pd.DataFrame] = []
    for horizon, days in horizons.items():
        frame = base.copy()
        frame["horizon"] = horizon
        frame["horizon_days"] = int(days)
        horizon_frames.append(frame)

    features = pd.concat(horizon_frames, ignore_index=True)
    return features[["date", "symbol", "horizon", "horizon_days", *FEATURE_COLUMNS]].sort_values(
        ["date", "symbol", "horizon"]
    )


def _prepare_prices(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "symbol", "close", "adj_close", "volume", "trading_value"}
    missing = required.difference(prices.columns)
    if missing:
        raise ValueError(f"prices is missing required columns: {sorted(missing)}")

    frame = prices.copy()
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    for column in ("close", "adj_close", "volume", "trading_value"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["feature_close"] = frame["adj_close"].where(frame["adj_close"].notna(), frame["close"])
    frame = frame.dropna(subset=["date", "symbol", "feature_close"])
    frame = frame[frame["feature_close"] > 0]
    return frame.sort_values(["symbol", "date"]).reset_index(drop=True)


def _rsi_14(close: pd.Series) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).rolling(14, min_periods=14).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + relative_strength))
    return rsi.fillna(50.0)

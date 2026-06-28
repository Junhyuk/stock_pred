from __future__ import annotations

import numpy as np
import pandas as pd

from roboquant.features.price_features import FEATURE_COLUMNS, compute_price_features


def test_price_features_do_not_change_when_future_prices_change() -> None:
    dates = pd.date_range("2022-01-01", periods=320, freq="B")
    prices = _prices_for_symbol("000001", dates, 100 + np.arange(len(dates), dtype=float))
    features_before = compute_price_features(prices, {"3M": 63})

    asof_date = dates[180].date()
    mutated = prices.copy()
    mutated.loc[pd.to_datetime(mutated["date"]) > pd.Timestamp(asof_date), "close"] *= 10
    mutated.loc[pd.to_datetime(mutated["date"]) > pd.Timestamp(asof_date), "adj_close"] *= 10
    features_after = compute_price_features(mutated, {"3M": 63})

    before_row = _feature_row(features_before, asof_date)
    after_row = _feature_row(features_after, asof_date)
    pd.testing.assert_series_equal(
        before_row[FEATURE_COLUMNS],
        after_row[FEATURE_COLUMNS],
        check_names=False,
    )


def _feature_row(features: pd.DataFrame, asof_date) -> pd.Series:
    return features[(features["date"] == asof_date) & (features["symbol"] == "000001")].iloc[0]


def _prices_for_symbol(symbol: str, dates: pd.DatetimeIndex, close: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates.date,
            "symbol": symbol,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "adj_close": close,
            "volume": 100_000,
            "trading_value": close * 100_000,
            "market_cap": np.nan,
            "collected_at": pd.Timestamp("2024-01-01"),
        }
    )


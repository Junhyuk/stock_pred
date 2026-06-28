from __future__ import annotations

import numpy as np
import pandas as pd

from roboquant.labels.make_labels import compute_labels


def test_bottom20_label_is_cross_sectional_by_date() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    prices = pd.concat(
        [
            _prices("000001", dates, [100, 120, 120]),
            _prices("000002", dates, [100, 110, 110]),
            _prices("000003", dates, [100, 105, 105]),
            _prices("000004", dates, [100, 95, 95]),
            _prices("000005", dates, [100, 80, 80]),
        ],
        ignore_index=True,
    )

    labels = compute_labels(prices, None, {"1D": 1})
    first_day = labels[labels["asof_date"] == dates[0].date()]

    assert first_day["is_bottom20pct"].sum() == 1
    assert first_day.sort_values("rank_quantile", ascending=True).iloc[0]["symbol"] == "000005"
    assert first_day[first_day["symbol"] == "000005"].iloc[0]["is_bottom20pct"]


def test_top_and_bottom_labels_do_not_overlap_when_universe_is_large_enough() -> None:
    dates = pd.date_range("2024-01-02", periods=3, freq="B")
    prices = pd.concat(
        [_prices(f"{idx:06d}", dates, [100, 100 + idx, 100 + idx]) for idx in range(1, 11)],
        ignore_index=True,
    )

    labels = compute_labels(prices, None, {"1D": 1})
    first_day = labels[labels["asof_date"] == dates[0].date()]
    overlap = first_day[first_day["is_top20pct"] & first_day["is_bottom20pct"]]

    assert overlap.empty
    assert first_day["is_top20pct"].sum() == 2
    assert first_day["is_bottom20pct"].sum() == 2


def _prices(symbol: str, dates: pd.DatetimeIndex, close: list[float]) -> pd.DataFrame:
    close_array = np.asarray(close, dtype=float)
    return pd.DataFrame(
        {
            "date": dates.date,
            "symbol": symbol,
            "open": close_array,
            "high": close_array,
            "low": close_array,
            "close": close_array,
            "adj_close": close_array,
            "volume": 1000,
            "trading_value": close_array * 1000,
            "market_cap": np.nan,
            "collected_at": pd.Timestamp("2024-01-01"),
        }
    )

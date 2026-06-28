from __future__ import annotations

import numpy as np
import pandas as pd

from roboquant.labels.make_labels import compute_labels


def test_labels_use_expected_future_window() -> None:
    dates = pd.date_range("2022-01-03", periods=6, freq="B")
    prices = pd.concat(
        [
            _prices("000001", dates, [100, 80, 120, 130, 125, 140]),
            _prices("000002", dates, [100, 105, 110, 90, 95, 100]),
        ],
        ignore_index=True,
    )
    benchmark = pd.DataFrame(
        {
            "date": dates.date,
            "benchmark": "TEST",
            "close": [100, 101, 102, 103, 104, 105],
        }
    )

    labels = compute_labels(prices, benchmark, {"2D": 2})
    row = labels[(labels["symbol"] == "000001") & (labels["asof_date"] == dates[0].date())].iloc[0]

    assert np.isclose(row["future_return"], 0.20)
    assert np.isclose(row["benchmark_return"], 0.02)
    assert np.isclose(row["excess_return"], 0.18)
    assert np.isclose(row["max_drawdown_forward"], -0.20)


def test_top20_label_is_cross_sectional_by_date() -> None:
    dates = pd.date_range("2022-01-03", periods=4, freq="B")
    prices = pd.concat(
        [
            _prices("000001", dates, [100, 120, 121, 122]),
            _prices("000002", dates, [100, 90, 91, 92]),
            _prices("000003", dates, [100, 95, 96, 97]),
            _prices("000004", dates, [100, 94, 95, 96]),
            _prices("000005", dates, [100, 93, 94, 95]),
        ],
        ignore_index=True,
    )
    labels = compute_labels(prices, None, {"1D": 1})
    first_day = labels[labels["asof_date"] == dates[0].date()]

    assert first_day["is_top20pct"].sum() == 1
    assert first_day.sort_values("rank_quantile", ascending=False).iloc[0]["symbol"] == "000001"


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


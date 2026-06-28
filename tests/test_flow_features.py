from __future__ import annotations

import pandas as pd

from roboquant.features.flow_features import compute_flow_features


def test_flow_features_do_not_change_when_future_flows_change() -> None:
    dates = pd.date_range("2024-01-01", periods=80, freq="B")
    flows = _flows(dates)
    prices = _prices(dates)
    metrics = _metrics(dates)

    before = compute_flow_features(flows, prices=prices, market_metrics=metrics)
    asof_date = dates[40].date()

    mutated = flows.copy()
    mutated.loc[pd.to_datetime(mutated["date"]) > pd.Timestamp(asof_date), "foreign_net_value"] *= 100
    mutated.loc[pd.to_datetime(mutated["date"]) > pd.Timestamp(asof_date), "institution_net_value"] *= -100
    after = compute_flow_features(mutated, prices=prices, market_metrics=metrics)

    before_row = before[(before["date"] == asof_date) & (before["symbol"] == "000001")].iloc[0]
    after_row = after[(after["date"] == asof_date) & (after["symbol"] == "000001")].iloc[0]

    assert before_row["foreign_net_value_20d_sum"] == after_row["foreign_net_value_20d_sum"]
    assert before_row["institution_net_value_20d_sum"] == after_row["institution_net_value_20d_sum"]
    assert before_row["supply_demand_score"] == after_row["supply_demand_score"]


def _flows(dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates.date,
            "symbol": "000001",
            "foreign_net_value": range(len(dates)),
            "institution_net_value": range(len(dates), 0, -1),
            "retail_net_value": 100,
        }
    )


def _prices(dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates.date,
            "symbol": "000001",
            "trading_value": 2_000_000_000,
        }
    )


def _metrics(dates: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates.date,
            "symbol": "000001",
            "market_cap": 10_000_000_000,
        }
    )


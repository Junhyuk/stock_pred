from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd

from roboquant.data.collectors.market_investor_flows import (
    combine_trading_value_by_date_panels,
    kospi_futures_investor_source_status,
)
from roboquant.market_foreign_flow_history import (
    SEQUENTIAL_STEPS,
    attach_actual_returns_to_forecasts,
    build_foreign_pressure_features,
    run_sequential7_foreign_flow_review,
)


def test_sequential7_review_is_not_ready_without_foreign_history() -> None:
    report = run_sequential7_foreign_flow_review(
        cash_flows=pd.DataFrame(),
        returns=_returns(80),
        forecasts=pd.DataFrame(),
        inventory={"market_investor_flows_daily_rows": 0},
    )

    assert [step["key"] for step in report["steps"]] == list(SEQUENTIAL_STEPS)
    assert report["production_weight"] == 0.0
    assert report["overall_status"] == "not_ready"
    assert report["cash_feature_rows"] == 0
    assert report["lead_lag"]["n"] == 0
    assert report["lead_lag"]["status"] == "not_ready"
    futures = next(step for step in report["steps"] if step["key"] == "futures_foreign_match")
    assert futures["status"] == "blocked_missing_official_source"
    assert futures["detail"]["same_sign_hit"] is None


def test_buy_sell_pressure_uses_next_session_not_same_day() -> None:
    dates = pd.bdate_range("2024-01-02", periods=90)
    rng = np.random.default_rng(7)
    sign = rng.choice([-1.0, 1.0], size=len(dates))
    buy = np.where(sign > 0, 8.0e11, 1.0e11)
    sell = np.where(sign > 0, 1.0e11, 8.0e11)
    flows = _cash_flows(dates, buy=buy, sell=sell)
    returns = _returns(len(dates), dates=dates, values=0.012 * sign)

    features = build_foreign_pressure_features(flows, returns)
    assert features.iloc[0]["available_for_prediction_date"] == dates[1].date()
    assert features.iloc[-1]["available_for_prediction_date"] is None

    report = run_sequential7_foreign_flow_review(cash_flows=flows, returns=returns, futures_flows=None)
    assert report["lead_lag"]["n"] >= 60
    assert report["lead_lag"]["contemporaneous_spearman"] > 0.8
    assert abs(report["lead_lag"]["predictive_spearman"] or 0.0) < 0.35
    assert report["production_weight"] == 0.0


def test_signed_pressure_downweights_high_churn_balanced_flow() -> None:
    dates = pd.bdate_range("2024-03-01", periods=4)
    flows = _cash_flows(
        dates,
        buy=[1.0e11, 9.0e11, 2.0e11, 2.0e11],
        sell=[1.0e10, 8.9e11, 1.0e11, 1.0e11],
    )
    features = build_foreign_pressure_features(flows)
    directional = float(features.loc[features["trade_date"] == dates[0].date(), "foreign_signed_pressure"].iloc[0])
    churn = float(features.loc[features["trade_date"] == dates[1].date(), "foreign_signed_pressure"].iloc[0])
    assert directional > churn
    assert features.loc[features["trade_date"] == dates[1].date(), "foreign_imbalance"].iloc[0] < 0.05


def test_forecast_overlay_scores_residuals_without_production_weight() -> None:
    dates = pd.bdate_range("2024-01-02", periods=100)
    signal = np.sin(np.arange(len(dates)) / 4.0)
    flows = _cash_flows(dates, buy=4.0e11 + 3.0e11 * np.clip(signal, 0, None), sell=4.0e11 + 3.0e11 * np.clip(-signal, 0, None))
    next_returns = np.roll(0.008 * np.sign(signal), 1)
    next_returns[0] = 0.0
    returns = _returns(len(dates), dates=dates, values=next_returns)
    forecasts = pd.DataFrame(
        {
            "prediction_date": dates[1:].date,
            "market": "KOSPI",
            "expected_return": 0.001 * np.cos(np.arange(len(dates) - 1)),
            "actual_return": next_returns[1:],
        }
    )
    report = run_sequential7_foreign_flow_review(
        cash_flows=flows,
        returns=returns,
        forecasts=forecasts,
    )
    assert report["overlay"]["n"] >= 60
    assert report["overlay"]["flow_direction_hit"] > report["overlay"]["forecast_direction_hit"]
    assert report["production_weight"] == 0.0
    assert report["overall_status"] in {"accepted_shadow", "rejected", "not_ready"}


def test_futures_confirmation_same_sign_beats_opposite_when_both_series_exist() -> None:
    dates = pd.bdate_range("2024-01-02", periods=80)
    cash_sign = np.where(np.arange(len(dates)) < 40, 1.0, -1.0)
    cash = _cash_flows(dates, buy=5.0e11 * (cash_sign > 0) + 1.0e11, sell=5.0e11 * (cash_sign < 0) + 1.0e11)
    futures = _cash_flows(dates, buy=5.0e11 * (cash_sign > 0) + 1.0e11, sell=5.0e11 * (cash_sign < 0) + 1.0e11)
    futures["instrument"] = "KOSPI200_FUTURES"
    next_returns = np.roll(0.01 * cash_sign, 1)
    next_returns[0] = 0.0
    returns = _returns(len(dates), dates=dates, values=next_returns)
    report = run_sequential7_foreign_flow_review(
        cash_flows=cash,
        futures_flows=futures,
        returns=returns,
    )
    assert report["futures"]["status"] in {"ready", "not_ready"}
    assert report["futures"]["same_sign_hit"] is not None
    assert report["futures"]["same_sign_hit"] >= (report["futures"]["opposite_sign_hit"] or 0.0)
    assert report["production_weight"] == 0.0


def test_by_date_panels_map_foreign_total_to_foreign_and_keep_buy_sell() -> None:
    index = pd.to_datetime(["2024-01-02", "2024-01-03"])
    buy = pd.DataFrame({"기관합계": [10, 20], "개인": [30, 40], "외국인합계": [100, 80], "전체": [140, 140]}, index=index)
    sell = pd.DataFrame({"기관합계": [8, 9], "개인": [25, 22], "외국인합계": [40, 90], "전체": [73, 121]}, index=index)
    combined = combine_trading_value_by_date_panels(buy, sell, market="KOSPI", run_id="test", collected_at=datetime(2024, 1, 3))
    foreign = combined[combined["investor"] == "외국인"].sort_values("trade_date")
    assert list(foreign["buy_value"]) == [100, 80]
    assert list(foreign["sell_value"]) == [40, 90]
    assert list(foreign["net_value"]) == [60, -10]
    assert foreign.iloc[0]["trade_date"] == date(2024, 1, 2)
    assert "전체" not in set(combined["investor"])


def test_production_forecasts_can_receive_benchmark_actuals() -> None:
    forecasts = pd.DataFrame(
        {
            "prediction_date": [date(2024, 1, 3)],
            "market": ["KOSPI"],
            "expected_return": [0.01],
        }
    )
    returns = pd.DataFrame(
        {"trade_date": [date(2024, 1, 2), date(2024, 1, 3)], "market": ["KOSPI", "KOSPI"], "actual_return": [0.0, -0.02]}
    )
    attached = attach_actual_returns_to_forecasts(forecasts, returns)
    assert attached.iloc[0]["actual_return"] == -0.02


def test_futures_source_status_does_not_invent_an_endpoint() -> None:
    status = kospi_futures_investor_source_status()
    assert status["status"] == "blocked_missing_official_source"
    assert status["production_weight"] == 0.0
    assert status["source"] is None


def _cash_flows(dates: pd.DatetimeIndex, buy, sell) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_date": dates.date,
            "market": "KOSPI",
            "investor": "외국인",
            "buy_value": buy,
            "sell_value": sell,
            "net_value": np.asarray(buy) - np.asarray(sell),
            "is_final": True,
        }
    )


def _returns(n: int, dates: pd.DatetimeIndex | None = None, values: np.ndarray | None = None) -> pd.DataFrame:
    index = dates if dates is not None else pd.bdate_range("2024-01-02", periods=n)
    series = values if values is not None else np.zeros(len(index))
    return pd.DataFrame({"trade_date": index.date, "market": "KOSPI", "actual_return": series})

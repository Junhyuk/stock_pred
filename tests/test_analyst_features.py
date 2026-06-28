from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from roboquant.features.analyst_features import (
    compute_analyst_report_outcomes,
    compute_analyst_scores,
    compute_target_price_features,
)
from roboquant.features.consensus_features import (
    attach_consensus_features,
    compute_consensus_history,
)


def test_target_price_features_calculate_revision_and_upside() -> None:
    reports = pd.DataFrame(
        {
            "report_id": ["r1"],
            "report_date": ["2024-01-02"],
            "symbol": ["005930"],
            "stock_name": ["삼성전자"],
            "broker_name": ["Test"],
            "analyst_name": ["Kim"],
            "target_price": [120_000],
            "previous_target_price": [100_000],
            "current_price_at_report": [80_000],
        }
    )

    features = compute_target_price_features(reports)

    assert features.iloc[0]["target_change_pct"] == pytest.approx(20.0)
    assert features.iloc[0]["upside_pct_at_report"] == pytest.approx(50.0)
    assert bool(features.iloc[0]["target_upgrade_flag"])
    assert features.iloc[0]["target_upside_score"] > 0.7


def test_analyst_outcomes_use_future_trading_dates() -> None:
    prices = _price_frame("005930", "2023-01-02", periods=520, start_value=100.0, step=0.2)
    reports = pd.DataFrame(
        {
            "report_id": ["r1"],
            "report_date": ["2023-01-03"],
            "symbol": ["005930"],
            "stock_name": ["삼성전자"],
            "broker_name": ["Test"],
            "analyst_name": ["Kim"],
            "target_price": [115.0],
            "current_price_at_report": [100.2],
        }
    )

    outcomes = compute_analyst_report_outcomes(reports, prices)

    assert len(outcomes) == 1
    assert outcomes.iloc[0]["price_3m"] > 100
    assert outcomes.iloc[0]["return_3m"] > 0
    assert bool(outcomes.iloc[0]["target_hit_12m"])
    assert outcomes.iloc[0]["target_hit_days"] > 0


def test_analyst_reliability_rewards_low_error_and_direction_accuracy() -> None:
    prices = _price_frame("005930", "2022-01-03", periods=780, start_value=100.0, step=0.08)
    reports = []
    report_dates = pd.date_range("2022-01-03", periods=5, freq="60D")
    for index, report_date in enumerate(report_dates):
        close = prices[pd.to_datetime(prices["date"]) >= report_date].iloc[0]["close"]
        reports.append(
            {
                "report_id": f"r{index}",
                "report_date": report_date.date(),
                "symbol": "005930",
                "stock_name": "삼성전자",
                "broker_name": "Test",
                "analyst_name": "Kim",
                "target_price": close * 1.12,
                "current_price_at_report": close,
            }
        )
    reports_frame = pd.DataFrame(reports)
    outcomes = compute_analyst_report_outcomes(reports_frame, prices)

    scores = compute_analyst_scores(
        reports_frame,
        outcomes,
        as_of_date="2023-12-31",
        min_reports=5,
        recent_window_days=730,
    )

    assert len(scores) == 1
    assert scores.iloc[0]["direction_accuracy_12m"] == 1.0
    assert scores.iloc[0]["reliability_score"] > 0.5


def test_consensus_features_are_joined_asof_without_future_leakage() -> None:
    reports = pd.DataFrame(
        {
            "report_id": ["r1", "r2"],
            "report_date": ["2024-01-20", "2024-02-10"],
            "symbol": ["005930", "005930"],
            "stock_name": ["삼성전자", "삼성전자"],
            "broker_name": ["Test", "Test"],
            "analyst_name": ["Kim", "Kim"],
            "target_price": [100.0, 120.0],
            "previous_target_price": [90.0, 100.0],
            "current_price_at_report": [80.0, 90.0],
        }
    )
    consensus = compute_consensus_history(reports)
    features = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-01-05", "2024-01-25", "2024-02-20"]).date,
            "symbol": ["005930", "005930", "005930"],
            "horizon": ["3M", "3M", "3M"],
        }
    )

    joined = attach_consensus_features(features, consensus)

    assert joined.iloc[0]["consensus_revision_score"] == 0.5
    assert joined.iloc[1]["target_up_count_30d"] == 1
    assert joined.iloc[2]["target_up_count_30d"] == 2
    assert joined.iloc[2]["consensus_upside_pct"] > 20.0
    assert joined.iloc[2]["consensus_revision_score"] >= joined.iloc[1]["consensus_revision_score"]


def _price_frame(symbol: str, start: str, periods: int, start_value: float, step: float) -> pd.DataFrame:
    dates = pd.date_range(start, periods=periods, freq="B")
    close = start_value + np.arange(periods, dtype=float) * step
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
        }
    )

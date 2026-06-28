from __future__ import annotations

import pandas as pd

from roboquant.long_short import build_long_short_recommendations
from roboquant.models.train import baseline_feature_predictions


def test_long_short_portfolio_uses_distinct_legs_and_liquidity_filter() -> None:
    predictions = pd.DataFrame(
        {
            "asof_date": ["2024-05-31"] * 6,
            "symbol": [f"{idx:06d}" for idx in range(1, 7)],
            "horizon": ["2M"] * 6,
            "pred_return": [0.20, 0.15, 0.05, -0.01, -0.08, -0.12],
            "pred_prob_top20": [0.90, 0.82, 0.60, 0.45, 0.25, 0.15],
            "pred_prob_bottom20": [0.05, 0.10, 0.25, 0.55, 0.78, 0.92],
            "long_score": [0.95, 0.86, 0.65, 0.42, 0.24, 0.10],
            "short_score": [0.08, 0.18, 0.40, 0.62, 0.85, 0.96],
            "pred_risk": [0.2, 0.3, 0.4, 0.5, 0.6, 0.7],
            "confidence": [0.95, 0.86, 0.65, 0.62, 0.85, 0.96],
            "model_version": ["test"] * 6,
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2024-05-31"] * 6,
            "symbol": [f"{idx:06d}" for idx in range(1, 7)],
            "horizon": ["2M"] * 6,
            "trading_value_ma20": [
                3_000_000_000,
                2_000_000_000,
                2_000_000_000,
                2_000_000_000,
                2_000_000_000,
                100_000_000,
            ],
            "liquidity_score": [0.9, 0.8, 0.8, 0.7, 0.7, 0.1],
            "risk_score": [0.2, 0.3, 0.4, 0.5, 0.6, 0.8],
            "momentum_score": [0.9, 0.8, 0.5, 0.3, 0.2, 0.1],
            "rsi_14": [55, 58, 60, 45, 35, 20],
        }
    )
    symbols = pd.DataFrame(
        {
            "symbol": [f"{idx:06d}" for idx in range(1, 7)],
            "name": [f"Stock {idx}" for idx in range(1, 7)],
            "market": ["KOSPI"] * 6,
        }
    )

    recommendations = build_long_short_recommendations(
        predictions,
        features=features,
        symbols=symbols,
        horizon="2M",
        config={
            "long_short": {
                "long_count": 2,
                "short_count": 2,
                "gross_long": 0.5,
                "gross_short": 0.5,
                "min_trading_value_20d": 1_000_000_000,
            }
        },
    )

    long_symbols = set(recommendations[recommendations["side"] == "LONG"]["symbol"])
    short_symbols = set(recommendations[recommendations["side"] == "SHORT"]["symbol"])

    assert long_symbols == {"000001", "000002"}
    assert short_symbols == {"000005", "000004"}
    assert "000006" not in set(recommendations["symbol"])
    assert long_symbols.isdisjoint(short_symbols)
    assert recommendations[recommendations["side"] == "LONG"]["weight"].sum() == 0.5
    assert recommendations[recommendations["side"] == "SHORT"]["weight"].sum() == -0.5


def test_long_short_portfolio_splits_by_market_with_proportional_legs() -> None:
    predictions = pd.DataFrame(
        {
            "asof_date": ["2024-05-31"] * 10,
            "symbol": [f"{idx:06d}" for idx in range(1, 11)],
            "horizon": ["2M"] * 10,
            "pred_return": [0.20, 0.15, 0.10, 0.05, 0.00, 0.20, 0.10, -0.05, -0.10, -0.15],
            "pred_prob_top20": [0.9, 0.8, 0.7, 0.6, 0.5, 0.9, 0.7, 0.4, 0.3, 0.2],
            "pred_prob_bottom20": [0.1, 0.2, 0.3, 0.4, 0.5, 0.1, 0.3, 0.6, 0.7, 0.8],
            "long_score": [0.95, 0.85, 0.75, 0.65, 0.55, 0.92, 0.72, 0.42, 0.32, 0.22],
            "short_score": [0.15, 0.25, 0.35, 0.45, 0.55, 0.12, 0.32, 0.62, 0.72, 0.82],
            "confidence": [0.9] * 10,
            "model_version": ["test"] * 10,
        }
    )
    symbols = pd.DataFrame(
        {
            "symbol": [f"{idx:06d}" for idx in range(1, 11)],
            "name": [f"Stock {idx}" for idx in range(1, 11)],
            "market": ["KOSPI"] * 5 + ["KOSDAQ"] * 5,
        }
    )
    recommendations = build_long_short_recommendations(
        predictions,
        symbols=symbols,
        horizon="2M",
        config={
            "long_short": {
                "long_count": 4,
                "short_count": 4,
                "gross_long": 0.5,
                "gross_short": 0.5,
                "market_split": {"enabled": True, "kospi_target": 30, "kosdaq_target": 20},
            }
        },
    )

    kospi = recommendations[recommendations["market"] == "KOSPI"]
    kosdaq = recommendations[recommendations["market"] == "KOSDAQ"]
    assert len(kospi[kospi["side"] == "LONG"]) == 2
    assert len(kospi[kospi["side"] == "SHORT"]) == 2
    assert len(kosdaq[kosdaq["side"] == "LONG"]) == 2
    assert len(kosdaq[kosdaq["side"] == "SHORT"]) == 2
    assert set(kospi["symbol"]).isdisjoint(set(kosdaq["symbol"]))


def test_baseline_predictions_include_long_short_scores() -> None:
    features = pd.DataFrame(
        {
            "date": ["2024-05-31"] * 3,
            "symbol": ["000001", "000002", "000003"],
            "horizon": ["2M"] * 3,
            "ret_63d": [0.1, -0.1, 0.0],
            "momentum_score": [0.8, 0.2, 0.5],
            "supply_demand_score": [0.7, 0.2, 0.5],
            "value_score": [0.6, 0.4, 0.5],
            "quality_score": [0.6, 0.4, 0.5],
            "liquidity_score": [0.7, 0.7, 0.7],
            "risk_score": [0.2, 0.8, 0.5],
        }
    )

    predictions = baseline_feature_predictions(features, "2M")

    assert {"pred_prob_bottom20", "long_score", "short_score"}.issubset(predictions.columns)
    assert predictions["long_score"].between(0, 1).all()
    assert predictions["short_score"].between(0, 1).all()

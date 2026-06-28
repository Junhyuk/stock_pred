from __future__ import annotations

import pandas as pd

from roboquant.recommend.scorer import build_recommendations, score_predictions


def test_recommendations_filter_low_liquidity_and_rank() -> None:
    predictions = pd.DataFrame(
        {
            "asof_date": ["2024-01-31"] * 3,
            "symbol": ["000001", "000002", "000003"],
            "horizon": ["3M", "3M", "3M"],
            "pred_return": [0.2, 0.5, 0.1],
            "pred_prob_top20": [0.8, 0.9, 0.4],
            "pred_risk": [0.2, 0.1, 0.2],
            "confidence": [0.8, 0.9, 0.6],
            "model_version": ["test"] * 3,
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2024-01-31"] * 3,
            "symbol": ["000001", "000002", "000003"],
            "horizon": ["3M", "3M", "3M"],
            "horizon_days": [63, 63, 63],
            "momentum_score": [0.8, 0.9, 0.2],
            "liquidity_score": [0.8, 0.1, 0.7],
            "risk_score": [0.2, 0.1, 0.2],
            "trading_value_ma20": [2_000_000_000, 100_000_000, 2_000_000_000],
            "rsi_14": [55, 60, 80],
        }
    )

    scored = score_predictions(predictions, features)
    recommendations = build_recommendations(
        scored,
        horizon="3M",
        top_k=2,
        min_trading_value_20d=1_000_000_000,
    )

    assert "000002" not in recommendations["symbol"].tolist()
    assert recommendations.iloc[0]["rank"] == 1
    assert len(recommendations) == 2


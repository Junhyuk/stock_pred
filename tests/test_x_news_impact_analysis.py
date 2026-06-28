from __future__ import annotations

from datetime import date

import pandas as pd

from roboquant.db import connect_database
from roboquant.signals import x_news_impact
from roboquant.signals.x_news_impact import (
    build_x_news_prediction_impact_for_features,
    neutralize_x_news_features,
)


def test_x_news_top20_impact_compares_with_and_without_x_features(tmp_path, monkeypatch) -> None:
    conn = connect_database(tmp_path / "x_impact.duckdb")
    features = pd.DataFrame(
        [
            _feature("000660", 0.0, 0.0),
            _feature("066570", 0.0, 0.0),
            _feature("005930", 2.0, 1.0),
        ]
    )

    def fake_predict(config, frame, horizon, *, suffix):
        score = 0.50 + pd.to_numeric(frame["x_news_count_3d"], errors="coerce").fillna(0.0) * 0.08
        returns = 0.01 + pd.to_numeric(frame["x_news_negative_attention_score"], errors="coerce").fillna(0.0) * -0.02
        return pd.DataFrame(
            {
                "asof_date": frame["date"],
                "symbol": frame["symbol"],
                "horizon": horizon,
                "pred_return": returns,
                "pred_prob_top20": score,
                "pred_prob_bottom20": 1.0 - score,
                "long_score": score,
                "short_score": 1.0 - score,
                "pred_risk": 0.5,
                "confidence": 0.6,
                "model_version": "fixture-model",
            }
        )

    monkeypatch.setattr(x_news_impact, "_predict_stock_features", fake_predict)

    frame = build_x_news_prediction_impact_for_features(conn, {"paths": {"model_dir": str(tmp_path)}}, features, "3M")
    samsung = frame[frame["symbol"].eq("005930")].iloc[0]

    assert samsung["pred_prob_delta"] > 0
    assert samsung["pred_return_delta"] < 0
    assert samsung["rank_with_x"] < samsung["rank_without_x"]
    assert bool(samsung["top20_with_x"]) is True
    assert samsung["impact_level"] in {"medium", "high"}


def test_x_news_top20_impact_empty_without_x_activity(tmp_path, monkeypatch) -> None:
    conn = connect_database(tmp_path / "x_empty.duckdb")
    features = pd.DataFrame([_feature("005930", 0.0, 0.0)])

    frame = build_x_news_prediction_impact_for_features(conn, {"paths": {"model_dir": str(tmp_path)}}, features, "3M")

    assert frame.empty


def test_neutralize_x_news_features_sets_defaults() -> None:
    frame = pd.DataFrame([_feature("005930", 3.0, 1.0)])

    neutral = neutralize_x_news_features(frame)

    assert neutral["x_news_count_3d"].iloc[0] == 0.0
    assert neutral["x_news_negative_attention_score"].iloc[0] == 0.0
    assert neutral["x_news_bias_adjusted_sentiment_score"].iloc[0] == 0.5


def _feature(symbol: str, x_count: float, x_negative_attention: float) -> dict:
    return {
        "date": date(2026, 6, 26),
        "symbol": symbol,
        "horizon": "3M",
        "ret_21d": 0.0,
        "ret_63d": 0.0,
        "risk_score": 0.5,
        "x_news_count_24h": x_count,
        "x_news_count_3d": x_count,
        "x_news_negative_count_3d": x_negative_attention,
        "x_news_negative_attention_score": x_negative_attention,
        "x_news_bias_adjusted_sentiment_score": 0.2 if x_count else 0.5,
    }

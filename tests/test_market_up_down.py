from __future__ import annotations

import pandas as pd

from roboquant.market_up_down import build_market_up_down_recommendations


def test_market_up_down_splits_kospi_and_kosdaq() -> None:
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
    recommendations = build_market_up_down_recommendations(
        predictions,
        symbols=symbols,
        horizon="2M",
        config={
            "market_up_down": {
                "upside_count": 4,
                "downside_count": 4,
                "market_split": {"enabled": True, "kospi_target": 30, "kosdaq_target": 20},
            }
        },
    )

    kospi = recommendations[recommendations["market"] == "KOSPI"]
    kosdaq = recommendations[recommendations["market"] == "KOSDAQ"]
    assert len(kospi[kospi["side"] == "UP"]) == 2
    assert len(kospi[kospi["side"] == "DOWN"]) == 2
    assert len(kosdaq[kosdaq["side"] == "UP"]) == 2
    assert len(kosdaq[kosdaq["side"] == "DOWN"]) == 2
    up_symbols = set(recommendations[recommendations["side"] == "UP"]["symbol"])
    down_symbols = set(recommendations[recommendations["side"] == "DOWN"]["symbol"])
    assert up_symbols.isdisjoint(down_symbols)


def test_market_up_down_uses_independent_market_ranking() -> None:
    predictions = pd.DataFrame(
        {
            "asof_date": ["2024-05-31"] * 6,
            "symbol": ["000001", "000002", "000003", "000004", "000005", "000006"],
            "horizon": ["2M"] * 6,
            "pred_return": [0.01, 0.02, 0.03, 0.50, 0.40, 0.45],
            "pred_prob_top20": [0.5] * 6,
            "pred_prob_bottom20": [0.5] * 6,
            "long_score": [0.10, 0.20, 0.30, 0.95, 0.90, 0.85],
            "short_score": [0.90, 0.80, 0.70, 0.10, 0.20, 0.30],
            "confidence": [0.5] * 6,
            "model_version": ["test"] * 6,
        }
    )
    symbols = pd.DataFrame(
        {
            "symbol": ["000001", "000002", "000003", "000004", "000005", "000006"],
            "name": ["A", "B", "C", "D", "E", "F"],
            "market": ["KOSPI"] * 3 + ["KOSDAQ"] * 3,
        }
    )
    recommendations = build_market_up_down_recommendations(
        predictions,
        symbols=symbols,
        horizon="2M",
        config={
            "market_up_down": {
                "upside_count": 4,
                "downside_count": 4,
                "market_split": {"enabled": True, "kospi_target": 30, "kosdaq_target": 20},
            }
        },
    )
    kosdaq_up = recommendations[
        (recommendations["market"] == "KOSDAQ") & (recommendations["side"] == "UP")
    ]["symbol"].tolist()
    assert kosdaq_up == ["000004", "000005"]


def test_market_up_down_service_empty(tmp_path) -> None:
    from roboquant.dashboard.market_up_down_service import get_latest_market_up_down
    from roboquant.db import connect_database

    conn = connect_database(tmp_path / "api.duckdb")
    payload = get_latest_market_up_down(conn, horizon="2M")
    assert payload["asof_date"] is None
    assert payload["markets"]["KOSPI"]["upside"] == []

from __future__ import annotations

import sys
from datetime import date, datetime
from math import sqrt
from pathlib import Path

import pandas as pd
import pytest

from roboquant.dashboard.price_forecast_service import get_top20_price_forecast
from roboquant.db import append_dedup_table, connect_database


def test_top20_price_forecast_calculates_expected_and_range_prices(tmp_path) -> None:
    conn = connect_database(tmp_path / "top20_price_forecast.duckdb")
    _seed_price_forecast_fixture(conn)

    payload = get_top20_price_forecast(conn, horizons="3M,6M,9M,1Y", limit=3)

    assert payload["status"] == "ready"
    assert payload["horizons"] == ["3M", "6M", "9M", "1Y"]
    assert payload["summary"]["forecast_count"] == 12

    first = next(item for item in payload["items"] if item["symbol"] == "000001")
    three_month = _forecast(first, "3M")
    assert three_month["status"] == "ready"
    assert three_month["expected_price"] == pytest.approx(110.0)
    assert three_month["upside_price"] == pytest.approx(115.0990195)
    assert three_month["downside_price"] == pytest.approx(94.9009805)
    assert three_month["error_band_source"] == "backtest_rmse"
    assert three_month["up_probability"] == pytest.approx(0.70)
    assert three_month["down_probability"] == pytest.approx(0.20)

    six_month = _forecast(first, "6M")
    assert six_month["status"] == "ready"
    assert six_month["expected_price"] == pytest.approx(95.0)
    assert six_month["error_band"] == pytest.approx(0.20 * sqrt(126 / 252))
    assert six_month["error_band_source"] == "volatility_60d"

    assert _forecast(first, "9M")["status"] == "missing_prediction"

    missing_price_item = next(item for item in payload["items"] if item["symbol"] == "000003")
    assert _forecast(missing_price_item, "3M")["status"] == "missing_price"


def test_top20_price_forecast_api_returns_top20_items_and_four_horizons(tmp_path, monkeypatch) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "top20_price_forecast_api.duckdb"
    conn = connect_database(db_path)
    _seed_price_forecast_fixture(conn)
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    client = TestClient(app_main.app)

    response = client.get("/api/recommendations/top20-price-forecast?horizons=3M,6M,9M,1Y&limit=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert len(payload["items"]) == 1
    assert [forecast["horizon"] for forecast in payload["items"][0]["forecasts"]] == ["3M", "6M", "9M", "1Y"]


def _forecast(item: dict, horizon: str) -> dict:
    return next(forecast for forecast in item["forecasts"] if forecast["horizon"] == horizon)


def _seed_price_forecast_fixture(conn) -> None:
    collected_at = datetime(2026, 1, 3, 9, 0)
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            [
                {"symbol": "000001", "name": "Alpha", "market": "KOSPI", "sector": "Tech", "is_active": True, "collected_at": collected_at},
                {"symbol": "000002", "name": "Beta", "market": "KOSPI", "sector": "Auto", "is_active": True, "collected_at": collected_at},
                {"symbol": "000003", "name": "Gamma", "market": "KOSDAQ", "sector": "Bio", "is_active": True, "collected_at": collected_at},
            ]
        ),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "recommendations",
        pd.DataFrame(
            [
                {"asof_date": date(2026, 1, 2), "horizon": "3M", "symbol": "000001", "final_score": 0.95, "rank": 1, "model_version": "fixture"},
                {"asof_date": date(2026, 1, 2), "horizon": "3M", "symbol": "000002", "final_score": 0.90, "rank": 2, "model_version": "fixture"},
                {"asof_date": date(2026, 1, 2), "horizon": "3M", "symbol": "000003", "final_score": 0.85, "rank": 3, "model_version": "fixture"},
            ]
        ),
        ["asof_date", "horizon", "symbol"],
    )
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame(
            [
                {"date": date(2026, 1, 3), "symbol": "000001", "close": 100.0, "volume": 1_000_000.0, "source": "fixture", "collected_at": collected_at},
                {"date": date(2026, 1, 3), "symbol": "000002", "close": 200.0, "volume": 900_000.0, "source": "fixture", "collected_at": collected_at},
            ]
        ),
        ["date", "symbol", "source"],
    )
    append_dedup_table(
        conn,
        "predictions",
        pd.DataFrame(
            [
                {
                    "asof_date": date(2026, 1, 2),
                    "symbol": "000001",
                    "horizon": "3M",
                    "pred_return": 0.10,
                    "pred_prob_top20": 0.70,
                    "pred_prob_bottom20": 0.20,
                    "pred_risk": 0.25,
                    "confidence": 0.80,
                    "model_version": "fixture_3m",
                },
                {
                    "asof_date": date(2026, 1, 2),
                    "symbol": "000001",
                    "horizon": "6M",
                    "pred_return": -0.05,
                    "pred_prob_top20": 0.35,
                    "pred_prob_bottom20": 0.65,
                    "pred_risk": 0.40,
                    "confidence": 0.65,
                    "model_version": "fixture_6m",
                },
                {
                    "asof_date": date(2026, 1, 2),
                    "symbol": "000001",
                    "horizon": "1Y",
                    "pred_return": 0.20,
                    "pred_prob_top20": 0.55,
                    "pred_prob_bottom20": 0.30,
                    "pred_risk": 0.35,
                    "confidence": 0.70,
                    "model_version": "fixture_1y",
                },
                {
                    "asof_date": date(2026, 1, 2),
                    "symbol": "000002",
                    "horizon": "3M",
                    "pred_return": 0.05,
                    "pred_prob_top20": 0.60,
                    "pred_prob_bottom20": 0.25,
                    "pred_risk": 0.20,
                    "confidence": 0.75,
                    "model_version": "fixture_3m",
                },
                {
                    "asof_date": date(2026, 1, 2),
                    "symbol": "000003",
                    "horizon": "3M",
                    "pred_return": 0.02,
                    "pred_prob_top20": 0.52,
                    "pred_prob_bottom20": 0.22,
                    "pred_risk": 0.30,
                    "confidence": 0.60,
                    "model_version": "fixture_3m",
                },
            ]
        ),
        ["asof_date", "symbol", "horizon"],
    )
    append_dedup_table(
        conn,
        "features_daily",
        pd.DataFrame(
            [
                {"date": date(2026, 1, 2), "symbol": "000001", "horizon": "3M", "horizon_days": 63, "volatility_60d": 0.30, "risk_score": 0.21},
                {"date": date(2026, 1, 2), "symbol": "000001", "horizon": "6M", "horizon_days": 126, "volatility_60d": 0.20, "risk_score": 0.33},
                {"date": date(2026, 1, 2), "symbol": "000001", "horizon": "1Y", "horizon_days": 252, "volatility_60d": 0.25, "risk_score": 0.38},
                {"date": date(2026, 1, 2), "symbol": "000002", "horizon": "3M", "horizon_days": 63, "volatility_60d": 0.15, "risk_score": 0.20},
                {"date": date(2026, 1, 2), "symbol": "000003", "horizon": "3M", "horizon_days": 63, "volatility_60d": 0.18, "risk_score": 0.28},
            ]
        ),
        ["date", "symbol", "horizon"],
    )
    append_dedup_table(
        conn,
        "backtest_results",
        pd.DataFrame(
            [
                {
                    "result_id": "fixture-1",
                    "prediction_date": date(2025, 12, 1),
                    "target_date": date(2026, 3, 1),
                    "symbol": "000001",
                    "model_name": "lightgbm",
                    "model_version": "fixture_3m",
                    "horizon": "3M",
                    "horizon_days": 63,
                    "actual_return": 0.14,
                    "predicted_return": 0.10,
                    "rank_no": 1,
                },
                {
                    "result_id": "fixture-2",
                    "prediction_date": date(2025, 12, 1),
                    "target_date": date(2026, 3, 1),
                    "symbol": "000002",
                    "model_name": "lightgbm",
                    "model_version": "fixture_3m",
                    "horizon": "3M",
                    "horizon_days": 63,
                    "actual_return": 0.14,
                    "predicted_return": 0.20,
                    "rank_no": 2,
                },
            ]
        ),
        ["result_id"],
    )

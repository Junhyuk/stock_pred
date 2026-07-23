from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
from fastapi.testclient import TestClient

from roboquant.dashboard.price_gap_service import build_prediction_price_gap
from roboquant.db import append_dedup_table, connect_database


def test_prediction_price_gap_splits_completed_pending_and_calculates_gap(tmp_path) -> None:
    conn = connect_database(tmp_path / "price_gap.duckdb")
    _seed_price_gap_fixture(conn)

    payload = build_prediction_price_gap(
        conn,
        lookback_days=60,
        target_days=2,
        horizon="3M",
        as_of_date="2026-02-15",
    )

    assert payload["status"] == "ready"
    assert payload["summary"]["sample_count"] == 3
    assert payload["summary"]["completed_count"] == 2
    assert payload["summary"]["pending_count"] == 1
    samsung = next(item for item in payload["items"] if item["symbol"] == "005930" and item["status"] == "completed")
    sl = next(item for item in payload["items"] if item["symbol"] == "005850")
    assert round(samsung["actual_return_30d"], 4) == 0.15
    assert round(samsung["return_gap_30d"], 4) == 0.05
    assert sl["status"] == "pending"
    assert sl["actual_return_30d"] is None
    assert sl["direction_hit_latest"] is True
    assert payload["summary"]["mature_sample_count"] == 2
    assert payload["summary"]["mature_direction_accuracy"] == 0.5
    assert payload["summary"]["interim_sample_count"] == 1


def test_prediction_price_gap_api_and_backtest_page(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "price_gap_api.duckdb"
    conn = connect_database(db_path)
    _seed_price_gap_fixture(conn)
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)
    client = TestClient(app_main.app)

    response = client.get("/api/backtest/price-gap?target_days=1&horizon=3M&market=KOSPI&limit=1")

    assert response.status_code == 200
    assert response.json()["summary"]["sample_count"] == 3
    assert response.json()["lookback_days"] == 365
    assert len(response.json()["items"]) == 1
    page = client.get("/backtest")
    assert page.status_code == 200
    assert "horizon 만기 예측 괴리" in page.text
    assert "/api/backtest/price-gap" in page.text
    assert 'id="market"' in page.text
    assert "lookback_days=30" not in page.text


def test_prediction_price_gap_uses_horizon_trading_days_and_market_filter(tmp_path) -> None:
    conn = connect_database(tmp_path / "price_gap_market.duckdb")
    _seed_price_gap_fixture(conn)
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame([{"symbol": "035420", "name": "네이버", "market": "KOSDAQ", "sector": "인터넷"}]),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "predictions",
        pd.DataFrame([_prediction("2026-01-02", "035420", 0.10, 0.60)]),
        ["asof_date", "symbol", "horizon", "model_version"],
    )
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame([_price("2026-01-02", "035420", 100), _price("2026-02-15", "035420", 110)]),
        ["date", "symbol", "source"],
    )

    kospi = build_prediction_price_gap(conn, horizon="3M", market="KOSPI", as_of_date="2026-02-15")
    kosdaq = build_prediction_price_gap(conn, horizon="3M", market="KOSDAQ", as_of_date="2026-02-15")

    assert kospi["target_trading_days"] == 63
    assert kospi["target_days_source"] == "horizon"
    assert kospi["lookback_days"] == 365
    assert kospi["summary"]["sample_count"] == 3
    assert kosdaq["summary"]["sample_count"] == 1
    assert kosdaq["items"][0]["market"] == "KOSDAQ"


def test_prediction_price_gap_all_uses_each_horizon_trading_maturity(tmp_path) -> None:
    conn = connect_database(tmp_path / "price_gap_all_horizons.duckdb")
    dates = pd.bdate_range("2024-01-02", periods=100)
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame([{"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체"}]),
        ["symbol"],
    )
    rows = []
    for horizon in ("3M", "6M"):
        row = _prediction(dates[0].date().isoformat(), "005930", 0.10, 0.80)
        row["horizon"] = horizon
        rows.append(row)
    append_dedup_table(conn, "predictions", pd.DataFrame(rows), ["asof_date", "symbol", "horizon", "model_version"])
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame([_price(value.date().isoformat(), "005930", 100 + index) for index, value in enumerate(dates)]),
        ["date", "symbol", "source"],
    )

    payload = build_prediction_price_gap(
        conn,
        lookback_days=365,
        horizon="all",
        as_of_date=dates[-1].date().isoformat(),
    )
    items = {item["horizon"]: item for item in payload["items"]}

    assert payload["target_trading_days"] is None
    assert payload["target_trading_days_by_horizon"] == {"3M": 63, "6M": 126}
    assert items["3M"]["status"] == "completed"
    assert items["6M"]["status"] == "pending"
    assert items["3M"]["target_trading_days"] == 63
    assert items["6M"]["target_trading_days"] == 126


def _seed_price_gap_fixture(conn) -> None:
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            [
                {"symbol": "005930", "name": "삼성전자", "market": "KOSPI", "sector": "반도체"},
                {"symbol": "000660", "name": "SK하이닉스", "market": "KOSPI", "sector": "반도체"},
                {"symbol": "005850", "name": "에스엘", "market": "KOSPI", "sector": "자동차"},
            ]
        ),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "predictions",
        pd.DataFrame(
            [
                _prediction("2026-01-02", "005930", 0.10, 0.80),
                _prediction("2026-01-02", "000660", 0.10, 0.70),
                _prediction("2026-01-25", "005850", -0.05, 0.40),
            ]
        ),
        ["asof_date", "symbol", "horizon", "model_version"],
    )
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame(
            [
                _price("2026-01-02", "005930", 100),
                _price("2026-02-01", "005930", 112),
                _price("2026-02-15", "005930", 115),
                _price("2026-01-02", "000660", 100),
                _price("2026-02-01", "000660", 90),
                _price("2026-02-15", "000660", 88),
                _price("2026-01-25", "005850", 100),
                _price("2026-02-15", "005850", 95),
            ]
        ),
        ["date", "symbol", "source"],
    )


def _prediction(asof_date: str, symbol: str, pred_return: float, probability: float) -> dict:
    return {
        "asof_date": date.fromisoformat(asof_date),
        "symbol": symbol,
        "horizon": "3M",
        "pred_return": pred_return,
        "pred_prob_top20": probability,
        "pred_risk": 0.3,
        "confidence": 0.7,
        "model_version": "fixture",
    }


def _price(price_date: str, symbol: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(price_date),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close,
        "volume": 1000,
        "trading_value": close * 1000,
        "source": "fixture",
    }

from __future__ import annotations

import pandas as pd

from roboquant.clustering.stock_clusters import build_stock_clusters, persist_stock_clusters
from roboquant.dashboard.dashboard_service import (
    get_focus_stock,
    get_stock_cluster,
    get_stock_detail,
)
from roboquant.data.collectors import krx
from roboquant.db import append_dedup_table, connect_database


def test_fetch_symbols_falls_back_when_pykrx_returns_empty(monkeypatch) -> None:
    class EmptyStock:
        @staticmethod
        def get_market_ticker_list(*args, **kwargs):
            return []

    monkeypatch.setitem(__import__("sys").modules, "pykrx.stock", EmptyStock)
    monkeypatch.setattr(
        krx,
        "_fetch_symbols_fdr",
        lambda markets: pd.DataFrame(
            {
                "symbol": ["005930"],
                "name": ["삼성전자"],
                "market": ["KOSPI"],
                "sector": ["통신 및 방송 장비 제조업"],
            }
        ),
    )

    result = krx.fetch_symbols(["KOSPI"])

    assert result.iloc[0]["symbol"] == "005930"


def test_fdr_price_fallback_adds_trading_value_and_source(monkeypatch) -> None:
    monkeypatch.setattr(
        krx,
        "_fetch_prices_fdr",
        lambda symbol, start, end: pd.DataFrame(
            {
                "date": ["2025-01-02"],
                "open": [50_000],
                "high": [52_000],
                "low": [49_500],
                "close": [51_000],
                "volume": [1_000],
                "trading_value": [51_000_000],
            }
        ),
    )

    frame = krx.fetch_prices("005930", "2025-01-01", "2025-01-03")

    assert frame.iloc[0]["symbol"] == "005930"
    assert frame.iloc[0]["trading_value"] == 51_000_000
    assert frame.iloc[0]["source"] == "finance_data_reader"


def test_kospi_top_universe_keeps_samsung(monkeypatch) -> None:
    listing = pd.DataFrame(
        {
            "Code": [f"{idx:06d}" for idx in range(1, 102)] + ["005930"],
            "Name": [f"종목{idx}" for idx in range(1, 102)] + ["삼성전자"],
            "Market": ["KOSPI"] * 102,
            "Marcap": list(range(10_000, 9_899, -1)) + [1],
        }
    )
    description = pd.DataFrame(
        {
            "Code": listing["Code"],
            "Name": listing["Name"],
            "Industry": ["산업"] * len(listing),
            "Sector": [None] * len(listing),
            "ListingDate": ["2000-01-01"] * len(listing),
        }
    )

    class FakeFdr:
        @staticmethod
        def StockListing(name):
            return description if name == "KRX-DESC" else listing

    monkeypatch.setitem(__import__("sys").modules, "FinanceDataReader", FakeFdr)
    result = krx.fetch_kospi_top_symbols(limit=100, focus_symbol="005930")

    assert len(result) == 100
    assert "005930" in set(result["symbol"])


def test_kospi_top_universe_adds_extra_symbols(monkeypatch) -> None:
    listing = pd.DataFrame(
        {
            "Code": [f"{idx:06d}" for idx in range(1, 102)] + ["005850"],
            "Name": [f"종목{idx}" for idx in range(1, 102)] + ["에스엘"],
            "Market": ["KOSPI"] * 102,
            "Marcap": list(range(10_000, 9_899, -1)) + [1],
        }
    )
    description = pd.DataFrame(
        {
            "Code": listing["Code"],
            "Name": listing["Name"],
            "Industry": ["산업"] * len(listing),
            "Sector": [None] * len(listing),
            "ListingDate": ["2000-01-01"] * len(listing),
        }
    )

    class FakeFdr:
        @staticmethod
        def StockListing(name):
            return description if name == "KRX-DESC" else listing

    monkeypatch.setitem(__import__("sys").modules, "FinanceDataReader", FakeFdr)
    result = krx.fetch_kospi_top_symbols(
        limit=100,
        focus_symbol="000001",
        extra_symbols=["005850"],
    )

    assert len(result) == 101
    assert result[result["symbol"].eq("005850")].iloc[0]["name"] == "에스엘"


def test_clustering_and_samsung_focus_work_outside_top20(tmp_path) -> None:
    conn = connect_database(tmp_path / "samsung.duckdb")
    symbols = [f"{idx:06d}" for idx in range(1, 41)]
    symbols[0] = "005930"
    symbol_frame = pd.DataFrame(
        {
            "symbol": symbols,
            "name": ["삼성전자", *[f"종목{idx}" for idx in range(2, 41)]],
            "market": ["KOSPI"] * 40,
            "sector": ["전자"] * 20 + ["금융"] * 20,
        }
    )
    features = pd.DataFrame(
        {
            "date": ["2025-06-01"] * 40,
            "symbol": symbols,
            "horizon": ["3M"] * 40,
            "horizon_days": [63] * 40,
            "ret_21d": [idx / 100 for idx in range(40)],
            "ret_63d": [idx / 90 for idx in range(40)],
            "ret_126d": [idx / 80 for idx in range(40)],
            "momentum_score": [idx / 40 for idx in range(40)],
            "volatility_60d": [(40 - idx) / 40 for idx in range(40)],
            "liquidity_score": [0.8] * 40,
            "risk_score": [0.4] * 40,
            "market_cap_score": [(40 - idx) / 40 for idx in range(40)],
            "trading_value_ma20": [2_000_000_000] * 40,
        }
    )
    predictions = pd.DataFrame(
        {
            "asof_date": ["2025-06-01"] * 40,
            "symbol": symbols,
            "horizon": ["3M"] * 40,
            "pred_return": [idx / 100 for idx in range(40)],
            "pred_prob_top20": [idx / 40 for idx in range(40)],
            "pred_risk": [0.4] * 40,
            "confidence": [0.7] * 40,
            "model_version": ["demo"] * 40,
        }
    )
    prices = pd.DataFrame(
        {
            "date": ["2025-06-01"],
            "symbol": ["005930"],
            "open": [60_000],
            "high": [62_000],
            "low": [59_000],
            "close": [61_000],
            "adj_close": [61_000],
            "volume": [1_000_000],
            "trading_value": [61_000_000_000],
            "source": ["finance_data_reader"],
        }
    )
    append_dedup_table(conn, "symbols", symbol_frame, ["symbol"])
    append_dedup_table(conn, "features_daily", features, ["date", "symbol", "horizon"])
    append_dedup_table(conn, "predictions", predictions, ["asof_date", "symbol", "horizon", "model_version"])
    append_dedup_table(conn, "prices_daily", prices, ["date", "symbol"])
    assignments, summaries = build_stock_clusters(features, "3M", n_clusters=5, min_symbols=30)
    persist_stock_clusters(conn, assignments, summaries)

    focus = get_focus_stock(conn, "005930", "3M")
    cluster = get_stock_cluster(conn, "005930", "3M")
    detail = get_stock_detail(conn, "005930", "3M")

    assert len(assignments) == 40
    assert assignments["symbol"].nunique() == 40
    assert focus["name"] == "삼성전자"
    assert not focus["is_top20"]
    assert focus["prediction"]["rank"] == 40
    assert focus["latest_price"]["source"] == "finance_data_reader"
    assert cluster["cluster"] is not None
    assert cluster["peers"]
    assert detail["horizon"] == "3M"
    assert detail["latest_price"]["close"] == 61_000
    assert detail["features"]["momentum_score"] == 0.0
    assert len(detail["chart"]) == 1

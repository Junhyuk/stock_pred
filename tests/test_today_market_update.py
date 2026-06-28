from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

from roboquant.dashboard.dashboard_service import (
    build_today_market_snapshot,
    get_latest_news,
    get_today_market_snapshot,
    get_tomorrow_market_snapshot,
    hydrate_today_market_snapshot,
)
from roboquant.db import append_dedup_table, connect_database


def test_today_market_snapshot_handles_partial_data(tmp_path) -> None:
    conn = connect_database(tmp_path / "today.duckdb")
    _seed_partial_today_data(conn)

    snapshot = build_today_market_snapshot(
        conn,
        {"focus_stocks": [{"symbol": "005930", "name": "삼성전자"}]},
    )

    assert snapshot["status"] == "partial_ready"
    assert snapshot["focus_prices"][0]["symbol"] == "005930"
    assert snapshot["news"][0]["title"] == "삼성전자 오늘 뉴스"
    assert snapshot["data_quality"]["components"]["news"] == "ready"


def test_today_market_api_and_page_render(tmp_path, monkeypatch) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app import main as app_main

    db_path = tmp_path / "today_api.duckdb"
    conn = connect_database(db_path)
    _seed_partial_today_data(conn)
    _seed_next_day_market_outlook(conn)
    build_today_market_snapshot(conn, {"focus_stocks": [{"symbol": "005930", "name": "삼성전자"}]})
    conn.close()

    def _test_conn():
        return connect_database(db_path, read_only=True, initialize_schema=False)

    monkeypatch.setattr(app_main, "_conn", _test_conn)

    response = app_main.today_update_snapshot()
    assert response["status"] == "partial_ready"
    assert "market_outlook" in response
    assert "items" in app_main.market_outlook(date="latest", horizon="all", market="all")
    tomorrow = app_main.tomorrow_update_snapshot()
    assert tomorrow["market_outlook"]["target_date"] == "2026-06-29"
    assert tomorrow["market_outlook"]["range_multiplier"] == 1.25
    assert tomorrow["market_outlook"]["summary"]["count"] == 2
    assert tomorrow["long_short_range"]["summary"]["count"] == 2
    assert tomorrow["long_short_range"]["data_quality"]["components"]["credit_balance"] == "missing"
    assert app_main.news_latest(symbol="005930")["items"][0]["title"] == "삼성전자 오늘 뉴스"
    page = app_main.today_market_page()
    html = page.body.decode("utf-8")
    assert page.status_code == 200
    assert "오늘 시장 업데이트" in html
    assert "오늘·이번주 KOSPI/KOSDAQ 전망" in html
    assert "예상 등락률" in html
    assert "상승확률" in html
    assert "-2% 충격확률" in html
    assert "function todayStatusCards" in html
    assert "function renderTodayMarketOutlook" in html
    assert "function formatEvidence" in html
    assert "KOSPI/KOSDAQ -2% 시장충격" in html
    assert "JSON.stringify(ev.value)" not in html
    assert "검증 대기" in html
    assert "데이터 부족" in html
    assert "다음 거래일 예측" in html
    tomorrow_page = app_main.tomorrow_market_page()
    tomorrow_html = tomorrow_page.body.decode("utf-8")
    assert tomorrow_page.status_code == 200
    assert "다음 거래일 시장 예측" in tomorrow_html
    assert "다음 거래일 KOSPI/KOSDAQ 예측" in tomorrow_html
    assert "다음 거래일 숏·롱 범위" in tomorrow_html
    assert "function bootTomorrowMarket" in tomorrow_html
    assert "function renderTomorrowMarketOutlook" in tomorrow_html
    assert "function renderTomorrowLongShortRange" in tomorrow_html


def test_tomorrow_market_snapshot_wraps_next_day_outlook_with_wider_display_range(tmp_path) -> None:
    conn = connect_database(tmp_path / "tomorrow_snapshot.duckdb")
    _seed_partial_today_data(conn)
    _seed_next_day_market_outlook(conn)

    snapshot = get_tomorrow_market_snapshot(
        conn,
        {
            "focus_stocks": [{"symbol": "005930", "name": "삼성전자"}],
            "market_outlook": {
                "next_day_range_multiplier": 1.25,
                "use_pykrx_calendar": False,
                "krx_holidays": [],
            },
        },
    )

    outlook = snapshot["market_outlook"]
    assert outlook["horizon"] == "NEXT_TRADING_DAY"
    assert outlook["source_horizon"] == "TODAY"
    assert outlook["target_date"] == "2026-06-29"
    assert outlook["summary"]["count"] == 2
    kospi = next(item for item in outlook["items"] if item["market"] == "KOSPI")
    assert kospi["horizon"] == "NEXT_TRADING_DAY"
    assert kospi["source_horizon"] == "TODAY"
    assert kospi["source_range_low"] == -0.01
    assert kospi["source_range_high"] == 0.03
    assert round(kospi["range_low"], 4) == -0.015
    assert round(kospi["range_high"], 4) == 0.035
    raw = conn.execute(
        """
        SELECT range_low, range_high
        FROM market_outlook_forecasts
        WHERE asof_date = DATE '2026-06-26'
          AND horizon = 'TODAY'
          AND market = 'KOSPI'
        """
    ).fetchone()
    assert raw == (-0.01, 0.03)


def test_tomorrow_long_short_range_uses_credit_pressure_without_mutating_outlook(tmp_path) -> None:
    conn = connect_database(tmp_path / "tomorrow_long_short_range.duckdb")
    _seed_partial_today_data(conn)
    _seed_next_day_market_outlook(conn)
    _seed_market_credit_balance(conn)

    snapshot = get_tomorrow_market_snapshot(
        conn,
        {
            "focus_stocks": [{"symbol": "005930", "name": "삼성전자"}],
            "market_outlook": {"use_pykrx_calendar": False},
        },
    )

    payload = snapshot["long_short_range"]
    assert payload["status"] == "ready"
    assert payload["summary"]["count"] == 2
    kospi = next(item for item in payload["items"] if item["market"] == "KOSPI")
    kosdaq = next(item for item in payload["items"] if item["market"] == "KOSDAQ")
    assert kospi["long_center"] > 0.5
    assert kosdaq["long_center"] < 0.5
    assert kosdaq["short_high"] > kosdaq["long_high"]
    assert kosdaq["credit_pressure_score"] > 0.5
    raw = conn.execute(
        """
        SELECT range_low, range_high
        FROM market_outlook_forecasts
        WHERE asof_date = DATE '2026-06-26'
          AND horizon = 'TODAY'
          AND market = 'KOSDAQ'
        """
    ).fetchone()
    assert raw == (-0.025, 0.015)


def test_get_latest_news_returns_empty_before_schema_exists(tmp_path) -> None:
    conn = connect_database(tmp_path / "empty.duckdb")
    assert get_latest_news(conn, symbol="005930") == []


def test_today_market_snapshot_hydrates_live_regime_and_context(tmp_path) -> None:
    conn = connect_database(tmp_path / "hydrate.duckdb")
    _seed_partial_today_data(conn)
    build_today_market_snapshot(conn, {"focus_stocks": [{"symbol": "005930", "name": "삼성전자"}]})
    conn.execute(
        """
        INSERT INTO market_regime_daily (
            prediction_date, prediction_cutoff, us_equity_score, semiconductor_score,
            asia_score, volatility_score, rate_score, fx_score, commodity_score, global_risk_score,
            regime, recommended_cash_ratio, signals_json, reasons_json, feature_version, futures_score
        )
        VALUES (
            DATE '2026-06-10', TIMESTAMP '2026-06-10 08:00:00',
            0, 0, 0, 0, 0, 0, 0, 10,
            'risk_on', 0.05,
            '{"nasdaq_futures_return_snapshot": -0.011}',
            '["Nasdaq futures -1% 이하"]',
            'domestic_plus_global_regime_v1',
            10
        )
        """
    )
    conn.execute(
        """
        INSERT INTO global_market_daily (
            trade_date, symbol, market_group, display_name, close, return_1d, return_5d, source_name
        )
        VALUES
            (DATE '2026-06-09', '^IXIC', 'US_INDEX', 'Nasdaq', 100.0, -0.0097, -0.02, 'fixture'),
            (DATE '2026-06-09', '^GSPC', 'US_INDEX', 'S&P500', 200.0, -0.0026, -0.01, 'fixture')
        """
    )

    snapshot = get_today_market_snapshot(
        conn,
        {"focus_stocks": [{"symbol": "005930", "name": "삼성전자", "yahoo_symbol": "005930.KS", "currency": "KRW"}]},
    )

    assert snapshot["global_regime"]["status"] == "ready"
    assert snapshot["global_regime"]["regime"] == "risk_on"
    assert snapshot["news"][0]["title"] == "삼성전자 오늘 뉴스"
    assert snapshot["market_context"] == []
    assert snapshot["focus_prices"][0]["status"] == "ready"
    assert any(item["yahoo_symbol"] == "005930.KS" for item in snapshot["yahoo_prices"])


def test_today_market_snapshot_builds_market_context_without_news(tmp_path) -> None:
    conn = connect_database(tmp_path / "context.duckdb")
    _seed_partial_today_data(conn, include_news=False)
    conn.execute("DELETE FROM news_articles")
    conn.execute(
        """
        INSERT INTO market_regime_daily (
            prediction_date, prediction_cutoff, us_equity_score, semiconductor_score,
            asia_score, volatility_score, rate_score, fx_score, commodity_score, global_risk_score,
            regime, recommended_cash_ratio, signals_json, reasons_json, feature_version, futures_score
        )
        VALUES (
            DATE '2026-06-10', TIMESTAMP '2026-06-10 08:00:00',
            0, 0, 0, 0, 0, 0, 0, 10,
            'risk_on', 0.05,
            '{"nasdaq_futures_return_snapshot": -0.011}',
            '["Nasdaq futures -1% 이하"]',
            'domestic_plus_global_regime_v1',
            10
        )
        """
    )
    conn.execute(
        """
        INSERT INTO global_market_daily (
            trade_date, symbol, market_group, display_name, close, return_1d, return_5d, source_name
        )
        VALUES (DATE '2026-06-09', '^IXIC', 'US_INDEX', 'Nasdaq', 100.0, -0.0097, -0.02, 'fixture')
        """
    )

    snapshot = hydrate_today_market_snapshot(
        conn,
        {"snapshot_date": "2026-06-10", "status": "partial_ready", "disclaimer": "test"},
        {"focus_stocks": [{"symbol": "005930", "name": "삼성전자"}]},
    )

    assert snapshot["news"] == []
    assert any(item["kind"] == "regime" for item in snapshot["market_context"])
    assert any("Nasdaq" in item["title"] for item in snapshot["market_context"])


def test_today_market_snapshot_replaces_stale_yahoo_with_fallback(tmp_path) -> None:
    conn = connect_database(tmp_path / "stale_yahoo.duckdb")
    _seed_partial_today_data(conn)
    _seed_yahoo_price(conn, "005930.KS", "005930", date(2026, 6, 1), 65000.0)
    conn.execute(
        """
        INSERT INTO global_market_daily (
            trade_date, symbol, market_group, display_name, close, return_1d, source_name
        )
        VALUES (DATE '2026-06-10', 'SPY', 'US_ETF', 'SPY', 500.0, 0.01, 'fixture')
        """
    )

    snapshot = hydrate_today_market_snapshot(
        conn,
        {"snapshot_date": "2026-06-10", "status": "partial_ready", "disclaimer": "test"},
        {
            "focus_stocks": [{"symbol": "005930", "name": "삼성전자", "yahoo_symbol": "005930.KS", "currency": "KRW"}],
            "freshness": {"max_yahoo_age_days": 3},
        },
    )

    assert snapshot["data_quality"]["components"]["yahoo_prices"] == "stale"
    assert any("Yahoo/yfinance 원본 가격이 오래" in message for message in snapshot["data_quality"]["messages"])
    assert any(item["source"] == "domestic_prices_daily" for item in snapshot["yahoo_prices"])
    assert any(item["source"] == "global_market_daily" for item in snapshot["yahoo_prices"])
    assert not any(item["source"] == "yahoo_unofficial" for item in snapshot["yahoo_prices"])


def test_today_market_snapshot_uses_fresh_yahoo_prices(tmp_path) -> None:
    conn = connect_database(tmp_path / "fresh_yahoo.duckdb")
    _seed_partial_today_data(conn)
    _seed_yahoo_price(conn, "005930.KS", "005930", date(2026, 6, 10), 70500.0)

    snapshot = hydrate_today_market_snapshot(
        conn,
        {"snapshot_date": "2026-06-10", "status": "partial_ready", "disclaimer": "test"},
        {
            "focus_stocks": [{"symbol": "005930", "name": "삼성전자", "yahoo_symbol": "005930.KS", "currency": "KRW"}],
            "freshness": {"max_yahoo_age_days": 3},
        },
    )

    assert snapshot["data_quality"]["components"]["yahoo_prices"] == "ready"
    assert snapshot["yahoo_prices"][0]["source"] == "yahoo_unofficial"
    assert snapshot["yahoo_prices"][0]["close"] == 70500.0


def test_today_market_snapshot_flags_stale_macro_news_and_missing_supply(tmp_path) -> None:
    conn = connect_database(tmp_path / "macro_supply.duckdb")
    _seed_partial_today_data(conn, include_news=False)
    conn.execute("DELETE FROM news_articles")
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "old-flow-news",
                    "source": "fixture",
                    "category": "flow",
                    "title": "오래된 수급 뉴스",
                    "summary": "fixture",
                    "link": "https://example.com/flow",
                    "pub_date": datetime(2026, 6, 25, 9),
                    "tickers_json": "[]",
                    "themes_json": '["FLOW"]',
                    "sentiment_score": 0.5,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 25, 9),
                }
            ]
        ),
        ["article_id"],
    )
    append_dedup_table(
        conn,
        "collection_failures",
        pd.DataFrame(
            [
                {
                    "collected_at": datetime(2026, 6, 10, 9),
                    "step": "collect_investor_flows",
                    "source": "pykrx",
                    "symbol": None,
                    "target_date": "2026-06-10",
                    "error_message": "fixture pykrx failure",
                    "retry_count": 0,
                }
            ]
        ),
        ["collected_at", "step", "source", "error_message"],
    )

    snapshot = hydrate_today_market_snapshot(
        conn,
        {"snapshot_date": "2026-06-10", "status": "partial_ready", "disclaimer": "test"},
        {"focus_stocks": [{"symbol": "005930", "name": "삼성전자"}], "freshness": {"max_macro_news_age_hours": 1}},
    )

    assert snapshot["data_quality"]["components"]["news"] == "stale"
    assert snapshot["data_quality"]["components"]["supply_flows"] == "missing"
    assert any("거시·수급 뉴스" in message for message in snapshot["data_quality"]["messages"])
    assert any("fixture pykrx failure" in message for message in snapshot["data_quality"]["messages"])


def _seed_yahoo_price(conn, yahoo_symbol: str, symbol: str, price_date: date, close: float) -> None:
    append_dedup_table(
        conn,
        "yahoo_prices_daily",
        pd.DataFrame(
            [
                {
                    "date": price_date,
                    "symbol": symbol,
                    "yahoo_symbol": yahoo_symbol,
                    "asset_type": "stock",
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "adj_close": close,
                    "volume": 1000.0,
                    "currency": "KRW",
                    "source_timestamp": datetime.combine(price_date, datetime.min.time()),
                    "collected_at": datetime(2026, 6, 10, 9),
                }
            ]
        ),
        ["date", "yahoo_symbol"],
    )


def _seed_partial_today_data(conn, *, include_news: bool = True) -> None:
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "market": "KOSPI",
                    "sector": "반도체",
                    "is_active": True,
                }
            ]
        ),
        ["symbol"],
    )
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame(
            [
                {
                    "date": date(2026, 6, 10),
                    "symbol": "005930",
                    "close": 70000.0,
                    "volume": 1000.0,
                    "trading_value": 70000000.0,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 10, 15),
                }
            ]
        ),
        ["date", "symbol", "source"],
    )
    if not include_news:
        return
    append_dedup_table(
        conn,
        "news_articles",
        pd.DataFrame(
            [
                {
                    "article_id": "fixture-news-1",
                    "collected_at": datetime(2026, 6, 10, 9),
                    "query_date": date(2026, 6, 10),
                    "symbol": "005930",
                    "name": "삼성전자",
                    "query": "삼성전자 주가",
                    "title": "삼성전자 오늘 뉴스",
                    "description": "요약",
                    "originallink": "https://example.com/news",
                    "link": "https://news.naver.com/news",
                    "pub_date": datetime(2026, 6, 10, 8),
                    "source_name": "naver_search_api",
                    "raw_json": "{}",
                }
            ]
        ),
        ["article_id"],
    )


def _seed_next_day_market_outlook(conn) -> None:
    append_dedup_table(
        conn,
        "market_outlook_forecasts",
        pd.DataFrame(
            [
                {
                    "asof_date": date(2026, 6, 26),
                    "target_date": date(2026, 6, 29),
                    "horizon": "TODAY",
                    "market": "KOSPI",
                    "expected_return": 0.01,
                    "range_low": -0.01,
                    "range_high": 0.03,
                    "up_probability": 0.62,
                    "down_probability": 0.38,
                    "shock_probability": 0.12,
                    "direction": "BULLISH",
                    "confidence": 0.71,
                    "drivers_json": '[{"kind":"index_model","label":"지수 직접 모델","value":{"index_expected_return":0.01}}]',
                    "data_quality_json": '{"status":"ready","components":{"benchmark":"ready"},"messages":[]}',
                    "model_version": "fixture",
                    "created_at": datetime(2026, 6, 26, 16),
                },
                {
                    "asof_date": date(2026, 6, 26),
                    "target_date": date(2026, 6, 29),
                    "horizon": "TODAY",
                    "market": "KOSDAQ",
                    "expected_return": -0.005,
                    "range_low": -0.025,
                    "range_high": 0.015,
                    "up_probability": 0.44,
                    "down_probability": 0.56,
                    "shock_probability": 0.22,
                    "direction": "BEARISH",
                    "confidence": 0.65,
                    "drivers_json": '[{"kind":"breadth","label":"Top50 breadth 모델","value":{"breadth_expected_return":-0.005}}]',
                    "data_quality_json": '{"status":"ready","components":{"benchmark":"ready"},"messages":[]}',
                    "model_version": "fixture",
                    "created_at": datetime(2026, 6, 26, 16),
                },
            ]
        ),
        ["asof_date", "target_date", "horizon", "market", "model_version"],
    )


def _seed_market_credit_balance(conn) -> None:
    append_dedup_table(
        conn,
        "market_credit_balance_daily",
        pd.DataFrame(
            [
                {
                    "date": date(2026, 6, 26),
                    "market": "KOSPI",
                    "credit_loan_balance_krw": 1000.0,
                    "credit_loan_delta_1d_krw": 0.0,
                    "credit_loan_delta_5d_krw": 0.0,
                    "credit_loan_delta_20d_krw": 0.0,
                    "credit_to_market_cap": 0.01,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 26, 16),
                },
                {
                    "date": date(2026, 6, 26),
                    "market": "KOSDAQ",
                    "credit_loan_balance_krw": 1200.0,
                    "credit_loan_delta_1d_krw": 20.0,
                    "credit_loan_delta_5d_krw": 120.0,
                    "credit_loan_delta_20d_krw": 200.0,
                    "credit_to_market_cap": 0.03,
                    "source": "fixture",
                    "collected_at": datetime(2026, 6, 26, 16),
                },
            ]
        ),
        ["date", "market", "source"],
    )

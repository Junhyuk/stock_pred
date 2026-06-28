from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd

from roboquant.db import append_dedup_table, connect_database
from roboquant.features.build_feature_matrix import build_feature_matrix
from roboquant.signals.news_signals import (
    attach_news_signal_features,
    build_news_signal_daily,
)


def test_build_news_signal_daily_from_approved_stored_news(tmp_path) -> None:
    conn = connect_database(tmp_path / "news_signals.duckdb")
    _seed_news(conn)

    frame = build_news_signal_daily(
        conn,
        _news_config(),
        signal_date=date(2026, 6, 10),
        symbols=["005930", "000660"],
    )

    assert {"market", "stock"} <= set(frame["scope"])
    market = frame[frame["scope"].eq("market")].iloc[0]
    samsung = frame[frame["symbol"].eq("005930")].iloc[0]
    assert market["headline_count_3d"] == 1
    assert samsung["headline_count_1d"] == 1
    assert samsung["news_attention_score"] > 0
    assert samsung["news_urgency_score"] >= 0.6
    assert samsung["news_semiconductor_score"] > 0
    assert "SEMICONDUCTOR" in json.loads(samsung["themes_json"])


def test_attach_news_signal_features_uses_stock_then_market_fallback(tmp_path) -> None:
    conn = connect_database(tmp_path / "attach_news_signals.duckdb")
    _seed_news(conn)
    signals = build_news_signal_daily(
        conn,
        _news_config(),
        signal_date=date(2026, 6, 10),
        symbols=["005930", "000660"],
    )
    features = pd.DataFrame(
        [
            {"date": date(2026, 6, 10), "symbol": "005930", "horizon": "3M", "ret_21d": 0.01},
            {"date": date(2026, 6, 10), "symbol": "000660", "horizon": "3M", "ret_21d": 0.02},
        ]
    )

    attached = attach_news_signal_features(features, signals)

    samsung = attached[attached["symbol"].eq("005930")].iloc[0]
    hynix = attached[attached["symbol"].eq("000660")].iloc[0]
    assert samsung["news_urgency_score"] > hynix["news_urgency_score"]
    assert hynix["news_headline_count_3d"] == 1.0
    assert hynix["news_macro_score"] == 1.0


def test_feature_matrix_defaults_news_features_without_news_table() -> None:
    prices = pd.DataFrame(
        [
            _price_row("2026-01-01", "005930", 100.0),
            _price_row("2026-02-01", "005930", 110.0),
            _price_row("2026-03-01", "005930", 120.0),
        ]
    )

    features = build_feature_matrix(prices, {"3M": 63})

    assert "news_attention_score" in features.columns
    assert "x_news_count_3d" in features.columns
    assert features["news_attention_score"].fillna(0).eq(0).all()
    assert features["news_sentiment_score"].fillna(0.5).eq(0.5).all()
    assert features["news_bias_adjusted_sentiment_score"].fillna(0.5).eq(0.5).all()
    assert features["x_news_count_3d"].fillna(0).eq(0).all()
    assert features["x_news_bias_adjusted_sentiment_score"].fillna(0.5).eq(0.5).all()


def test_negative_headline_is_amplified_against_promotional_news(tmp_path) -> None:
    conn = connect_database(tmp_path / "negative_weight.duckdb")
    rows = []
    for index in range(4):
        rows.append(
            {
                "article_id": f"positive-{index}",
                "collected_at": datetime(2026, 6, 10, 9),
                "query_date": date(2026, 6, 10),
                "symbol": "005930",
                "name": "삼성전자",
                "query": "삼성전자 주가",
                "title": "삼성전자 반도체 성장과 마케팅 호조",
                "description": "신제품 수혜와 실적 성장 기대",
                "originallink": f"https://example.com/positive-{index}",
                "link": f"https://news.naver.com/positive-{index}",
                "pub_date": datetime(2026, 6, 10, 8, index),
                "source_name": "naver_search_api",
                "sentiment_score": 0.8,
                "raw_json": "{}",
            }
        )
    rows.append(
        {
            "article_id": "negative-1",
            "collected_at": datetime(2026, 6, 10, 10),
            "query_date": date(2026, 6, 10),
            "symbol": "005930",
            "name": "삼성전자",
            "query": "삼성전자 주가",
            "title": "Samsung Electronics shares fall after downgrade",
            "description": "weak demand and miss risk",
            "originallink": "https://example.com/negative",
            "link": "https://news.naver.com/negative",
            "pub_date": datetime(2026, 6, 10, 10),
            "source_name": "naver_search_api",
            "sentiment_score": 0.2,
            "raw_json": "{}",
        }
    )
    append_dedup_table(conn, "news_articles", pd.DataFrame(rows), ["article_id"])

    frame = build_news_signal_daily(
        conn,
        {
            **_news_config(),
            "news_signals": {
                "negative_weight": 2.0,
                "positive_weight": 0.75,
                "negative_sentiment_threshold": 0.45,
                "positive_sentiment_threshold": 0.60,
            },
        },
        signal_date=date(2026, 6, 10),
        symbols=["005930"],
    )

    samsung = frame[frame["symbol"].eq("005930")].iloc[0]
    assert samsung["headline_count_3d"] == 5
    assert samsung["news_negative_count_3d"] == 1
    assert round(samsung["news_negative_ratio_3d"], 2) == 0.2
    assert samsung["news_negative_attention_score"] > samsung["news_negative_ratio_3d"]
    assert samsung["news_bias_adjusted_sentiment_score"] < samsung["news_sentiment_score"]


def test_overseas_samsung_headline_maps_to_local_symbol(tmp_path) -> None:
    conn = connect_database(tmp_path / "overseas_alias.duckdb")
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "global-samsung-1",
                    "source": "official_global_feed",
                    "category": "sector",
                    "title": "Samsung Electronics shares fall after chip demand miss",
                    "summary": "SK hynix also weak after memory downgrade",
                    "link": "https://example.com/global-samsung",
                    "pub_date": datetime(2026, 6, 10, 0),
                    "tickers_json": "[]",
                    "themes_json": json.dumps(["SEMICONDUCTOR"]),
                    "sentiment_score": 0.25,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 10, 1),
                }
            ]
        ),
        ["article_id"],
    )

    frame = build_news_signal_daily(conn, _news_config(), signal_date=date(2026, 6, 10), symbols=["005930", "000660"])

    assert set(frame["symbol"]) >= {"ALL", "005930", "000660"}
    samsung = frame[frame["symbol"].eq("005930")].iloc[0]
    assert samsung["news_negative_count_3d"] == 1
    assert samsung["news_semiconductor_score"] == 1.0


def test_x_marketnews_feed_negative_post_is_used_as_prediction_news_signal(tmp_path) -> None:
    conn = connect_database(tmp_path / "x_news_signal.duckdb")
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "x-samsung-1",
                    "source": "x_marketnews_feed",
                    "category": "sector",
                    "title": "Samsung Electronics shares fall after downgrade and chip demand miss",
                    "summary": "Memory demand weakens; SK hynix also hit by semiconductor sell-off",
                    "link": "https://x.com/MarketNews_Feed/status/1885000000000000001",
                    "pub_date": datetime(2026, 6, 10, 3),
                    "tickers_json": json.dumps(["005930", "000660"]),
                    "themes_json": json.dumps(["SEMICONDUCTOR"]),
                    "sentiment_score": 0.2,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 10, 3, 1),
                }
            ]
        ),
        ["article_id"],
    )

    frame = build_news_signal_daily(conn, _news_config(), signal_date=date(2026, 6, 10), symbols=["005930", "000660"])

    samsung = frame[frame["symbol"].eq("005930")].iloc[0]
    market = frame[frame["scope"].eq("market")].iloc[0]
    assert samsung["news_negative_count_3d"] == 1
    assert samsung["news_negative_attention_score"] > 0
    assert samsung["news_bias_adjusted_sentiment_score"] < 0.5
    assert samsung["x_news_count_24h"] == 1
    assert samsung["x_news_count_3d"] == 1
    assert samsung["x_news_negative_count_3d"] == 1
    assert samsung["x_news_negative_attention_score"] > 0
    assert samsung["x_news_bias_adjusted_sentiment_score"] < 0.5
    assert market["news_source_diversity_score"] > 0


def _seed_news(conn) -> None:
    append_dedup_table(
        conn,
        "news_articles",
        pd.DataFrame(
            [
                {
                    "article_id": "naver-1",
                    "collected_at": datetime(2026, 6, 10, 9),
                    "query_date": date(2026, 6, 10),
                    "symbol": "005930",
                    "name": "삼성전자",
                    "query": "삼성전자 주가",
                    "title": "삼성전자 반도체 실적 서프라이즈",
                    "description": "HBM 성장과 목표가 상향",
                    "originallink": "https://example.com/original",
                    "link": "https://news.naver.com/example",
                    "pub_date": datetime(2026, 6, 10, 8),
                    "source_name": "naver_search_api",
                    "sentiment_score": None,
                    "raw_json": "{}",
                }
            ]
        ),
        ["article_id"],
    )
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "rss-1",
                    "source": "fed_press",
                    "category": "macro",
                    "title": "Fed rate outlook update",
                    "summary": "interest rate guidance and yield pressure",
                    "link": "https://example.com/fed",
                    "pub_date": datetime(2026, 6, 10, 0),
                    "tickers_json": "[]",
                    "themes_json": json.dumps(["RATE"]),
                    "sentiment_score": 0.4,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 10, 1),
                }
            ]
        ),
        ["article_id"],
    )


def _news_config() -> dict:
    return {
        "text_features": {
            "theme_keywords": {
                "SEMICONDUCTOR": ["반도체", "HBM"],
                "RATE": ["rate", "yield"],
            },
            "positive_words": ["서프라이즈", "상향", "growth"],
            "negative_words": ["pressure"],
            "urgency_keywords": {"high": ["서프라이즈"], "medium": ["목표가"], "low": ["outlook"]},
        }
    }


def _price_row(value_date: str, symbol: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(value_date),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "adj_close": close,
        "volume": 1000.0,
        "trading_value": close * 1000.0,
        "market_cap": close * 1_000_000.0,
        "source": "fixture",
        "collected_at": datetime(2026, 1, 1),
    }

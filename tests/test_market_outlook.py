from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd

from roboquant.dashboard.dashboard_service import get_market_outlook
from roboquant.db import append_dedup_table, connect_database
from roboquant.market_outlook import (
    build_market_outlook_dataset,
    normal_cdf,
    refresh_market_outlook_forecasts,
    target_dates_for_run,
)
from roboquant.signals.x_news_impact import build_x_market_outlook_impact


def test_market_outlook_target_dates_for_20260624_premarket() -> None:
    targets = target_dates_for_run(date(2026, 6, 23), today=date(2026, 6, 24))

    assert targets["TODAY"] == date(2026, 6, 24)
    assert targets["WEEK"] == date(2026, 6, 26)


def test_market_outlook_target_dates_skip_weekend_and_config_holiday() -> None:
    targets = target_dates_for_run(
        date(2026, 6, 26),
        today=date(2026, 6, 27),
        holidays={date(2026, 6, 29)},
    )

    assert targets["TODAY"] == date(2026, 6, 30)
    assert targets["WEEK"] == date(2026, 7, 3)


def test_market_outlook_dataset_uses_asof_or_earlier_features(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_dataset.duckdb")
    _seed_benchmark(conn)

    frame = build_market_outlook_dataset(conn, asof_date="2026-06-23")
    latest = frame[pd.to_datetime(frame["asof_date"]).dt.date.eq(date(2026, 6, 23))]

    assert not latest.empty
    assert pd.to_datetime(latest["feature_cutoff_date"]).dt.date.max() <= date(2026, 6, 23)
    assert set(latest["target_date"].astype(str)) == {"2026-06-24", "2026-06-26"}
    assert latest["label_return"].isna().all()


def test_market_outlook_news_features_use_trailing_window_without_lookahead(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_news_window.duckdb")
    _seed_benchmark(conn)
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "recent-news",
                    "source": "fixture",
                    "category": "macro",
                    "title": "직전 24시간 뉴스",
                    "summary": "fixture",
                    "link": "https://example.com/recent",
                    "pub_date": datetime(2026, 6, 22, 15),
                    "tickers_json": "[]",
                    "themes_json": "[]",
                    "sentiment_score": 0.7,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 22, 15),
                },
                {
                    "article_id": "future-news",
                    "source": "fixture",
                    "category": "macro",
                    "title": "asof 이후 뉴스",
                    "summary": "fixture",
                    "link": "https://example.com/future",
                    "pub_date": datetime(2026, 6, 23, 11),
                    "tickers_json": "[]",
                    "themes_json": "[]",
                    "sentiment_score": 0.1,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 23, 11),
                },
            ]
        ),
        ["article_id"],
    )

    frame = build_market_outlook_dataset(
        conn,
        asof_date="2026-06-23",
        now=datetime(2026, 6, 23, 10, tzinfo=UTC),
    )
    latest = frame[pd.to_datetime(frame["asof_date"]).dt.date.eq(date(2026, 6, 23))]

    assert set(latest["news_count_24h"]) == {1}
    assert set(latest["news_sentiment_score"].round(2)) == {0.7}


def test_market_outlook_uses_x_news_features_and_impact_ablation(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_x_news.duckdb")
    _seed_benchmark(conn)
    append_dedup_table(
        conn,
        "market_news_feed",
        pd.DataFrame(
            [
                {
                    "article_id": "x-market-1",
                    "source": "x_marketnews_feed",
                    "category": "macro",
                    "title": "Korea chip stocks fall after semiconductor sell-off",
                    "summary": "downgrade and weak demand pressure",
                    "link": "https://x.com/MarketNews_Feed/status/1885000000000000001",
                    "pub_date": datetime(2026, 6, 22, 15),
                    "tickers_json": "[]",
                    "themes_json": "[]",
                    "sentiment_score": 0.2,
                    "raw_json": "{}",
                    "collected_at": datetime(2026, 6, 22, 15),
                }
            ]
        ),
        ["article_id"],
    )

    dataset = build_market_outlook_dataset(
        conn,
        asof_date="2026-06-23",
        now=datetime(2026, 6, 23, 10, tzinfo=UTC),
    )
    latest = dataset[pd.to_datetime(dataset["asof_date"]).dt.date.eq(date(2026, 6, 23))]

    assert set(latest["x_news_count_24h"]) == {1}
    assert set(latest["x_news_negative_count_3d"]) == {1}
    assert set(latest["x_news_bias_adjusted_sentiment_score"].round(2)) == {0.2}

    impact = build_x_market_outlook_impact(
        conn,
        {"paths": {"model_dir": str(tmp_path / "models")}},
        asof_date="2026-06-23",
        model_path=tmp_path / "missing_model.json",
    )

    assert len(impact) == 4
    assert {"KOSPI", "KOSDAQ"} == set(impact["market"])
    assert "expected_return_delta" in impact.columns


def test_market_outlook_shock_probability_uses_minus_two_percent_threshold() -> None:
    bearish = normal_cdf((-0.02 - (-0.03)) / 0.01)
    flat = normal_cdf((-0.02 - 0.0) / 0.01)

    assert bearish > 0.80
    assert flat < 0.05


def test_market_outlook_forecasts_four_rows_and_partial_quality(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_forecast.duckdb")
    _seed_benchmark(conn)

    frame = refresh_market_outlook_forecasts(conn, asof_date="2026-06-23")

    assert len(frame) == 4
    assert set(frame["market"]) == {"KOSPI", "KOSDAQ"}
    assert set(frame["horizon"]) == {"TODAY", "WEEK"}
    assert set(frame["target_date"].astype(str)) == {"2026-06-24", "2026-06-26"}
    assert frame["shock_probability"].between(0.0, 1.0).all()
    quality = frame.iloc[0]["data_quality_json"]
    assert "partial_ready" in quality
    assert "KORU" in quality or "Telegram" in quality

    payload = get_market_outlook(conn, date="latest", horizon="all", market="all")
    assert payload["status"] == "partial_ready"
    assert payload["summary"]["count"] == 4
    assert payload["items"][0]["drivers"]


def _seed_benchmark(conn) -> None:
    rows = []
    current = date(2026, 3, 23)
    index = 0
    while current <= date(2026, 6, 23):
        if current.weekday() < 5:
            kospi_close = 1000.0 + index * 2.0 + (index % 7) * 0.8
            kosdaq_close = 800.0 + index * 1.2 - (index % 5) * 0.5
            rows.extend(
                [
                    {
                        "date": current,
                        "benchmark": "KOSPI",
                        "open": kospi_close - 1.0,
                        "high": kospi_close + 3.0,
                        "low": kospi_close - 3.0,
                        "close": kospi_close,
                        "volume": 1000.0,
                        "trading_value": 1000000.0,
                        "collected_at": datetime(2026, 6, 23, 16),
                    },
                    {
                        "date": current,
                        "benchmark": "KOSDAQ",
                        "open": kosdaq_close - 1.0,
                        "high": kosdaq_close + 3.0,
                        "low": kosdaq_close - 3.0,
                        "close": kosdaq_close,
                        "volume": 1000.0,
                        "trading_value": 1000000.0,
                        "collected_at": datetime(2026, 6, 23, 16),
                    },
                ]
            )
            index += 1
        current += timedelta(days=1)
    append_dedup_table(conn, "benchmark_daily", pd.DataFrame(rows), ["date", "benchmark"])

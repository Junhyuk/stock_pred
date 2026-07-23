from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from roboquant.dashboard.dashboard_service import get_market_outlook
from roboquant.db import append_dedup_table, connect_database
from roboquant.market_outlook import (
    _fit_linear_spec,
    _linear_predict,
    build_market_outlook_dataset,
    fit_market_outlook_model,
    get_market_outlook_feedback,
    market_outlook_input_status,
    normal_cdf,
    refresh_market_outlook_forecasts,
    target_dates_for_run,
    update_market_outlook_outcomes,
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


def test_market_outlook_target_dates_use_next_session_after_weekend_stale_asof() -> None:
    targets = target_dates_for_run(date(2026, 7, 16), today=date(2026, 7, 19))

    assert targets["TODAY"] == date(2026, 7, 20)


def test_market_outlook_blocks_stale_domestic_input_until_prior_session_is_complete(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_input_gate.duckdb")
    _seed_market_outlook_input(conn, date(2026, 7, 16), symbols=50)

    blocked = market_outlook_input_status(
        conn,
        asof_date=date(2026, 7, 16),
        target_date=date(2026, 7, 20),
    )

    assert blocked["forecast_status"] == "blocked_stale_input"
    assert blocked["required_domestic_asof_date"] == date(2026, 7, 17)
    assert any("국내 기준일" in message for message in blocked["messages"])

    _seed_market_outlook_input(conn, date(2026, 7, 17), symbols=50)
    confirmed = market_outlook_input_status(
        conn,
        asof_date=date(2026, 7, 17),
        target_date=date(2026, 7, 20),
    )

    assert confirmed["forecast_status"] == "confirmed"
    assert confirmed["benchmark_markets"] == ["KOSDAQ", "KOSPI"]
    assert confirmed["top50_price_count"] == confirmed["top50_total"] == 50


def test_latest_market_outlook_hides_stale_target_until_confirmed_inputs_exist(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_current_gate.duckdb")
    _seed_market_outlook_input(conn, date(2026, 7, 16), symbols=50)
    _seed_forecast_row(conn, date(2026, 7, 16), date(2026, 7, 17), "legacy")
    sunday = datetime(2026, 7, 19, 12, tzinfo=UTC)
    config = {"market_outlook": {"require_confirmed_inputs": True, "use_pykrx_calendar": False}}

    blocked = get_market_outlook(conn, config=config, now=sunday)

    assert blocked["status"] == "blocked_stale_input"
    assert blocked["target_date"] == "2026-07-20"
    assert blocked["items"] == []

    _seed_market_outlook_input(conn, date(2026, 7, 17), symbols=50)
    _seed_forecast_row(conn, date(2026, 7, 17), date(2026, 7, 20), "confirmed")
    confirmed = get_market_outlook(conn, config=config, now=sunday)

    assert confirmed["forecast_status"] == "confirmed"
    assert confirmed["target_date"] == "2026-07-20"
    assert {item["market"] for item in confirmed["items"]} == {"KOSPI", "KOSDAQ"}


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


def test_market_outlook_model_reports_chronological_validation_metrics(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_validation.duckdb")
    _seed_benchmark(conn)
    dataset = build_market_outlook_dataset(conn, asof_date="2026-06-23")

    model = fit_market_outlook_model(dataset)
    spec = model["models"]["KOSPI|TODAY"]

    assert spec["validation_sample_count"] > 0
    assert 0.0 <= spec["validation_direction_accuracy"] <= 1.0
    assert spec["validation_rmse"] >= 0.0


def test_market_outlook_model_blocks_sparse_features() -> None:
    frame = pd.DataFrame(
        {
            "label_return": [0.01 if index % 2 else -0.01 for index in range(100)],
            "sparse": [50.0] * 95 + [0.0] * 5,
            "supported": [float(index) for index in range(100)],
        }
    )

    spec = _fit_linear_spec(frame, ["sparse", "supported"], 0.0)

    assert spec["features"] == ["supported"]
    assert spec["blocked_features"] == ["sparse"]
    assert spec["support_threshold"] == 60


def test_market_outlook_model_caps_standardized_feature_values() -> None:
    spec = {
        "features": ["signal"],
        "intercept": 0.0,
        "coef": [0.01],
        "mean": {"signal": 0.0},
        "std": {"signal": 1.0},
        "fill": {"signal": 0.0},
    }

    assert _linear_predict({"signal": 100.0}, spec) == 0.05
    assert _linear_predict({"signal": -100.0}, spec) == -0.05


def test_market_outlook_settings_preserve_zero_calibration_values() -> None:
    model = fit_market_outlook_model(
        pd.DataFrame(),
        config={
            "market_outlook": {
                "index_weight": 1.0,
                "breadth_weight": 0.0,
                "direction_return_threshold": 0.0,
                "direction_probability_threshold": 0.5,
            }
        },
    )

    assert model["index_weight"] == 1.0
    assert model["breadth_weight"] == 0.0
    assert model["direction_return_threshold"] == 0.0


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
    assert "domestic_index_only" in quality
    assert "KORU" in quality or "Telegram" in quality

    payload = get_market_outlook(conn, date="latest", horizon="all", market="all")
    assert payload["status"] == "domestic_index_only"
    assert payload["summary"]["count"] == 4
    assert payload["items"][0]["drivers"]


def test_market_outlook_uses_explicit_domestic_index_fallback_and_immutable_outcomes(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_immutable.duckdb")
    _seed_benchmark(conn)

    frame = refresh_market_outlook_forecasts(conn, asof_date="2026-06-23")

    assert set(frame["forecast_status"]) == {"domestic_index_only"}
    quality = json.loads(frame.iloc[0]["data_quality_json"])
    freshness = quality["active_feature_freshness"]
    assert freshness["components"]["breadth"]["status"] == "stale_or_default"
    assert quality["input_mode"] == "domestic_index_only"
    before = conn.execute(
        "SELECT expected_return, created_at FROM market_outlook_forecast_history ORDER BY forecast_id LIMIT 1"
    ).fetchone()
    assert conn.execute("SELECT COUNT(*) FROM market_outlook_forecast_history").fetchone()[0] == 4

    refresh_market_outlook_forecasts(conn, asof_date="2026-06-23")
    after = conn.execute(
        "SELECT expected_return, created_at FROM market_outlook_forecast_history ORDER BY forecast_id LIMIT 1"
    ).fetchone()
    assert after == before

    asof_close = conn.execute(
        "SELECT close FROM benchmark_daily WHERE date = ? AND benchmark = 'KOSPI'",
        [date(2026, 6, 23)],
    ).fetchone()[0]
    append_dedup_table(
        conn,
        "benchmark_daily",
        pd.DataFrame(
            [
                {
                    "date": date(2026, 6, 24),
                    "benchmark": "KOSPI",
                    "open": asof_close,
                    "high": asof_close * 1.03,
                    "low": asof_close * 0.99,
                    "close": asof_close * 1.02,
                    "volume": 1.0,
                    "trading_value": 1.0,
                    "collected_at": datetime(2026, 6, 24, 16),
                }
            ]
        ),
        ["date", "benchmark"],
    )
    outcome = update_market_outlook_outcomes(conn)

    assert outcome["updated"] == 1
    saved = conn.execute(
        """
        SELECT actual_return, actual_direction, direction_hit, asof_date, target_date
        FROM market_outlook_forecast_outcomes
        WHERE market = 'KOSPI' AND horizon = 'TODAY'
        """
    ).fetchone()
    assert saved[0] == pytest.approx(0.02)
    assert saved[1] == "BULLISH"
    assert saved[2] in (True, False)
    assert saved[3:] == (date(2026, 6, 23), date(2026, 6, 24))
    assert update_market_outlook_outcomes(conn)["updated"] == 0
    feedback = get_market_outlook_feedback(conn, market="KOSPI", horizon="TODAY")
    assert feedback["status"] == "ready"
    assert feedback["summary"][0]["sample_count"] == 1
    assert feedback["items"][0]["target_date"] == "2026-06-24"
    conn.close()


def test_market_regime_joins_target_session_and_excludes_late_cutoff_rows(tmp_path) -> None:
    conn = connect_database(tmp_path / "outlook_regime_cutoff.duckdb")
    _seed_benchmark(conn)
    rows = [
        (datetime(2026, 6, 23, 22, 30), 11.0, "eligible"),
        (datetime(2026, 6, 23, 23, 30), 99.0, "late"),
    ]
    for cutoff, score, version in rows:
        conn.execute(
            """
            INSERT INTO market_regime_daily (
              prediction_date, prediction_cutoff, us_equity_score, semiconductor_score,
              asia_score, volatility_score, rate_score, fx_score, futures_score,
              commodity_score, global_risk_score, regime, recommended_cash_ratio,
              feature_version, created_at
            ) VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?, 'neutral', 0.15, ?, ?)
            """,
            [date(2026, 6, 24), cutoff, score, version, cutoff],
        )

    frame = build_market_outlook_dataset(
        conn,
        config={"regime": {"cutoff_time_kst": "08:00"}},
        asof_date="2026-06-23",
        include_feature_provenance=True,
    )
    row = frame[
        (frame["market"] == "KOSPI")
        & (frame["horizon"] == "TODAY")
        & (frame["asof_date"] == date(2026, 6, 23))
    ].iloc[0]

    assert row["__raw_global_risk_score"] == 11.0
    assert row["global_risk_score"] == 11.0
    conn.close()


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


def _seed_market_outlook_input(conn, asof: date, *, symbols: int) -> None:
    append_dedup_table(
        conn,
        "benchmark_daily",
        pd.DataFrame(
            [
                {
                    "date": asof,
                    "benchmark": market,
                    "open": 1000.0,
                    "high": 1010.0,
                    "low": 990.0,
                    "close": 1000.0,
                    "volume": 1.0,
                    "trading_value": 1.0,
                    "collected_at": datetime(2026, 7, 17, 18),
                }
                for market in ("KOSPI", "KOSDAQ")
            ]
        ),
        ["date", "benchmark"],
    )
    if asof != date(2026, 7, 16):
        return
    snapshot = date(2026, 7, 17)
    rows = []
    for index in range(symbols):
        symbol = f"{index + 1:06d}"
        market = "KOSPI" if index < 30 else "KOSDAQ"
        conn.execute(
            """
            INSERT INTO prediction_universe_snapshot (
              snapshot_date, symbol, name, market, raw_market_cap_rank,
              prediction_rank, provider, universe_rule, is_enabled
            )
            VALUES (?, ?, ?, ?, ?, ?, 'fixture', 'prediction_top_market_cap', TRUE)
            """,
            [snapshot, symbol, f"Stock {symbol}", market, index + 1, index + 1],
        )
        rows.append(
            {
                "date": date(2026, 7, 17),
                "symbol": symbol,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "adj_close": 100.0,
                "volume": 1000.0,
                "trading_value": 100000.0,
                "market_cap": None,
                "source": "fixture",
                "collected_at": datetime(2026, 7, 17, 18),
            }
        )
    conn.execute(
        """
        INSERT INTO universe_refresh_runs (
          run_id, snapshot_date, universe_rule, provider, status, completed_at
        ) VALUES ('fixture-ready', ?, 'prediction_top_market_cap', 'fixture', 'ready', ?)
        """,
        [snapshot, datetime(2026, 7, 17, 18)],
    )
    append_dedup_table(conn, "prices_daily", pd.DataFrame(rows), ["date", "symbol"])


def _seed_forecast_row(conn, asof: date, target: date, status: str) -> None:
    rows = []
    for market in ("KOSPI", "KOSDAQ"):
        rows.append(
            {
                "asof_date": asof,
                "target_date": target,
                "horizon": "TODAY",
                "market": market,
                "expected_return": 0.01,
                "range_low": -0.01,
                "range_high": 0.03,
                "up_probability": 0.6,
                "down_probability": 0.4,
                "shock_probability": 0.1,
                "direction": "BULLISH",
                "confidence": 0.7,
                "drivers_json": "[]",
                "data_quality_json": '{"status":"ready"}',
                "model_version": f"fixture-{target.isoformat()}",
                "created_at": datetime(2026, 7, 17, 18),
                "prediction_session_date": target,
                "domestic_asof_date": asof,
                "forecast_status": status,
                "calendar_source": "fixture",
            }
        )
    append_dedup_table(
        conn,
        "market_outlook_forecasts",
        pd.DataFrame(rows),
        ["asof_date", "target_date", "horizon", "market", "model_version"],
    )

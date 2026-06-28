from __future__ import annotations

from roboquant.db import connect_database


def test_duckdb_schema_contains_required_tables(tmp_path) -> None:
    duckdb = __import__("pytest").importorskip("duckdb")
    assert duckdb is not None

    conn = connect_database(tmp_path / "test.duckdb")
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }

    assert {
        "symbols",
        "prices_daily",
        "benchmark_daily",
        "market_metrics_daily",
        "investor_flows_daily",
        "market_credit_balance_daily",
        "collection_failures",
        "analyst_reports",
        "analyst_report_outcomes",
        "analyst_scores",
        "consensus_history",
        "features_daily",
        "labels",
        "predictions",
        "long_short_recommendations",
        "long_short_backtest_results",
        "market_up_down_recommendations",
        "model_registry",
        "backtest_runs",
        "backtest_results",
        "model_performance_daily",
        "dashboard_snapshot",
        "stock_clusters",
        "cluster_summary",
        "model_predictions",
        "feature_set_registry",
        "recommendations",
        "raw_market_cap_snapshot",
        "prediction_universe_snapshot",
        "universe_refresh_runs",
        "global_market_daily",
        "global_market_intraday_snapshot",
        "market_regime_daily",
        "stock_global_exposure",
        "us_sector_linkage_daily",
        "yahoo_prices_daily",
        "yahoo_fundamentals_snapshot",
        "news_articles",
        "market_news_feed",
        "news_signal_daily",
        "market_outlook_forecasts",
        "x_news_prediction_impact_daily",
        "x_market_outlook_impact_daily",
        "koru_korea_linkage",
        "koru_weight_decisions",
        "telegram_posts",
        "telegram_ticker_mentions",
        "telegram_signal_daily",
        "telegram_market_signal_daily",
        "today_market_update_runs",
        "today_market_snapshot",
    }.issubset(tables)

    views = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.views WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert "current_prediction_universe" in views

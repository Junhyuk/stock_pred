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
        "marketnewsfeed_headlines",
        "marketnewsfeed_llm_runs",
        "marketnewsfeed_llm_assessments",
        "marketnewsfeed_llm_signal_daily",
        "marketnewsfeed_llm_shadow_runs",
        "marketnewsfeed_llm_market_shadow",
        "local_llm_news_documents",
        "local_llm_news_runs",
        "local_llm_news_assessments",
        "local_llm_news_signal_daily",
        "local_llm_news_briefs",
        "market_outlook_ml_shadow_forecasts",
        "market_outlook_ml_shadow_runs",
        "news_url_crawl_cache",
        "bigkinds_issue_daily",
        "global_news_events",
        "dart_disclosures",
        "official_market_prices",
        "official_data_runs",
        "news_signal_daily",
        "market_outlook_forecasts",
        "market_outlook_forecast_history",
        "market_outlook_forecast_outcomes",
        "market_outlook_direction_shadow_runs",
        "market_outlook_direction_shadow_observations",
        "market_outlook_feature_attributions",
        "market_investor_flow_runs",
        "market_investor_flows_daily",
        "market_investor_top_net_purchases_daily",
        "market_flow_features_daily",
        "market_outlook_range_shadow",
        "market_anchor_constituents_daily",
        "market_anchor_features_daily",
        "market_anchor_shadow_forecasts",
        "market_anchor_shadow_backtest_runs",
        "premarket_features_daily",
        "premarket_shadow_forecasts",
        "market_us_linkage_features_daily",
        "market_us_linkage_shadow_forecasts",
        "market_us_linkage_feedback_daily",
        "market_us_linkage_backtest_runs",
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

    forecast_columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'market_outlook_forecasts'
            """
        ).fetchall()
    }
    assert {
        "prediction_session_date",
        "domestic_asof_date",
        "global_cutoff",
        "forecast_status",
        "calendar_source",
    }.issubset(forecast_columns)

    outcome_columns = {
        row[0]
        for row in conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'market_outlook_forecast_outcomes'
            """
        ).fetchall()
    }
    assert {
        "forecast_id",
        "actual_return",
        "actual_direction",
        "direction_hit",
        "asof_close",
        "target_close",
    }.issubset(outcome_columns)

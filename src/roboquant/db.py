from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pandas as pd

SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS symbols (
      symbol VARCHAR,
      name VARCHAR,
      market VARCHAR,
      sector VARCHAR,
      listing_date DATE,
      delisting_date DATE,
      is_active BOOLEAN,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prices_daily (
      date DATE,
      symbol VARCHAR,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      adj_close DOUBLE,
      volume DOUBLE,
      trading_value DOUBLE,
      market_cap DOUBLE,
      source VARCHAR,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS benchmark_daily (
      date DATE,
      benchmark VARCHAR,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      volume DOUBLE,
      trading_value DOUBLE,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS features_daily (
      date DATE,
      symbol VARCHAR,
      horizon VARCHAR,
      horizon_days INTEGER,
      ret_21d DOUBLE,
      ret_63d DOUBLE,
      ret_126d DOUBLE,
      ret_252d DOUBLE,
      ma_gap_20d DOUBLE,
      ma_gap_60d DOUBLE,
      ma_gap_120d DOUBLE,
      ma_gap_250d DOUBLE,
      volatility_20d DOUBLE,
      volatility_60d DOUBLE,
      volume_ratio_20d DOUBLE,
      trading_value_ma20 DOUBLE,
      close_to_52w_high DOUBLE,
      rsi_14 DOUBLE,
      momentum_score DOUBLE,
      volatility_score DOUBLE,
      liquidity_score DOUBLE,
      risk_score DOUBLE,
      market_cap DOUBLE,
      per DOUBLE,
      pbr DOUBLE,
      eps DOUBLE,
      bps DOUBLE,
      dividend_yield DOUBLE,
      market_cap_score DOUBLE,
      value_score DOUBLE,
      quality_score DOUBLE,
      supply_demand_score DOUBLE,
      sentiment_score DOUBLE,
      foreign_net_value_1d_sum DOUBLE,
      foreign_net_value_5d_sum DOUBLE,
      foreign_net_value_20d_sum DOUBLE,
      foreign_net_value_60d_sum DOUBLE,
      institution_net_value_1d_sum DOUBLE,
      institution_net_value_5d_sum DOUBLE,
      institution_net_value_20d_sum DOUBLE,
      institution_net_value_60d_sum DOUBLE,
      retail_net_value_1d_sum DOUBLE,
      retail_net_value_5d_sum DOUBLE,
      retail_net_value_20d_sum DOUBLE,
      retail_net_value_60d_sum DOUBLE,
      foreign_net_20d_to_mcap DOUBLE,
      institution_net_20d_to_value DOUBLE,
      retail_overheat_score DOUBLE,
      foreign_consecutive_buy_days INTEGER,
      institution_consecutive_buy_days INTEGER,
      consensus_upside_pct DOUBLE,
      consensus_momentum_30_90 DOUBLE,
      target_up_count_30d DOUBLE,
      target_down_count_30d DOUBLE,
      new_coverage_count_30d DOUBLE,
      target_revision_balance_30d DOUBLE,
      consensus_revision_score DOUBLE,
      target_upside_score DOUBLE,
      analyst_reliability_score DOUBLE,
      weighted_analyst_reliability_score DOUBLE,
      telegram_attention_score DOUBLE,
      telegram_sentiment_score DOUBLE,
      telegram_urgency_score DOUBLE,
      telegram_risk_score DOUBLE,
      telegram_semiconductor_score DOUBLE,
      telegram_macro_score DOUBLE,
      us_sector_return_1d DOUBLE,
      us_sector_return_5d DOUBLE,
      us_sector_zscore_20d DOUBLE,
      us_sector_beta_60d DOUBLE,
      us_sector_corr_60d DOUBLE,
      us_sector_impact_score DOUBLE,
      us_sector_direction_agreement DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS koru_korea_linkage (
      trade_date DATE PRIMARY KEY,
      us_signal_date DATE,
      koru_return_1d DOUBLE,
      koru_volume_ratio_20d DOUBLE,
      ewy_return_1d DOUBLE,
      spy_return_1d DOUBLE,
      qqq_return_1d DOUBLE,
      koru_ewy_spread_1d DOUBLE,
      koru_leverage_drift_1d DOUBLE,
      kospi_return_1d DOUBLE,
      kosdaq_return_1d DOUBLE,
      samsung_return_1d DOUBLE,
      hynix_return_1d DOUBLE,
      usdkrw_change_pct DOUBLE,
      foreign_net_buy_krw DOUBLE,
      institution_net_buy_krw DOUBLE,
      retail_net_buy_krw DOUBLE,
      koru_impact_score DOUBLE,
      koru_market_shock_flag BOOLEAN,
      market_index_trigger_json VARCHAR,
      causes_json VARCHAR,
      data_quality_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS koru_weight_decisions (
      decision_date DATE,
      horizon VARCHAR,
      decision VARCHAR,
      overlay_weight DOUBLE,
      baseline_metrics_json VARCHAR,
      enhanced_metrics_json VARCHAR,
      reason_json VARCHAR,
      created_at TIMESTAMP,
      PRIMARY KEY (decision_date, horizon)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analyst_reports (
      report_id VARCHAR,
      report_date DATE,
      symbol VARCHAR,
      stock_name VARCHAR,
      market VARCHAR,
      sector VARCHAR,
      broker_name VARCHAR,
      analyst_name VARCHAR,
      report_title VARCHAR,
      investment_rating VARCHAR,
      target_price DOUBLE,
      previous_target_price DOUBLE,
      target_change_pct DOUBLE,
      current_price_at_report DOUBLE,
      upside_pct_at_report DOUBLE,
      source_name VARCHAR,
      source_url VARCHAR,
      imported_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analyst_report_outcomes (
      report_id VARCHAR,
      symbol VARCHAR,
      report_date DATE,
      target_price DOUBLE,
      price_1m DOUBLE,
      price_3m DOUBLE,
      price_6m DOUBLE,
      price_12m DOUBLE,
      price_24m DOUBLE,
      return_1m DOUBLE,
      return_3m DOUBLE,
      return_6m DOUBLE,
      return_12m DOUBLE,
      return_24m DOUBLE,
      benchmark_return_3m DOUBLE,
      benchmark_return_6m DOUBLE,
      benchmark_return_12m DOUBLE,
      excess_return_3m DOUBLE,
      excess_return_6m DOUBLE,
      excess_return_12m DOUBLE,
      max_drawdown_3m DOUBLE,
      max_drawdown_6m DOUBLE,
      target_hit_date DATE,
      target_hit_days INTEGER,
      target_hit_6m BOOLEAN,
      target_hit_12m BOOLEAN,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS analyst_scores (
      analyst_name VARCHAR,
      broker_name VARCHAR,
      as_of_date DATE,
      report_count INTEGER,
      recent_report_count_1y INTEGER,
      rmse_12m DOUBLE,
      mae_12m DOUBLE,
      bias_12m DOUBLE,
      std_error_12m DOUBLE,
      direction_accuracy_6m DOUBLE,
      direction_accuracy_12m DOUBLE,
      target_hit_rate_6m DOUBLE,
      target_hit_rate_12m DOUBLE,
      avg_excess_return_6m DOUBLE,
      avg_excess_return_12m DOUBLE,
      reliability_score DOUBLE,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS consensus_history (
      date DATE,
      symbol VARCHAR,
      stock_name VARCHAR,
      consensus_target_avg DOUBLE,
      consensus_target_median DOUBLE,
      consensus_target_high DOUBLE,
      consensus_target_low DOUBLE,
      consensus_target_std DOUBLE,
      report_count_30d INTEGER,
      report_count_90d INTEGER,
      target_up_count_30d INTEGER,
      target_down_count_30d INTEGER,
      new_coverage_count_30d INTEGER,
      rating_buy_ratio DOUBLE,
      consensus_upside_pct DOUBLE,
      consensus_momentum_30_90 DOUBLE,
      target_revision_balance_30d DOUBLE,
      consensus_revision_score DOUBLE,
      target_upside_score DOUBLE,
      analyst_reliability_score DOUBLE,
      weighted_analyst_reliability_score DOUBLE,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_metrics_daily (
      date DATE,
      symbol VARCHAR,
      market_cap DOUBLE,
      per DOUBLE,
      pbr DOUBLE,
      eps DOUBLE,
      bps DOUBLE,
      dividend_yield DOUBLE,
      source VARCHAR,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS investor_flows_daily (
      date DATE,
      symbol VARCHAR,
      foreign_buy_value DOUBLE,
      foreign_sell_value DOUBLE,
      foreign_net_value DOUBLE,
      institution_buy_value DOUBLE,
      institution_sell_value DOUBLE,
      institution_net_value DOUBLE,
      retail_buy_value DOUBLE,
      retail_sell_value DOUBLE,
      retail_net_value DOUBLE,
      pension_net_value DOUBLE,
      trust_net_value DOUBLE,
      private_fund_net_value DOUBLE,
      source VARCHAR,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_credit_balance_daily (
      date DATE,
      market VARCHAR,
      credit_loan_balance_krw DOUBLE,
      credit_loan_delta_1d_krw DOUBLE,
      credit_loan_delta_5d_krw DOUBLE,
      credit_loan_delta_20d_krw DOUBLE,
      credit_to_market_cap DOUBLE,
      source VARCHAR,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS collection_failures (
      collected_at TIMESTAMP,
      step VARCHAR,
      source VARCHAR,
      symbol VARCHAR,
      target_date DATE,
      error_message VARCHAR,
      retry_count INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS labels (
      asof_date DATE,
      symbol VARCHAR,
      horizon VARCHAR,
      horizon_days INTEGER,
      future_return DOUBLE,
      benchmark_return DOUBLE,
      excess_return DOUBLE,
      rank_quantile DOUBLE,
      is_top20pct BOOLEAN,
      is_bottom20pct BOOLEAN,
      max_drawdown_forward DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS predictions (
      asof_date DATE,
      symbol VARCHAR,
      horizon VARCHAR,
      pred_return DOUBLE,
      pred_prob_top20 DOUBLE,
      pred_prob_bottom20 DOUBLE,
      long_score DOUBLE,
      short_score DOUBLE,
      pred_risk DOUBLE,
      confidence DOUBLE,
      model_version VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS long_short_recommendations (
      asof_date DATE,
      horizon VARCHAR,
      market VARCHAR,
      symbol VARCHAR,
      side VARCHAR,
      leg_rank INTEGER,
      long_score DOUBLE,
      short_score DOUBLE,
      pred_return DOUBLE,
      pred_prob_top20 DOUBLE,
      pred_prob_bottom20 DOUBLE,
      risk_score DOUBLE,
      confidence DOUBLE,
      weight DOUBLE,
      reason_json VARCHAR,
      risk_flags_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS long_short_backtest_results (
      asof_date DATE,
      horizon VARCHAR,
      market VARCHAR,
      long_symbols VARCHAR,
      short_symbols VARCHAR,
      long_return DOUBLE,
      short_return DOUBLE,
      gross_spread_return DOUBLE,
      transaction_cost DOUBLE,
      net_return DOUBLE,
      turnover DOUBLE,
      equity DOUBLE,
      metrics_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_registry (
      model_name VARCHAR,
      model_type VARCHAR,
      feature_set_name VARCHAR,
      label_name VARCHAR,
      horizons VARCHAR,
      train_start DATE,
      train_end DATE,
      valid_start DATE,
      valid_end DATE,
      test_start DATE,
      test_end DATE,
      status VARCHAR,
      production_weight DOUBLE,
      shadow_mode BOOLEAN,
      artifact_path VARCHAR,
      metrics_json VARCHAR,
      fail_reason VARCHAR,
      created_at TIMESTAMP,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_runs (
      run_id VARCHAR,
      model_name VARCHAR,
      baseline_model_name VARCHAR,
      horizon VARCHAR,
      start_date DATE,
      end_date DATE,
      top_k INTEGER,
      top20_return DOUBLE,
      excess_return DOUBLE,
      hit_ratio DOUBLE,
      mdd DOUBLE,
      turnover DOUBLE,
      sharpe DOUBLE,
      transaction_cost_adjusted_return DOUBLE,
      accepted BOOLEAN,
      fail_reason VARCHAR,
      metrics_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS backtest_results (
      result_id VARCHAR,
      prediction_date DATE,
      target_date DATE,
      symbol VARCHAR,
      model_name VARCHAR,
      model_version VARCHAR,
      horizon VARCHAR,
      horizon_days INTEGER,
      entry_price DOUBLE,
      exit_price DOUBLE,
      actual_return DOUBLE,
      predicted_return DOUBLE,
      predicted_probability DOUBLE,
      recommendation_score DOUBLE,
      benchmark_return DOUBLE,
      excess_return DOUBLE,
      is_hit BOOLEAN,
      is_outperform BOOLEAN,
      is_top20 BOOLEAN,
      rank_no INTEGER,
      sector VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_performance_daily (
      eval_date DATE,
      model_name VARCHAR,
      model_version VARCHAR,
      horizon VARCHAR,
      horizon_days INTEGER,
      sample_count INTEGER,
      hit_ratio DOUBLE,
      precision_top20 DOUBLE,
      avg_actual_return DOUBLE,
      avg_benchmark_return DOUBLE,
      avg_excess_return DOUBLE,
      median_actual_return DOUBLE,
      win_rate DOUBLE,
      mdd DOUBLE,
      sharpe DOUBLE,
      rank_ic DOUBLE,
      production_weight DOUBLE,
      gate_status VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dashboard_snapshot (
      snapshot_date DATE,
      position_summary_json VARCHAR,
      theme_data_json VARCHAR,
      ai_recommendations_json VARCHAR,
      core_portfolio_json VARCHAR,
      quant_portfolio_json VARCHAR,
      qual_portfolio_json VARCHAR,
      upside_ranking_json VARCHAR,
      analyst_reports_json VARCHAR,
      backtest_summary_json VARCHAR,
      model_accuracy_json VARCHAR,
      focus_stock_json VARCHAR,
      cluster_data_json VARCHAR,
      sector_ranking_json VARCHAR,
      data_quality_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_clusters (
      asof_date DATE,
      horizon VARCHAR,
      symbol VARCHAR,
      cluster_id INTEGER,
      cluster_label VARCHAR,
      distance_to_centroid DOUBLE,
      feature_values_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cluster_summary (
      asof_date DATE,
      horizon VARCHAR,
      cluster_id INTEGER,
      cluster_label VARCHAR,
      member_count INTEGER,
      centroid_json VARCHAR,
      top_symbols_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_predictions (
      date DATE,
      symbol VARCHAR,
      model_name VARCHAR,
      model_version VARCHAR,
      horizon VARCHAR,
      pred_score DOUBLE,
      pred_prob DOUBLE,
      risk_score DOUBLE,
      recommendation_score DOUBLE,
      rank INTEGER,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_market_cap_snapshot (
      snapshot_date DATE NOT NULL,
      symbol VARCHAR NOT NULL,
      name VARCHAR NOT NULL,
      market VARCHAR NOT NULL,
      raw_market_cap_rank INTEGER,
      market_cap DOUBLE,
      security_type VARCHAR,
      is_suspended BOOLEAN,
      listing_date DATE,
      provider VARCHAR NOT NULL,
      universe_rule VARCHAR NOT NULL,
      exclusion_reason VARCHAR,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (snapshot_date, symbol, universe_rule)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS prediction_universe_snapshot (
      snapshot_date DATE NOT NULL,
      symbol VARCHAR NOT NULL,
      name VARCHAR NOT NULL,
      market VARCHAR NOT NULL,
      raw_market_cap_rank INTEGER,
      prediction_rank INTEGER,
      market_cap DOUBLE,
      security_type VARCHAR,
      provider VARCHAR NOT NULL,
      universe_rule VARCHAR NOT NULL,
      is_enabled BOOLEAN DEFAULT TRUE,
      exclusion_reason VARCHAR,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (snapshot_date, symbol, universe_rule)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS universe_refresh_runs (
      run_id VARCHAR PRIMARY KEY,
      snapshot_date DATE NOT NULL,
      universe_rule VARCHAR NOT NULL,
      provider VARCHAR NOT NULL,
      status VARCHAR NOT NULL
        CHECK (status IN ('ready', 'refreshing', 'stale', 'failed')),
      kospi_raw_count INTEGER DEFAULT 0,
      kosdaq_raw_count INTEGER DEFAULT 0,
      kospi_selected_count INTEGER DEFAULT 0,
      kosdaq_selected_count INTEGER DEFAULT 0,
      kospi_excluded_count INTEGER DEFAULT 0,
      kosdaq_excluded_count INTEGER DEFAULT 0,
      started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      completed_at TIMESTAMP,
      error_message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS global_market_daily (
      trade_date DATE NOT NULL,
      symbol VARCHAR NOT NULL,
      market_group VARCHAR NOT NULL,
      display_name VARCHAR NOT NULL,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      volume DOUBLE,
      return_1d DOUBLE,
      return_5d DOUBLE,
      return_20d DOUBLE,
      volatility_20d DOUBLE,
      source_name VARCHAR NOT NULL,
      source_timestamp TIMESTAMP,
      ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (trade_date, symbol, source_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS global_market_intraday_snapshot (
      snapshot_at TIMESTAMP NOT NULL,
      symbol VARCHAR NOT NULL,
      market_group VARCHAR NOT NULL,
      price DOUBLE,
      change_rate DOUBLE,
      source_name VARCHAR NOT NULL,
      source_timestamp TIMESTAMP,
      freshness_seconds INTEGER,
      ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (snapshot_at, symbol, source_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_regime_daily (
      prediction_date DATE NOT NULL,
      prediction_cutoff TIMESTAMP NOT NULL,
      us_equity_score DOUBLE,
      semiconductor_score DOUBLE,
      asia_score DOUBLE,
      volatility_score DOUBLE,
      rate_score DOUBLE,
      fx_score DOUBLE,
      futures_score DOUBLE,
      commodity_score DOUBLE,
      global_risk_score DOUBLE NOT NULL,
      regime VARCHAR NOT NULL,
      recommended_cash_ratio DOUBLE NOT NULL,
      signals_json VARCHAR,
      reasons_json VARCHAR,
      feature_version VARCHAR NOT NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (prediction_date, prediction_cutoff, feature_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_global_exposure (
      symbol VARCHAR NOT NULL,
      feature_version VARCHAR NOT NULL,
      beta_sp500 DOUBLE,
      beta_nasdaq DOUBLE,
      beta_sox DOUBLE,
      beta_nikkei DOUBLE,
      beta_taiwan DOUBLE,
      beta_usdkrw DOUBLE,
      beta_wti DOUBLE,
      sector VARCHAR,
      estimated_from DATE,
      estimated_to DATE,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (symbol, feature_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS us_sector_linkage_daily (
      trade_date DATE NOT NULL,
      domestic_sector VARCHAR NOT NULL,
      primary_proxy VARCHAR,
      proxy_symbols_json VARCHAR,
      us_sector_return_1d DOUBLE,
      us_sector_return_5d DOUBLE,
      us_sector_zscore_20d DOUBLE,
      us_sector_beta_60d DOUBLE,
      us_sector_corr_60d DOUBLE,
      us_sector_impact_score DOUBLE,
      us_sector_direction_agreement DOUBLE,
      sample_count_60d INTEGER,
      data_quality_json VARCHAR,
      created_at TIMESTAMP,
      PRIMARY KEY (trade_date, domestic_sector)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS yahoo_prices_daily (
      date DATE NOT NULL,
      symbol VARCHAR NOT NULL,
      yahoo_symbol VARCHAR NOT NULL,
      asset_type VARCHAR,
      open DOUBLE,
      high DOUBLE,
      low DOUBLE,
      close DOUBLE,
      adj_close DOUBLE,
      volume DOUBLE,
      currency VARCHAR,
      source_timestamp TIMESTAMP,
      collected_at TIMESTAMP,
      PRIMARY KEY (date, yahoo_symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS yahoo_fundamentals_snapshot (
      asof_date DATE NOT NULL,
      symbol VARCHAR NOT NULL,
      yahoo_symbol VARCHAR NOT NULL,
      asset_type VARCHAR,
      market_cap DOUBLE,
      trailing_pe DOUBLE,
      forward_pe DOUBLE,
      price_to_book DOUBLE,
      beta DOUBLE,
      dividend_yield DOUBLE,
      currency VARCHAR,
      raw_info_json VARCHAR,
      collected_at TIMESTAMP,
      PRIMARY KEY (asof_date, yahoo_symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS news_articles (
      article_id VARCHAR PRIMARY KEY,
      collected_at TIMESTAMP,
      query_date DATE,
      symbol VARCHAR,
      name VARCHAR,
      query VARCHAR,
      title VARCHAR,
      description VARCHAR,
      originallink VARCHAR,
      link VARCHAR,
      pub_date TIMESTAMP,
      source_name VARCHAR,
      sentiment_score DOUBLE,
      raw_json VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_news_feed (
      article_id VARCHAR PRIMARY KEY,
      source VARCHAR,
      category VARCHAR,
      title VARCHAR,
      summary VARCHAR,
      link VARCHAR,
      pub_date TIMESTAMP,
      tickers_json VARCHAR,
      themes_json VARCHAR,
      sentiment_score DOUBLE,
      raw_json VARCHAR,
      collected_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_up_down_recommendations (
      asof_date DATE,
      horizon VARCHAR,
      market VARCHAR,
      symbol VARCHAR,
      side VARCHAR,
      rank INTEGER,
      long_score DOUBLE,
      short_score DOUBLE,
      pred_return DOUBLE,
      pred_prob_top20 DOUBLE,
      pred_prob_bottom20 DOUBLE,
      risk_score DOUBLE,
      confidence DOUBLE,
      reason_json VARCHAR,
      risk_flags_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_move_explanations (
      asof_date DATE,
      scope VARCHAR,
      symbol VARCHAR,
      market VARCHAR,
      name VARCHAR,
      move_pct DOUBLE,
      direction VARCHAR,
      triggered BOOLEAN,
      primary_reason VARCHAR,
      evidence_json VARCHAR,
      prediction_context_json VARCHAR,
      market_index_trigger_json VARCHAR,
      confidence DOUBLE,
      data_quality_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_outlook_forecasts (
      asof_date DATE,
      target_date DATE,
      horizon VARCHAR,
      market VARCHAR,
      expected_return DOUBLE,
      range_low DOUBLE,
      range_high DOUBLE,
      up_probability DOUBLE,
      down_probability DOUBLE,
      shock_probability DOUBLE,
      direction VARCHAR,
      confidence DOUBLE,
      drivers_json VARCHAR,
      data_quality_json VARCHAR,
      model_version VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_posts (
      channel VARCHAR NOT NULL,
      message_id BIGINT NOT NULL,
      date_utc TIMESTAMP,
      text VARCHAR,
      text_excerpt VARCHAR,
      tickers_json VARCHAR,
      urls_json VARCHAR,
      themes_json VARCHAR,
      risk_keywords_json VARCHAR,
      sentiment_raw DOUBLE,
      sentiment_score DOUBLE,
      urgency_score DOUBLE,
      source_weight DOUBLE,
      duplicate_key VARCHAR,
      duplicate_score DOUBLE,
      telegram_url VARCHAR,
      collected_at TIMESTAMP,
      raw_json VARCHAR,
      PRIMARY KEY (channel, message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_ticker_mentions (
      mention_id VARCHAR PRIMARY KEY,
      ticker VARCHAR NOT NULL,
      channel VARCHAR NOT NULL,
      message_id BIGINT NOT NULL,
      date_utc TIMESTAMP,
      themes_json VARCHAR,
      risk_keywords_json VARCHAR,
      sentiment_raw DOUBLE,
      sentiment_score DOUBLE,
      urgency_score DOUBLE,
      source_weight DOUBLE,
      duplicate_key VARCHAR,
      duplicate_score DOUBLE,
      telegram_url VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_signal_daily (
      signal_date DATE NOT NULL,
      ticker VARCHAR NOT NULL,
      mention_count_1h INTEGER,
      mention_count_24h INTEGER,
      mention_delta_24h DOUBLE,
      sentiment_avg_24h DOUBLE,
      urgency_avg_24h DOUBLE,
      source_weighted_score DOUBLE,
      duplicate_score DOUBLE,
      risk_penalty DOUBLE,
      price_momentum_score DOUBLE,
      telegram_attention_score DOUBLE,
      final_signal_score DOUBLE,
      themes_json VARCHAR,
      risk_keywords_json VARCHAR,
      evidence_json VARCHAR,
      created_at TIMESTAMP,
      PRIMARY KEY (signal_date, ticker)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS telegram_market_signal_daily (
      signal_date DATE PRIMARY KEY,
      message_count_1h INTEGER,
      message_count_24h INTEGER,
      sentiment_avg_24h DOUBLE,
      urgency_avg_24h DOUBLE,
      source_weighted_score DOUBLE,
      duplicate_score DOUBLE,
      risk_penalty DOUBLE,
      telegram_attention_score DOUBLE,
      telegram_sentiment_score DOUBLE,
      telegram_urgency_score DOUBLE,
      telegram_risk_score DOUBLE,
      telegram_semiconductor_score DOUBLE,
      telegram_macro_score DOUBLE,
      themes_json VARCHAR,
      risk_keywords_json VARCHAR,
      evidence_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS today_market_update_runs (
      run_id VARCHAR PRIMARY KEY,
      run_date DATE,
      status VARCHAR,
      started_at TIMESTAMP,
      completed_at TIMESTAMP,
      steps_json VARCHAR,
      error_message VARCHAR
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS today_market_snapshot (
      snapshot_date DATE PRIMARY KEY,
      status VARCHAR,
      focus_prices_json VARCHAR,
      yahoo_prices_json VARCHAR,
      global_regime_json VARCHAR,
      global_markets_json VARCHAR,
      news_json VARCHAR,
      move_explanations_json VARCHAR,
      market_outlook_json VARCHAR,
      data_quality_json VARCHAR,
      created_at TIMESTAMP
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_global_market_daily_symbol_date
    ON global_market_daily(symbol, trade_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_global_market_intraday_symbol_time
    ON global_market_intraday_snapshot(symbol, snapshot_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_us_sector_linkage_sector_date
    ON us_sector_linkage_daily(domestic_sector, trade_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_regime_daily_date
    ON market_regime_daily(prediction_date, feature_version)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_yahoo_prices_daily_symbol_date
    ON yahoo_prices_daily(symbol, date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_yahoo_fundamentals_symbol_date
    ON yahoo_fundamentals_snapshot(symbol, asof_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_news_articles_symbol_pub_date
    ON news_articles(symbol, pub_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_news_feed_source_pub_date
    ON market_news_feed(source, pub_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_news_feed_category_pub_date
    ON market_news_feed(category, pub_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_posts_channel_date
    ON telegram_posts(channel, date_utc)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_mentions_ticker_date
    ON telegram_ticker_mentions(ticker, date_utc)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_signal_daily_score
    ON telegram_signal_daily(signal_date, final_signal_score)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_telegram_market_signal_daily_date
    ON telegram_market_signal_daily(signal_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_long_short_recommendations_latest
    ON long_short_recommendations(horizon, asof_date, side)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_up_down_recommendations_latest
    ON market_up_down_recommendations(horizon, asof_date, side, market)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_move_explanations_latest
    ON market_move_explanations(asof_date, scope, triggered, market)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_market_outlook_forecasts_latest
    ON market_outlook_forecasts(asof_date, horizon, market)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_long_short_backtest_results_horizon
    ON long_short_backtest_results(horizon, asof_date)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_today_market_update_runs_date
    ON today_market_update_runs(run_date, status)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_prediction_universe_snapshot_market
    ON prediction_universe_snapshot(snapshot_date, market, is_enabled)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_universe_refresh_runs_status
    ON universe_refresh_runs(universe_rule, status, snapshot_date)
    """,
    """
    CREATE OR REPLACE VIEW current_prediction_universe AS
    WITH latest_ready AS (
      SELECT
        universe_rule,
        snapshot_date,
        provider,
        status,
        ROW_NUMBER() OVER (
          PARTITION BY universe_rule
          ORDER BY
            snapshot_date DESC,
            completed_at DESC NULLS LAST,
            started_at DESC NULLS LAST,
            run_id DESC
        ) AS row_number
      FROM universe_refresh_runs
      WHERE status = 'ready'
    )
    SELECT
      snapshot.snapshot_date,
      snapshot.symbol,
      snapshot.name,
      snapshot.market,
      snapshot.raw_market_cap_rank,
      snapshot.prediction_rank,
      snapshot.market_cap,
      snapshot.security_type,
      snapshot.provider,
      snapshot.universe_rule,
      snapshot.is_enabled,
      snapshot.exclusion_reason,
      snapshot.created_at,
      latest_ready.provider AS refresh_provider,
      latest_ready.status AS refresh_status
    FROM prediction_universe_snapshot AS snapshot
    JOIN latest_ready
      ON snapshot.universe_rule = latest_ready.universe_rule
      AND snapshot.snapshot_date = latest_ready.snapshot_date
      AND latest_ready.row_number = 1
    WHERE snapshot.is_enabled = TRUE
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backtest_results_date
    ON backtest_results(prediction_date, horizon)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backtest_results_model
    ON backtest_results(model_name, model_version, horizon)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_backtest_results_symbol
    ON backtest_results(symbol, horizon)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_model_performance_daily_model
    ON model_performance_daily(model_name, model_version, horizon)
    """,
    """
    CREATE TABLE IF NOT EXISTS feature_set_registry (
      feature_set_name VARCHAR,
      feature_list_json VARCHAR,
      status VARCHAR,
      description VARCHAR,
      created_at TIMESTAMP,
      updated_at TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recommendations (
      asof_date DATE,
      horizon VARCHAR,
      symbol VARCHAR,
      final_score DOUBLE,
      rank INTEGER,
      reason_json VARCHAR,
      risk_flags_json VARCHAR,
      model_version VARCHAR
    )
    """,
]

MIGRATION_SQL = [
    "ALTER TABLE prices_daily ADD COLUMN IF NOT EXISTS source VARCHAR",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS market_cap DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS per DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS pbr DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS eps DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS bps DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS dividend_yield DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS market_cap_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS value_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS quality_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS supply_demand_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS sentiment_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_net_value_1d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_net_value_5d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_net_value_20d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_net_value_60d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_net_value_1d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_net_value_5d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_net_value_20d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_net_value_60d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS retail_net_value_1d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS retail_net_value_5d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS retail_net_value_20d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS retail_net_value_60d_sum DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_net_20d_to_mcap DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_net_20d_to_value DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS retail_overheat_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS foreign_consecutive_buy_days INTEGER",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS institution_consecutive_buy_days INTEGER",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS consensus_upside_pct DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS consensus_momentum_30_90 DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS target_up_count_30d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS target_down_count_30d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS new_coverage_count_30d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS target_revision_balance_30d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS consensus_revision_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS target_upside_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS analyst_reliability_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS weighted_analyst_reliability_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_return_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_volume_ratio_20d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS ewy_return_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_ewy_spread_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_leverage_drift_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_impact_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS koru_market_shock_flag DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS kospi_return_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS kosdaq_return_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_attention_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_sentiment_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_urgency_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_risk_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_semiconductor_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS telegram_macro_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_return_1d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_return_5d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_zscore_20d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_beta_60d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_corr_60d DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_impact_score DOUBLE",
    "ALTER TABLE features_daily ADD COLUMN IF NOT EXISTS us_sector_direction_agreement DOUBLE",
    "ALTER TABLE consensus_history ADD COLUMN IF NOT EXISTS consensus_momentum_30_90 DOUBLE",
    "ALTER TABLE consensus_history ADD COLUMN IF NOT EXISTS target_revision_balance_30d DOUBLE",
    "ALTER TABLE consensus_history ADD COLUMN IF NOT EXISTS target_upside_score DOUBLE",
    "ALTER TABLE consensus_history ADD COLUMN IF NOT EXISTS analyst_reliability_score DOUBLE",
    "ALTER TABLE consensus_history ADD COLUMN IF NOT EXISTS weighted_analyst_reliability_score DOUBLE",
    "ALTER TABLE model_predictions ADD COLUMN IF NOT EXISTS model_version VARCHAR",
    "ALTER TABLE model_predictions ADD COLUMN IF NOT EXISTS risk_score DOUBLE",
    "ALTER TABLE model_predictions ADD COLUMN IF NOT EXISTS recommendation_score DOUBLE",
    "ALTER TABLE dashboard_snapshot ADD COLUMN IF NOT EXISTS focus_stock_json VARCHAR",
    "ALTER TABLE dashboard_snapshot ADD COLUMN IF NOT EXISTS cluster_data_json VARCHAR",
    "ALTER TABLE dashboard_snapshot ADD COLUMN IF NOT EXISTS sector_ranking_json VARCHAR",
    "ALTER TABLE dashboard_snapshot ADD COLUMN IF NOT EXISTS data_quality_json VARCHAR",
    "ALTER TABLE market_regime_daily ADD COLUMN IF NOT EXISTS signals_json VARCHAR",
    "ALTER TABLE market_regime_daily ADD COLUMN IF NOT EXISTS reasons_json VARCHAR",
    "ALTER TABLE market_regime_daily ADD COLUMN IF NOT EXISTS futures_score DOUBLE",
    "ALTER TABLE labels ADD COLUMN IF NOT EXISTS is_bottom20pct BOOLEAN",
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS pred_prob_bottom20 DOUBLE",
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS long_score DOUBLE",
    "ALTER TABLE predictions ADD COLUMN IF NOT EXISTS short_score DOUBLE",
    "ALTER TABLE long_short_recommendations ADD COLUMN IF NOT EXISTS market VARCHAR",
    "ALTER TABLE long_short_backtest_results ADD COLUMN IF NOT EXISTS market VARCHAR",
    "ALTER TABLE market_move_explanations ADD COLUMN IF NOT EXISTS name VARCHAR",
    "ALTER TABLE market_move_explanations ADD COLUMN IF NOT EXISTS prediction_context_json VARCHAR",
    "ALTER TABLE market_move_explanations ADD COLUMN IF NOT EXISTS market_index_trigger_json VARCHAR",
    "ALTER TABLE today_market_snapshot ADD COLUMN IF NOT EXISTS move_explanations_json VARCHAR",
    "ALTER TABLE today_market_snapshot ADD COLUMN IF NOT EXISTS market_outlook_json VARCHAR",
]


def connect_database(path: str | Path, read_only: bool = False, initialize_schema: bool = True):
    import duckdb

    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(db_path), read_only=read_only)
    if initialize_schema and not read_only:
        ensure_schema(conn)
    return conn


def ensure_schema(conn) -> None:
    for statement in SCHEMA_SQL:
        conn.execute(statement)
    for statement in MIGRATION_SQL:
        conn.execute(statement)


def replace_table(conn, table: str, df: pd.DataFrame) -> None:
    df = align_to_table_columns(conn, table, df)
    temp_name = f"tmp_{table}_{uuid4().hex}"
    conn.register(temp_name, df)
    try:
        conn.execute(f"DELETE FROM {table}")
        columns = ", ".join(df.columns)
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp_name}")
    finally:
        conn.unregister(temp_name)


def append_dedup_table(
    conn,
    table: str,
    df: pd.DataFrame,
    key_columns: list[str],
) -> None:
    if df.empty:
        return
    df = align_to_table_columns(conn, table, df)
    temp_name = f"tmp_{table}_{uuid4().hex}"
    conn.register(temp_name, df)
    join_condition = " AND ".join([f"{table}.{col} = {temp_name}.{col}" for col in key_columns])
    try:
        conn.execute(f"DELETE FROM {table} USING {temp_name} WHERE {join_condition}")
        columns = ", ".join(df.columns)
        conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp_name}")
    finally:
        conn.unregister(temp_name)


def align_to_table_columns(conn, table: str, df: pd.DataFrame) -> pd.DataFrame:
    """Align a DataFrame to a DuckDB table, adding missing columns as nulls."""
    columns = get_table_columns(conn, table)
    if not columns:
        return df
    aligned = df.copy()
    for column in columns:
        if column not in aligned.columns:
            aligned[column] = None
    return aligned[columns]


def get_table_columns(conn, table: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = ?
        ORDER BY ordinal_position
        """,
        [table],
    ).fetchall()
    return [row[0] for row in rows]


def table_exists(conn, table: str) -> bool:
    result = conn.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [table]
    ).fetchone()[0]
    return bool(result)


def read_table(conn, table: str) -> pd.DataFrame:
    if not table_exists(conn, table):
        return pd.DataFrame()
    return conn.execute(f"SELECT * FROM {table}").fetchdf()

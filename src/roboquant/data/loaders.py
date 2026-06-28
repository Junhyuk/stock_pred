from __future__ import annotations

import pandas as pd

from roboquant.db import table_exists


def load_prices(conn, symbols: list[str] | None = None) -> pd.DataFrame:
    query = "SELECT * FROM prices_daily"
    params: list[object] = []
    if symbols:
        normalized = [str(symbol).zfill(6) for symbol in symbols]
        placeholders = ", ".join(["?"] * len(normalized))
        query += f" WHERE symbol IN ({placeholders})"
        params.extend(normalized)
    query += " ORDER BY symbol, date"
    return conn.execute(query, params).fetchdf()


def load_benchmark(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM benchmark_daily ORDER BY date").fetchdf()


def load_symbols(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM symbols ORDER BY symbol").fetchdf()


def load_current_prediction_universe_symbols(conn, universe_rule: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT symbol
        FROM current_prediction_universe
        WHERE universe_rule = ?
        ORDER BY market, prediction_rank, symbol
        """,
        [universe_rule],
    ).fetchall()
    return [str(row[0]).zfill(6) for row in rows]


def load_market_metrics(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM market_metrics_daily ORDER BY symbol, date").fetchdf()


def load_investor_flows(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM investor_flows_daily ORDER BY symbol, date").fetchdf()


def load_collection_failures(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM collection_failures ORDER BY collected_at DESC").fetchdf()


def load_analyst_reports(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM analyst_reports ORDER BY symbol, report_date").fetchdf()


def load_analyst_report_outcomes(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM analyst_report_outcomes ORDER BY symbol, report_date").fetchdf()


def load_analyst_scores(conn) -> pd.DataFrame:
    return conn.execute(
        "SELECT * FROM analyst_scores ORDER BY broker_name, analyst_name, as_of_date"
    ).fetchdf()


def load_consensus_history(conn) -> pd.DataFrame:
    return conn.execute("SELECT * FROM consensus_history ORDER BY symbol, date").fetchdf()


def load_koru_linkage(conn) -> pd.DataFrame:
    if not table_exists(conn, "koru_korea_linkage"):
        return pd.DataFrame()
    return conn.execute("SELECT * FROM koru_korea_linkage ORDER BY trade_date").fetchdf()


def load_telegram_market_signals(conn) -> pd.DataFrame:
    if not table_exists(conn, "telegram_market_signal_daily"):
        return pd.DataFrame()
    return conn.execute("SELECT * FROM telegram_market_signal_daily ORDER BY signal_date").fetchdf()


def load_us_sector_linkage(conn) -> pd.DataFrame:
    if not table_exists(conn, "us_sector_linkage_daily"):
        return pd.DataFrame()
    return conn.execute("SELECT * FROM us_sector_linkage_daily ORDER BY trade_date, domestic_sector").fetchdf()


def load_features(conn, horizon: str | None = None) -> pd.DataFrame:
    query = "SELECT * FROM features_daily"
    params: list[object] = []
    if horizon:
        query += " WHERE horizon = ?"
        params.append(horizon)
    query += " ORDER BY date, symbol"
    return conn.execute(query, params).fetchdf()


def load_labels(conn, horizon: str | None = None) -> pd.DataFrame:
    query = "SELECT * FROM labels"
    params: list[object] = []
    if horizon:
        query += " WHERE horizon = ?"
        params.append(horizon)
    query += " ORDER BY asof_date, symbol"
    return conn.execute(query, params).fetchdf()


def load_modeling_dataset(conn, horizon: str | None = None) -> pd.DataFrame:
    where = ""
    params: list[object] = []
    if horizon:
        where = "WHERE f.horizon = ?"
        params.append(horizon)
    return conn.execute(
        f"""
        SELECT
          f.*,
          l.future_return,
          l.benchmark_return,
          l.excess_return,
          l.rank_quantile,
          l.is_top20pct,
          l.is_bottom20pct,
          l.max_drawdown_forward
        FROM features_daily AS f
        INNER JOIN labels AS l
          ON f.date = l.asof_date
         AND f.symbol = l.symbol
         AND f.horizon = l.horizon
        {where}
        ORDER BY f.date, f.symbol
        """,
        params,
    ).fetchdf()


def load_prediction_dataset(conn, horizon: str) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT
          p.*,
          COALESCE(u.market, s.market) AS market,
          COALESCE(u.name, s.name) AS name,
          s.sector AS sector,
          f.ret_21d,
          f.ret_63d,
          f.ret_126d,
          f.ret_252d,
          f.ma_gap_20d,
          f.ma_gap_60d,
          f.ma_gap_120d,
          f.ma_gap_250d,
          f.volatility_20d,
          f.volatility_60d,
          f.volume_ratio_20d,
          f.trading_value_ma20,
          f.close_to_52w_high,
          f.rsi_14,
          f.momentum_score,
          f.volatility_score,
          f.liquidity_score,
          f.risk_score,
          f.market_cap,
          f.per,
          f.pbr,
          f.eps,
          f.bps,
          f.dividend_yield,
          f.market_cap_score,
          f.value_score,
          f.quality_score,
          f.supply_demand_score,
          f.sentiment_score,
          f.foreign_net_value_20d_sum,
          f.institution_net_value_20d_sum,
          f.retail_net_value_20d_sum,
          f.foreign_net_20d_to_mcap,
          f.institution_net_20d_to_value,
          f.retail_overheat_score,
          f.foreign_consecutive_buy_days,
          f.institution_consecutive_buy_days,
          f.consensus_upside_pct,
          f.consensus_momentum_30_90,
          f.target_up_count_30d,
          f.target_down_count_30d,
          f.new_coverage_count_30d,
          f.target_revision_balance_30d,
          f.consensus_revision_score,
          f.target_upside_score,
          f.analyst_reliability_score,
          f.weighted_analyst_reliability_score,
          l.future_return,
          l.benchmark_return,
          l.excess_return,
          l.is_top20pct,
          l.is_bottom20pct
        FROM predictions AS p
        INNER JOIN features_daily AS f
          ON p.asof_date = f.date
         AND p.symbol = f.symbol
         AND p.horizon = f.horizon
        LEFT JOIN labels AS l
          ON p.asof_date = l.asof_date
         AND p.symbol = l.symbol
         AND p.horizon = l.horizon
        LEFT JOIN current_prediction_universe AS u
          ON p.symbol = u.symbol
         AND u.universe_rule = 'prediction_top_market_cap'
        LEFT JOIN symbols AS s
          ON p.symbol = s.symbol
        WHERE p.horizon = ?
        ORDER BY p.asof_date, p.symbol
        """,
        [horizon],
    ).fetchdf()


def load_latest_features(conn, horizon: str, asof_date: str | None = None) -> pd.DataFrame:
    if asof_date and asof_date != "latest":
        return conn.execute(
            "SELECT * FROM features_daily WHERE horizon = ? AND date = ? ORDER BY symbol",
            [horizon, asof_date],
        ).fetchdf()
    latest = conn.execute(
        "SELECT MAX(date) FROM features_daily WHERE horizon = ?", [horizon]
    ).fetchone()[0]
    if latest is None:
        return pd.DataFrame()
    return conn.execute(
        "SELECT * FROM features_daily WHERE horizon = ? AND date = ? ORDER BY symbol",
        [horizon, latest],
    ).fetchdf()

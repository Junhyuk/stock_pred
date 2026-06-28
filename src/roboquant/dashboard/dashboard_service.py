from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from roboquant.dashboard.portfolio_service import build_portfolio_from_recommendations
from roboquant.dashboard.price_gap_service import build_prediction_price_gap
from roboquant.data.freshness import price_freshness_report
from roboquant.db import append_dedup_table, table_exists
from roboquant.koru import get_latest_koru_linkage
from roboquant.market_outlook import (
    get_market_outlook as _get_market_outlook_payload,
    market_outlook_holidays,
    target_dates_for_run,
)
from roboquant.signals.market_move_explanations import build_market_move_explanations
from roboquant.signals.market_news_context import get_recent_market_news
from roboquant.us_sector_linkage import (
    get_sector_linkage as _get_sector_linkage_payload,
    normalize_domestic_sector,
)

DISCLAIMER = (
    "본 서비스는 투자 참고용 정보이며, 투자 판단과 책임은 사용자 본인에게 있습니다. "
    "과거 수익률과 백테스트 결과가 미래 수익을 보장하지 않습니다."
)
FOCUS_DEMO_SYMBOLS = ("005930", "000660", "005850")
FOUR_STOCK_DEMO_SYMBOLS = ("005930", "000660", "066570", "005850")
DEFAULT_TODAY_FOCUS_STOCKS = [
    {"symbol": "005930", "name": "삼성전자"},
    {"symbol": "000660", "name": "SK하이닉스"},
    {"symbol": "005850", "name": "에스엘"},
]
REGIME_SCORE_FIELDS = (
    ("us_equity_score", "US Equity"),
    ("semiconductor_score", "Semiconductor"),
    ("futures_score", "Futures"),
    ("volatility_score", "Volatility"),
    ("rate_score", "Rates"),
    ("fx_score", "FX"),
    ("asia_score", "Asia"),
    ("commodity_score", "Commodity"),
)
GLOBAL_SENSITIVITY = {
    "005930": ["SOX", "Nasdaq", "TSM", "USD/KRW"],
    "000660": ["SOX", "Nasdaq", "TSM", "USD/KRW"],
    "066570": ["SOX", "Nasdaq", "TSM", "USD/KRW"],
    "005850": ["Dow", "S&P500", "USD/KRW", "Nikkei"],
}
REGIME_WEIGHT_CAP = {
    "risk_on": 0.15,
    "neutral": 0.12,
    "risk_off": 0.08,
    "panic": 0.05,
}
TODAY_FRESHNESS_DEFAULTS = {
    "max_yahoo_age_days": 3,
    "max_macro_news_age_hours": 24,
    "max_supply_age_days": 5,
}
NEXT_DAY_RANGE_MULTIPLIER = 1.25


def build_dashboard_snapshot(conn, horizon: str = "3M") -> dict[str, Any]:
    recommendations = _latest_recommendations(conn, horizon)
    performance = _model_accuracy(conn)
    backtest_summary = _backtest_summary(conn, horizon)
    focus_stock = get_focus_stock(conn, "005930", horizon)
    cluster_data = get_clusters(conn, horizon)
    sector_ranking = build_sector_ranking(recommendations)
    data_quality = build_data_quality(conn)
    snapshot_date = _snapshot_date(recommendations, performance)
    snapshot = {
        "snapshot_date": str(snapshot_date),
        "disclaimer": DISCLAIMER,
        "position_summary": build_position_summary(recommendations, performance),
        "theme_data": build_theme_data(recommendations),
        "ai_recommendations": build_ai_recommendations(recommendations),
        "core_portfolio": build_core_portfolio(recommendations),
        "quant_portfolio": build_portfolio_from_recommendations(recommendations, "neutral"),
        "qual_portfolio": build_qual_portfolio(conn),
        "upside_ranking": build_upside_ranking(recommendations),
        "analyst_reports": build_analyst_reports(conn),
        "backtest_summary": backtest_summary,
        "model_accuracy": performance.to_dict(orient="records"),
        "focus_stock": focus_stock,
        "cluster_data": cluster_data,
        "sector_ranking": sector_ranking,
        "data_quality": data_quality,
    }
    row = pd.DataFrame(
        [
            {
                "snapshot_date": snapshot_date,
                "position_summary_json": _json(snapshot["position_summary"]),
                "theme_data_json": _json(snapshot["theme_data"]),
                "ai_recommendations_json": _json(snapshot["ai_recommendations"]),
                "core_portfolio_json": _json(snapshot["core_portfolio"]),
                "quant_portfolio_json": _json(snapshot["quant_portfolio"]),
                "qual_portfolio_json": _json(snapshot["qual_portfolio"]),
                "upside_ranking_json": _json(snapshot["upside_ranking"]),
                "analyst_reports_json": _json(snapshot["analyst_reports"]),
                "backtest_summary_json": _json(snapshot["backtest_summary"]),
                "model_accuracy_json": _json(snapshot["model_accuracy"]),
                "focus_stock_json": _json(snapshot["focus_stock"]),
                "cluster_data_json": _json(snapshot["cluster_data"]),
                "sector_ranking_json": _json(snapshot["sector_ranking"]),
                "data_quality_json": _json(snapshot["data_quality"]),
                "created_at": _utcnow(),
            }
        ]
    )
    append_dedup_table(conn, "dashboard_snapshot", row, ["snapshot_date"])
    return snapshot


def get_latest_dashboard_snapshot(conn) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM dashboard_snapshot ORDER BY snapshot_date DESC LIMIT 1"
    ).fetchdf()
    if row.empty:
        return empty_dashboard_snapshot()
    data = row.iloc[0].to_dict()
    return {
        "snapshot_date": _date_string(data.get("snapshot_date")),
        "disclaimer": DISCLAIMER,
        "position_summary": _loads(data.get("position_summary_json"), {}),
        "theme_data": _loads(data.get("theme_data_json"), {}),
        "ai_recommendations": _loads(data.get("ai_recommendations_json"), []),
        "core_portfolio": _loads(data.get("core_portfolio_json"), []),
        "quant_portfolio": _loads(data.get("quant_portfolio_json"), {}),
        "qual_portfolio": _loads(data.get("qual_portfolio_json"), {}),
        "upside_ranking": _loads(data.get("upside_ranking_json"), []),
        "analyst_reports": _loads(data.get("analyst_reports_json"), []),
        "backtest_summary": _loads(data.get("backtest_summary_json"), {}),
        "model_accuracy": _loads(data.get("model_accuracy_json"), []),
        "focus_stock": _loads(data.get("focus_stock_json"), {}),
        "cluster_data": _loads(data.get("cluster_data_json"), []),
        "sector_ranking": _loads(data.get("sector_ranking_json"), []),
        "data_quality": _loads(data.get("data_quality_json"), {}),
    }


def empty_dashboard_snapshot() -> dict[str, Any]:
    today = _local_today()
    return {
        "snapshot_date": str(today),
        "disclaimer": DISCLAIMER,
        "position_summary": build_position_summary(pd.DataFrame(), pd.DataFrame()),
        "theme_data": {"sectors": [], "top_sector": None},
        "ai_recommendations": [],
        "core_portfolio": [],
        "quant_portfolio": {"profile": "neutral", "cash_ratio": 0.15, "items": []},
        "qual_portfolio": {"theme": "애널리스트/공시 기반 정성 후보", "items": []},
        "upside_ranking": [],
        "analyst_reports": [],
        "backtest_summary": {
            "horizon": "3M",
            "horizon_days": 60,
            "sample_count": 0,
            "hit_ratio": None,
            "precision_top20": None,
            "avg_excess_return": None,
            "mdd": None,
            "sharpe": None,
            "rank_ic": None,
            "weekly_returns": [],
        },
        "model_accuracy": [],
        "focus_stock": {},
        "cluster_data": [],
        "sector_ranking": [],
        "data_quality": {},
    }


def get_backtest_summary(conn, horizon: str | int = "3M") -> dict[str, Any]:
    return _backtest_summary(conn, _normalize_horizon(horizon))


def get_model_accuracy(conn) -> list[dict[str, Any]]:
    return _records(_model_accuracy(conn))


def get_backtest_by_model(
    conn,
    model: str,
    version: str | None = None,
    horizon: str | int = "3M",
) -> list[dict[str, Any]]:
    query = """
        SELECT *
        FROM backtest_results
        WHERE horizon = ?
          AND model_name = ?
    """
    params: list[Any] = [_normalize_horizon(horizon), model]
    if version:
        query += " AND model_version = ?"
        params.append(version)
    query += " ORDER BY prediction_date DESC, rank_no LIMIT 500"
    return _records(conn.execute(query, params).fetchdf())


def get_stock_backtest(conn, symbol: str, horizon: str | int | None = None) -> list[dict[str, Any]]:
    symbol = str(symbol).zfill(6)
    query = """
        SELECT *
        FROM backtest_results
        WHERE symbol = ?
    """
    params: list[Any] = [symbol]
    if horizon is not None:
        query += " AND horizon = ?"
        params.append(_normalize_horizon(horizon))
    query += " ORDER BY prediction_date DESC, model_name, rank_no LIMIT 500"
    return _records(conn.execute(query, params).fetchdf())


def get_top20_backtest(conn, horizon: str | int = "3M") -> list[dict[str, Any]]:
    return _records(
        conn.execute(
            """
            SELECT b.*, s.name, s.market
            FROM backtest_results AS b
            LEFT JOIN symbols AS s ON b.symbol = s.symbol
            WHERE b.horizon = ?
              AND b.is_top20 = TRUE
            ORDER BY b.prediction_date DESC, b.model_name, b.rank_no
            LIMIT 500
            """,
            [_normalize_horizon(horizon)],
        ).fetchdf()
    )


def get_sector_backtest(conn, sector: str, horizon: str | int = "3M") -> dict[str, Any]:
    frame = conn.execute(
        """
        SELECT b.*, COALESCE(b.sector, s.sector, s.market, '기타') AS resolved_sector
        FROM backtest_results AS b
        LEFT JOIN symbols AS s ON b.symbol = s.symbol
        WHERE b.horizon = ?
          AND COALESCE(b.sector, s.sector, s.market, '기타') = ?
        ORDER BY b.prediction_date DESC, b.model_name, b.rank_no
        LIMIT 500
        """,
        [_normalize_horizon(horizon), sector],
    ).fetchdf()
    if frame.empty:
        return {"sector": sector, "horizon": _normalize_horizon(horizon), "items": [], "summary": {}}
    return {
        "sector": sector,
        "horizon": _normalize_horizon(horizon),
        "summary": {
            "sample_count": int(len(frame)),
            "hit_ratio": _safe_float((pd.to_numeric(frame["actual_return"], errors="coerce") > 0).mean()),
            "avg_excess_return": _safe_float(pd.to_numeric(frame["excess_return"], errors="coerce").mean()),
            "precision_top20": _safe_float(
                (pd.to_numeric(frame[frame["rank_no"] <= 20]["actual_return"], errors="coerce") > 0).mean()
            ),
        },
        "items": _records(frame),
    }


def get_prediction_history(conn, symbol: str, horizon: str | int | None = None) -> list[dict[str, Any]]:
    symbol = str(symbol).zfill(6)
    horizon_filter = "" if horizon is None else " AND horizon = ?"
    params: list[Any] = [symbol]
    if horizon is not None:
        params.append(_normalize_horizon(horizon))
    production = conn.execute(
        f"""
        SELECT
          asof_date AS prediction_date,
          symbol,
          horizon,
          'lightgbm' AS model_name,
          COALESCE(model_version, 'unknown') AS model_version,
          pred_return AS predicted_return,
          pred_prob_top20 AS predicted_probability,
          pred_risk AS risk_score,
          pred_prob_top20 AS recommendation_score,
          NULL AS rank_no,
          'production' AS source
        FROM predictions
        WHERE symbol = ?
        {horizon_filter}
        """,
        params,
    ).fetchdf()
    shadow = conn.execute(
        f"""
        SELECT
          date AS prediction_date,
          symbol,
          horizon,
          model_name,
          COALESCE(model_version, model_name) AS model_version,
          pred_score AS predicted_return,
          pred_prob AS predicted_probability,
          risk_score,
          COALESCE(recommendation_score, pred_prob) AS recommendation_score,
          rank AS rank_no,
          'shadow' AS source
        FROM model_predictions
        WHERE symbol = ?
        {horizon_filter}
        """,
        params,
    ).fetchdf()
    frame = pd.concat([production, shadow], ignore_index=True)
    if frame.empty:
        return []
    return _records(frame.sort_values(["prediction_date", "model_name"], ascending=[False, True]))


def get_top20_recommendations(conn, horizon: str = "3M") -> list[dict[str, Any]]:
    return build_ai_recommendations(_latest_recommendations(conn, horizon))


def get_top20_upside_recommendations(conn, horizon: str = "3M", limit: int = 20) -> dict[str, Any]:
    recommendations = _latest_recommendations(conn, horizon)
    items = build_top20_upside_recommendations(recommendations, limit=limit)
    return {
        "horizon": _normalize_horizon(horizon),
        "asof_date": None if recommendations.empty else _date_string(recommendations["asof_date"].max()),
        "limit": int(max(1, min(int(limit), 100))),
        "summary": _top20_upside_summary(items),
        "items": items,
        "disclaimer": DISCLAIMER,
    }


def get_top50_universe(
    conn,
    *,
    horizon: str = "3M",
    universe_rule: str = "prediction_top_market_cap",
) -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    if not table_exists(conn, "prediction_universe_snapshot"):
        return {
            "horizon": horizon,
            "universe_rule": universe_rule,
            "snapshot_date": None,
            "status": "not_collected",
            "summary": {"total": 0, "kospi": 0, "kosdaq": 0, "with_prediction": 0, "with_price": 0},
            "items": [],
            "disclaimer": DISCLAIMER,
        }
    frame = conn.execute(
        """
        WITH universe AS (
            SELECT *
            FROM current_prediction_universe
            WHERE universe_rule = ?
              AND is_enabled = TRUE
        ),
        latest_prices AS (
            SELECT
                p.symbol,
                p.date AS price_date,
                p.close,
                p.volume,
                p.source AS price_source,
                ROW_NUMBER() OVER (
                    PARTITION BY p.symbol
                    ORDER BY p.date DESC, p.collected_at DESC NULLS LAST
                ) AS row_number
            FROM prices_daily AS p
            INNER JOIN universe AS u ON p.symbol = u.symbol
        ),
        latest_predictions AS (
            SELECT
                p.symbol,
                p.asof_date,
                p.pred_prob_top20,
                p.pred_return,
                p.model_version,
                ROW_NUMBER() OVER (
                    PARTITION BY p.symbol
                    ORDER BY p.asof_date DESC, p.model_version DESC
                ) AS row_number
            FROM predictions AS p
            INNER JOIN universe AS u ON p.symbol = u.symbol
            WHERE p.horizon = ?
        ),
        latest_recommendations AS (
            SELECT
                r.symbol,
                r.rank AS recommendation_rank,
                r.final_score,
                ROW_NUMBER() OVER (
                    PARTITION BY r.symbol
                    ORDER BY r.asof_date DESC, r.rank ASC
                ) AS row_number
            FROM recommendations AS r
            INNER JOIN universe AS u ON r.symbol = u.symbol
            WHERE r.horizon = ?
        )
        SELECT
            u.snapshot_date,
            u.symbol,
            u.name,
            u.market,
            u.prediction_rank,
            u.raw_market_cap_rank,
            u.market_cap,
            u.refresh_provider,
            u.refresh_status,
            s.sector,
            lp.price_date,
            lp.close,
            lp.volume,
            lp.price_source,
            pred.asof_date AS prediction_date,
            pred.pred_prob_top20,
            pred.pred_return,
            pred.model_version,
            rec.recommendation_rank,
            rec.final_score
        FROM universe AS u
        LEFT JOIN symbols AS s ON u.symbol = s.symbol
        LEFT JOIN latest_prices AS lp
          ON u.symbol = lp.symbol
         AND lp.row_number = 1
        LEFT JOIN latest_predictions AS pred
          ON u.symbol = pred.symbol
         AND pred.row_number = 1
        LEFT JOIN latest_recommendations AS rec
          ON u.symbol = rec.symbol
         AND rec.row_number = 1
        ORDER BY u.market, u.prediction_rank NULLS LAST, u.symbol
        """,
        [universe_rule, horizon, horizon],
    ).fetchdf()
    items = _records(frame)
    snapshot_date = None if frame.empty else _date_string(frame["snapshot_date"].max())
    summary = {
        "total": len(items),
        "kospi": sum(1 for item in items if item.get("market") == "KOSPI"),
        "kosdaq": sum(1 for item in items if item.get("market") == "KOSDAQ"),
        "with_prediction": sum(1 for item in items if item.get("pred_prob_top20") is not None),
        "with_price": sum(1 for item in items if item.get("close") is not None),
    }
    status = "not_collected"
    if summary["total"] == 50:
        status = "ready"
    elif summary["total"] > 0:
        status = "partial_ready"
    return {
        "horizon": horizon,
        "universe_rule": universe_rule,
        "snapshot_date": snapshot_date,
        "status": status,
        "summary": summary,
        "items": items,
        "disclaimer": "Top50 유니버스는 KOSPI 30 + KOSDAQ 20 시가총액 기준입니다. 투자 판단과 책임은 사용자 본인에게 있습니다.",
    }


def get_clusters(conn, horizon: str = "3M") -> list[dict[str, Any]]:
    horizon = _normalize_horizon(horizon)
    frame = conn.execute(
        """
        SELECT
          c.asof_date,
          c.horizon,
          c.cluster_id,
          c.cluster_label,
          c.member_count,
          c.centroid_json,
          c.top_symbols_json,
          c.model_version
        FROM cluster_summary AS c
        WHERE c.horizon = ?
          AND c.asof_date = (SELECT MAX(asof_date) FROM cluster_summary WHERE horizon = ?)
        ORDER BY c.cluster_id
        """,
        [horizon, horizon],
    ).fetchdf()
    return _records(frame)


def get_cluster_members(conn, cluster_id: int, horizon: str = "3M") -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    frame = conn.execute(
        """
        SELECT c.*, s.name, s.market, s.sector
        FROM stock_clusters AS c
        LEFT JOIN symbols AS s ON c.symbol = s.symbol
        WHERE c.horizon = ?
          AND c.cluster_id = ?
          AND c.asof_date = (SELECT MAX(asof_date) FROM stock_clusters WHERE horizon = ?)
        ORDER BY c.distance_to_centroid
        """,
        [horizon, int(cluster_id), horizon],
    ).fetchdf()
    return {
        "cluster_id": int(cluster_id),
        "horizon": horizon,
        "cluster_label": None if frame.empty else frame.iloc[0]["cluster_label"],
        "items": _records(frame),
    }


def get_stock_cluster(conn, symbol: str, horizon: str = "3M") -> dict[str, Any]:
    symbol = str(symbol).zfill(6)
    horizon = _normalize_horizon(horizon)
    row = conn.execute(
        """
        SELECT c.*, s.name, s.market, s.sector
        FROM stock_clusters AS c
        LEFT JOIN symbols AS s ON c.symbol = s.symbol
        WHERE c.symbol = ?
          AND c.horizon = ?
          AND c.asof_date = (SELECT MAX(asof_date) FROM stock_clusters WHERE horizon = ?)
        LIMIT 1
        """,
        [symbol, horizon, horizon],
    ).fetchdf()
    if row.empty:
        return {"symbol": symbol, "horizon": horizon, "cluster": None, "peers": []}
    cluster = row.iloc[0].to_dict()
    members = get_cluster_members(conn, int(cluster["cluster_id"]), horizon)["items"]
    peers = [item for item in members if item.get("symbol") != symbol][:10]
    return {"symbol": symbol, "horizon": horizon, "cluster": _json_safe(cluster), "peers": peers}


def get_focus_stock(conn, symbol: str = "005930", horizon: str = "3M") -> dict[str, Any]:
    symbol = str(symbol).zfill(6)
    horizon = _normalize_horizon(horizon)
    stock = conn.execute("SELECT * FROM symbols WHERE symbol = ? LIMIT 1", [symbol]).fetchdf()
    price = conn.execute(
        """
        SELECT date, close, volume, trading_value, source
        FROM prices_daily
        WHERE symbol = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol],
    ).fetchdf()
    predictions = conn.execute(
        """
        SELECT *
        FROM predictions
        WHERE horizon = ?
          AND asof_date = (SELECT MAX(asof_date) FROM predictions WHERE horizon = ?)
        """,
        [horizon, horizon],
    ).fetchdf()
    prediction = {}
    if not predictions.empty:
        predictions["rank"] = predictions["pred_prob_top20"].rank(ascending=False, method="first")
        selected = predictions[predictions["symbol"].astype(str).str.zfill(6).eq(symbol)]
        if not selected.empty:
            prediction = _json_safe(selected.sort_values("pred_prob_top20", ascending=False).iloc[0].to_dict())
    recs = _latest_recommendations(conn, horizon)
    selected_rec = recs[recs["symbol"].astype(str).str.zfill(6).eq(symbol)] if not recs.empty else recs
    feature = conn.execute(
        """
        SELECT *
        FROM features_daily
        WHERE symbol = ? AND horizon = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol, horizon],
    ).fetchdf()
    sector_value = None if stock.empty else stock.iloc[0].get("sector")
    return {
        "symbol": symbol,
        "name": None if stock.empty else stock.iloc[0].get("name"),
        "market": None if stock.empty else stock.iloc[0].get("market"),
        "sector": sector_value,
        "latest_price": {} if price.empty else _json_safe(price.iloc[0].to_dict()),
        "prediction": prediction,
        "is_top20": not selected_rec.empty,
        "recommendation": {} if selected_rec.empty else _records(selected_rec.head(1))[0],
        "features": {} if feature.empty else _json_safe(feature.iloc[0].to_dict()),
        "sector_linkage": _sector_linkage_for_stock(conn, sector_value),
        "cluster": get_stock_cluster(conn, symbol, horizon),
        "data_status": "데이터 미수집" if stock.empty or price.empty else "실데이터",
        "disclaimer": DISCLAIMER,
    }


def build_sector_ranking(recommendations: pd.DataFrame) -> list[dict[str, Any]]:
    if recommendations.empty:
        return []
    frame = recommendations.copy()
    frame["sector"] = frame["sector"].fillna("기타")
    grouped = (
        frame.groupby("sector")
        .agg(
            average_score=("final_score", "mean"),
            recommendation_count=("symbol", "count"),
            average_risk=("risk_score", "mean"),
        )
        .reset_index()
        .sort_values(["average_score", "recommendation_count"], ascending=[False, False])
    )
    return _records(grouped)


def build_data_quality(conn) -> dict[str, Any]:
    counts = {}
    for table in (
        "symbols",
        "prices_daily",
        "features_daily",
        "labels",
        "predictions",
        "recommendations",
        "stock_clusters",
    ):
        counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    source_rows = conn.execute(
        "SELECT COALESCE(source, 'unknown') AS source, COUNT(*) AS rows FROM prices_daily GROUP BY source"
    ).fetchdf()
    latest_price_date = conn.execute("SELECT MAX(date) FROM prices_daily").fetchone()[0]
    neutral_factors = counts["features_daily"] > 0 and conn.execute(
        "SELECT COUNT(*) FROM investor_flows_daily"
    ).fetchone()[0] == 0
    return {
        "counts": counts,
        "price_sources": _records(source_rows),
        "latest_price_date": _date_string(latest_price_date),
        "neutral_factor_defaults_applied": bool(neutral_factors),
    }


def get_stock_detail(conn, symbol: str, horizon: str = "3M") -> dict[str, Any]:
    symbol = str(symbol).zfill(6)
    horizon = _normalize_horizon(horizon)
    symbol_row = conn.execute(
        "SELECT * FROM symbols WHERE symbol = ? LIMIT 1",
        [symbol],
    ).fetchdf()
    prices = conn.execute(
        "SELECT date, open, high, low, close, volume FROM prices_daily WHERE symbol = ? ORDER BY date DESC LIMIT 260",
        [symbol],
    ).fetchdf()
    features = conn.execute(
        """
        SELECT *
        FROM features_daily
        WHERE symbol = ? AND horizon = ?
        ORDER BY date DESC
        LIMIT 1
        """,
        [symbol, horizon],
    ).fetchdf()
    recs = _latest_recommendations(conn, horizon)
    rec = recs[recs["symbol"].astype(str).str.zfill(6) == symbol]
    prediction = _latest_prediction_for_symbol(conn, symbol, horizon)
    reports = conn.execute(
        """
        SELECT report_date, broker_name, analyst_name, report_title, investment_rating,
               target_price, target_change_pct, upside_pct_at_report, source_name
        FROM analyst_reports
        WHERE symbol = ?
        ORDER BY report_date DESC
        LIMIT 20
        """,
        [symbol],
    ).fetchdf()
    backtest = conn.execute(
        """
        SELECT *
        FROM backtest_results
        WHERE symbol = ? AND horizon = ?
        ORDER BY prediction_date DESC
        LIMIT 50
        """,
        [symbol, horizon],
    ).fetchdf()
    ordered_prices = prices.sort_values("date")
    return {
        "symbol": symbol,
        "name": None if symbol_row.empty else symbol_row.iloc[0].get("name"),
        "market": None if symbol_row.empty else symbol_row.iloc[0].get("market"),
        "sector": None if symbol_row.empty else symbol_row.iloc[0].get("sector"),
        "horizon": horizon,
        "prediction": prediction,
        "is_top20": not rec.empty,
        "recommendation": {} if rec.empty else _records(rec.head(1))[0],
        "latest_price": {} if ordered_prices.empty else _records(ordered_prices.tail(1))[0],
        "features": {} if features.empty else _records(features)[0],
        "chart": _records(ordered_prices),
        "analyst_reports": _records(reports),
        "backtest": _records(backtest),
        "disclaimer": DISCLAIMER,
    }


def get_two_stock_demo(
    conn,
    symbols: tuple[str, str] = ("005930", "005850"),
    horizon: str = "3M",
) -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    items = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).zfill(6)
        detail = get_stock_detail(conn, symbol=symbol, horizon=horizon)
        history = get_prediction_history(conn, symbol=symbol, horizon=horizon)
        cluster = get_stock_cluster(conn, symbol=symbol, horizon=horizon)
        recommendation = detail.get("recommendation") or {}
        prediction = detail.get("prediction") or {}
        display_score = recommendation.get("final_score")
        score_source = "recommendation"
        if display_score is None:
            display_score = prediction.get("pred_prob_top20")
            score_source = "prediction_probability"
        items.append(
            {
                "symbol": symbol,
                "name": detail.get("name"),
                "market": detail.get("market"),
                "sector": detail.get("sector"),
                "horizon": horizon,
                "latest_price": detail.get("latest_price") or {},
                "prediction": prediction,
                "recommendation": recommendation,
                "display_score": display_score,
                "score_source": score_source,
                "is_top20": bool(detail.get("is_top20")),
                "cluster": cluster.get("cluster"),
                "peers": cluster.get("peers", []),
                "history": history[:20],
                "data_status": detail.get("data_status", "데이터 미수집"),
            }
        )
    return {
        "horizon": horizon,
        "items": items,
        "disclaimer": DISCLAIMER,
    }


def get_current_market_regime(conn) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT *
        FROM market_regime_daily
        ORDER BY prediction_date DESC, prediction_cutoff DESC, created_at DESC
        LIMIT 1
        """
    ).fetchdf()
    if row.empty:
        return {
            "status": "not_collected",
            "message": "장전 글로벌 레짐 데이터가 아직 생성되지 않았습니다.",
            "regime": None,
            "global_risk_score": None,
            "recommended_cash_ratio": None,
            "signals": {},
            "reasons": ["글로벌 일봉/장전 snapshot 수집 대기"],
        }
    data = _json_safe(row.iloc[0].to_dict())
    return {
        **data,
        "status": "ready",
        "signals": _loads(data.get("signals_json"), {}),
        "reasons": _loads(data.get("reasons_json"), []),
    }


def get_latest_global_markets(conn) -> dict[str, Any]:
    frame = conn.execute(
        """
        WITH latest AS (
            SELECT symbol, source_name, MAX(trade_date) AS trade_date
            FROM global_market_daily
            GROUP BY symbol, source_name
        )
        SELECT g.*
        FROM global_market_daily AS g
        JOIN latest AS l
          ON g.symbol = l.symbol
         AND g.source_name = l.source_name
         AND g.trade_date = l.trade_date
        ORDER BY g.market_group, g.symbol
        """
    ).fetchdf()
    return {
        "status": "not_collected" if frame.empty else "ready",
        "items": _records(frame),
    }


def build_today_market_snapshot(conn, config: dict[str, Any] | None = None) -> dict[str, Any]:
    focus_items = (config or {}).get("focus_stocks") or [
        {"symbol": symbol, "name": symbol} for symbol in FOCUS_DEMO_SYMBOLS
    ]
    snapshot_date = _local_today()
    focus_prices = _latest_focus_prices(conn, focus_items)
    regime = get_current_market_regime(conn)
    global_markets = get_latest_global_markets(conn)
    yahoo_prices = _resolve_yahoo_prices(conn, focus_items, focus_prices, global_markets, config=config)
    news = get_latest_news(conn, symbols=tuple(str(item["symbol"]).zfill(6) for item in focus_items))
    macro_news = get_recent_market_news(conn)
    move_explanations = get_market_move_explanations(conn, config=config)
    market_outlook = get_market_outlook(conn, config=config)
    sector_linkage = get_sector_linkage(conn, config=config)
    koru_linkage = get_koru_linkage(conn)
    data_quality = _today_data_quality(
        conn,
        focus_prices,
        yahoo_prices,
        regime,
        global_markets,
        news,
        macro_news,
        config=config,
    )
    snapshot = {
        "snapshot_date": str(snapshot_date),
        "status": data_quality["status"],
        "focus_prices": focus_prices,
        "yahoo_prices": yahoo_prices,
        "global_regime": regime,
        "global_markets": global_markets,
        "news": news,
        "move_explanations": move_explanations,
        "market_outlook": market_outlook,
        "sector_linkage": sector_linkage,
        "koru_linkage": koru_linkage,
        "data_quality": data_quality,
        "disclaimer": "오늘 시장 업데이트는 연구·정보제공용이며 투자 결과를 보장하지 않습니다.",
    }
    row = pd.DataFrame(
        [
            {
                "snapshot_date": snapshot_date,
                "status": snapshot["status"],
                "focus_prices_json": _json(focus_prices),
                "yahoo_prices_json": _json(yahoo_prices),
                "global_regime_json": _json(regime),
                "global_markets_json": _json(global_markets),
                "news_json": _json(news),
                "move_explanations_json": _json(move_explanations),
                "market_outlook_json": _json(market_outlook),
                "data_quality_json": _json(data_quality),
                "created_at": _utcnow(),
            }
        ]
    )
    append_dedup_table(conn, "today_market_snapshot", row, ["snapshot_date"])
    return snapshot


def get_today_market_snapshot(conn, config: dict[str, Any] | None = None) -> dict[str, Any]:
    base: dict[str, Any]
    if not table_exists(conn, "today_market_snapshot"):
        base = {
            "snapshot_date": str(_local_today()),
            "status": "not_collected",
            "focus_prices": [],
            "yahoo_prices": [],
            "global_regime": {},
            "global_markets": {},
            "news": [],
            "move_explanations": {},
            "market_outlook": get_market_outlook(conn, config=config),
            "sector_linkage": get_sector_linkage(conn, config=config),
            "koru_linkage": get_koru_linkage(conn),
            "data_quality": {"status": "not_collected", "messages": ["오늘 업데이트 스냅샷이 아직 없습니다."]},
            "disclaimer": "오늘 시장 업데이트는 연구·정보제공용이며 투자 결과를 보장하지 않습니다.",
        }
    else:
        row = conn.execute(
            "SELECT * FROM today_market_snapshot ORDER BY snapshot_date DESC, created_at DESC LIMIT 1"
        ).fetchdf()
        if row.empty:
            base = {
                "snapshot_date": str(_local_today()),
                "status": "not_collected",
                "focus_prices": [],
                "yahoo_prices": [],
                "global_regime": {},
                "global_markets": {},
                "news": [],
                "move_explanations": {},
                "market_outlook": get_market_outlook(conn, config=config),
                "sector_linkage": get_sector_linkage(conn, config=config),
                "koru_linkage": get_koru_linkage(conn),
                "data_quality": {"status": "not_collected", "messages": ["오늘 업데이트 스냅샷이 아직 없습니다."]},
                "disclaimer": "오늘 시장 업데이트는 연구·정보제공용이며 투자 결과를 보장하지 않습니다.",
            }
        else:
            data = row.iloc[0].to_dict()
            base = {
                "snapshot_date": _date_string(data.get("snapshot_date")),
                "status": data.get("status") or "partial_ready",
                "focus_prices": _loads(data.get("focus_prices_json"), []),
                "yahoo_prices": _loads(data.get("yahoo_prices_json"), []),
                "global_regime": _loads(data.get("global_regime_json"), {}),
                "global_markets": _loads(data.get("global_markets_json"), {}),
                "news": _loads(data.get("news_json"), []),
                "move_explanations": _loads(data.get("move_explanations_json"), {}),
                "market_outlook": _loads(data.get("market_outlook_json"), {}),
                "koru_linkage": get_koru_linkage(conn),
                "data_quality": _loads(data.get("data_quality_json"), {}),
                "disclaimer": "오늘 시장 업데이트는 연구·정보제공용이며 투자 결과를 보장하지 않습니다.",
            }
    return hydrate_today_market_snapshot(conn, base, config)


def get_tomorrow_market_snapshot(conn, config: dict[str, Any] | None = None) -> dict[str, Any]:
    base = get_today_market_snapshot(conn, config)
    next_day_outlook = _next_trading_day_outlook(conn, config=config)
    long_short_range = _next_day_long_short_range(conn, next_day_outlook, config=config)
    data_quality = _tomorrow_data_quality(base.get("data_quality") or {}, next_day_outlook)
    data_quality = _merge_long_short_range_quality(data_quality, long_short_range)
    return {
        **base,
        "status": data_quality["status"],
        "market_outlook": next_day_outlook,
        "long_short_range": long_short_range,
        "next_trading_day": next_day_outlook.get("target_date"),
        "range_multiplier": next_day_outlook.get("range_multiplier", NEXT_DAY_RANGE_MULTIPLIER),
        "data_quality": data_quality,
        "disclaimer": "다음 거래일 시장 예측은 연구·정보제공용이며 투자 결과를 보장하지 않습니다.",
    }


def hydrate_today_market_snapshot(
    conn,
    base: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    focus_items = (config or {}).get("focus_stocks") or DEFAULT_TODAY_FOCUS_STOCKS
    focus_symbols = tuple(str(item["symbol"]).zfill(6) for item in focus_items)
    focus_prices = _latest_focus_prices(conn, focus_items)
    global_markets = get_latest_global_markets(conn)
    regime = get_current_market_regime(conn)
    yahoo_prices = _resolve_yahoo_prices(conn, focus_items, focus_prices, global_markets, config=config)
    news = get_latest_news(conn, symbols=focus_symbols)
    macro_news = get_recent_market_news(conn)
    market_context = _build_market_context(regime, global_markets, macro_news) if not news else _macro_context_items(macro_news)
    move_explanations = get_market_move_explanations(conn, config=config)
    market_outlook = get_market_outlook(conn, config=config)
    sector_linkage = get_sector_linkage(conn, config=config)
    koru_linkage = get_koru_linkage(conn)
    data_quality = _today_data_quality(
        conn,
        focus_prices,
        yahoo_prices,
        regime,
        global_markets,
        news,
        macro_news,
        config=config,
    )
    return {
        **base,
        "snapshot_date": str(_local_today()),
        "status": data_quality["status"],
        "focus_prices": focus_prices,
        "yahoo_prices": yahoo_prices,
        "global_regime": regime,
        "global_markets": global_markets,
        "news": news,
        "macro_news": macro_news,
        "market_context": market_context,
        "move_explanations": move_explanations,
        "market_outlook": market_outlook,
        "sector_linkage": sector_linkage,
        "koru_linkage": koru_linkage,
        "data_quality": data_quality,
    }


def get_koru_linkage(conn, *, date: str = "latest") -> dict[str, Any]:
    return get_latest_koru_linkage(conn, asof_date=date)


def _sector_linkage_for_stock(conn, sector_value: Any) -> dict[str, Any]:
    domestic_sector = normalize_domestic_sector(sector_value)
    payload = get_sector_linkage(conn, sector=domestic_sector, limit=1)
    item = (payload.get("items") or [{}])[0] if payload.get("items") else {}
    return {
        "domestic_sector": domestic_sector,
        "status": payload.get("status"),
        "asof_date": payload.get("asof_date"),
        "item": item,
    }


def get_market_outlook(
    conn,
    *,
    date: str = "latest",
    horizon: str = "all",
    market: str = "all",
    limit: int = 20,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _get_market_outlook_payload(
        conn,
        date=date,
        horizon=horizon,
        market=market,
        limit=limit,
        config=config,
    )


def _next_trading_day_outlook(conn, config: dict[str, Any] | None = None) -> dict[str, Any]:
    source_payload = get_market_outlook(conn, date="latest", horizon="TODAY", market="all", limit=10, config=config)
    multiplier = _next_day_range_multiplier(config)
    source_items = list(source_payload.get("items") or [])
    asof = _as_date(source_payload.get("asof_date")) or _first_item_date(source_items, "asof_date")
    expected_target, calendar_source, calendar_messages = _next_trading_day_calendar(asof, config)

    items: list[dict[str, Any]] = []
    for item in source_items:
        item_asof = _as_date(item.get("asof_date")) or asof
        target = _as_date(item.get("target_date"))
        market = str(item.get("market") or "").upper()
        if market not in {"KOSPI", "KOSDAQ"}:
            continue
        if item_asof is None or target is None or target <= item_asof:
            continue
        items.append(_widen_next_day_forecast_item(item, multiplier=multiplier))

    target_dates = sorted({_date_string(item.get("target_date")) for item in items if item.get("target_date")})
    target_date = target_dates[0] if target_dates else _date_string(expected_target)
    quality = dict(source_payload.get("data_quality") or {})
    messages = _unique_messages(list(quality.get("messages") or []) + calendar_messages)
    if expected_target and target_date and _date_string(expected_target) != target_date:
        messages.append(
            f"저장된 다음 거래일({target_date})이 캘린더 기준({_date_string(expected_target)})과 달라 market_outlook 재생성이 필요합니다."
        )
    components = dict(quality.get("components") or {})
    components["next_day_outlook"] = "ready" if len(items) >= 2 else "missing"
    components["next_trading_day_calendar"] = "ready" if calendar_source != "weekday_fallback" else "fallback"
    status = "ready" if len(items) >= 2 and all(value == "ready" for value in components.values()) else "partial_ready"
    if not items:
        status = "not_collected"
        messages.append("다음 거래일 전망 데이터가 없습니다.")
    return {
        "asof_date": _date_string(asof),
        "target_date": target_date,
        "horizon": "NEXT_TRADING_DAY",
        "source_horizon": "TODAY",
        "status": status,
        "source": source_payload.get("source") or "market_outlook_forecasts",
        "range_multiplier": multiplier,
        "calendar_source": calendar_source,
        "summary": {
            "count": len(items),
            "markets": sorted({str(item.get("market")) for item in items if item.get("market")}),
            "horizons": ["NEXT_TRADING_DAY"] if items else [],
        },
        "items": items,
        "data_quality": {
            **quality,
            "status": status,
            "components": components,
            "messages": messages[:8],
            "source_horizon": "TODAY",
            "range_multiplier": multiplier,
            "calendar_source": calendar_source,
        },
    }


def _widen_next_day_forecast_item(item: dict[str, Any], *, multiplier: float) -> dict[str, Any]:
    output = dict(item)
    mu = _safe_float(item.get("expected_return"))
    low = _safe_float(item.get("range_low"))
    high = _safe_float(item.get("range_high"))
    output["source_horizon"] = item.get("horizon")
    output["horizon"] = "NEXT_TRADING_DAY"
    output["source_range_low"] = low
    output["source_range_high"] = high
    output["range_multiplier"] = multiplier
    if mu is not None and low is not None and high is not None:
        output["range_low"] = float(mu - abs(mu - low) * multiplier)
        output["range_high"] = float(mu + abs(high - mu) * multiplier)
    return output


def _tomorrow_data_quality(base_quality: dict[str, Any], outlook: dict[str, Any]) -> dict[str, Any]:
    outlook_quality = dict(outlook.get("data_quality") or {})
    components = dict(base_quality.get("components") or {})
    components["next_day_outlook"] = "ready" if (outlook.get("items") or []) else "missing"
    calendar_status = (outlook_quality.get("components") or {}).get("next_trading_day_calendar")
    if calendar_status:
        components["next_trading_day_calendar"] = calendar_status
    messages = _unique_messages(
        list(base_quality.get("messages") or []) + list(outlook_quality.get("messages") or [])
    )
    if not outlook.get("items"):
        status = "not_collected"
    elif base_quality.get("status") == "ready" and outlook.get("status") == "ready":
        status = "ready"
    else:
        status = "partial_ready"
    return {
        **base_quality,
        "status": status,
        "components": components,
        "messages": messages[:10],
    }


def _next_day_long_short_range(
    conn,
    outlook: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    items = []
    asof = _as_date(outlook.get("asof_date"))
    target = _as_date(outlook.get("target_date"))
    credit_by_market, credit_quality = _market_credit_features(conn, asof)
    for item in outlook.get("items") or []:
        market = str(item.get("market") or "").upper()
        if market not in {"KOSPI", "KOSDAQ"}:
            continue
        credit = credit_by_market.get(market) or credit_by_market.get("ALL") or _missing_credit_feature(market)
        expected_return = _safe_float(item.get("expected_return")) or 0.0
        up_probability = _safe_float(item.get("up_probability")) or 0.5
        down_probability = _safe_float(item.get("down_probability")) or 0.5
        shock_probability = _safe_float(item.get("shock_probability")) or 0.0
        confidence = _safe_float(item.get("confidence")) or 0.5
        credit_pressure = _safe_float(credit.get("credit_pressure_score")) or 0.5
        long_center = _clamp(
            0.50
            + 0.35 * (up_probability - down_probability)
            + 3.0 * expected_return
            - 0.10 * credit_pressure
            - 0.08 * shock_probability,
            0.25,
            0.75,
        )
        band = _clamp(
            0.08
            + 0.04 * credit_pressure
            + 0.04 * shock_probability
            + 0.03 * (1.0 - confidence),
            0.08,
            0.18,
        )
        long_low = _clamp(long_center - band, 0.0, 1.0)
        long_high = _clamp(long_center + band, 0.0, 1.0)
        short_low = _clamp(1.0 - long_high, 0.0, 1.0)
        short_high = _clamp(1.0 - long_low, 0.0, 1.0)
        items.append(
            {
                "market": market,
                "asof_date": _date_string(asof),
                "target_date": _date_string(target or _as_date(item.get("target_date"))),
                "long_center": long_center,
                "long_low": long_low,
                "long_high": long_high,
                "short_low": short_low,
                "short_high": short_high,
                "band": band,
                "expected_return": expected_return,
                "up_probability": up_probability,
                "down_probability": down_probability,
                "shock_probability": shock_probability,
                "confidence": confidence,
                "credit_delta_1d_pct": credit.get("credit_delta_1d_pct"),
                "credit_delta_5d_pct": credit.get("credit_delta_5d_pct"),
                "credit_delta_20d_pct": credit.get("credit_delta_20d_pct"),
                "credit_overheat_score": credit.get("credit_overheat_score"),
                "credit_deleveraging_score": credit.get("credit_deleveraging_score"),
                "credit_pressure_score": credit_pressure,
                "credit_balance_date": credit.get("date"),
                "credit_balance_krw": credit.get("credit_loan_balance_krw"),
                "credit_source": credit.get("source"),
                "basis": "market_outlook + market_credit_balance_daily",
                "disclaimer": "실제 주문·공매도 지시가 아닌 다음 거래일 시장별 모의 노출 범위입니다.",
            }
        )
    messages = list(credit_quality.get("messages") or [])
    if not items:
        messages.append("다음 거래일 숏·롱 범위를 계산할 시장 전망이 없습니다.")
    status = "ready" if items and credit_quality.get("status") == "ready" else "partial_ready" if items else "not_collected"
    return {
        "status": status,
        "asof_date": outlook.get("asof_date"),
        "target_date": outlook.get("target_date"),
        "items": items,
        "summary": {
            "count": len(items),
            "markets": sorted({item["market"] for item in items}),
        },
        "data_quality": {
            "status": status,
            "components": {
                "market_outlook": "ready" if items else "missing",
                "credit_balance": credit_quality.get("status", "missing"),
            },
            "messages": messages[:8],
        },
        "disclaimer": "다음 거래일 숏·롱 범위는 정보제공용 시뮬레이션이며 투자 결과를 보장하지 않습니다.",
    }


def _merge_long_short_range_quality(
    base_quality: dict[str, Any],
    long_short_range: dict[str, Any],
) -> dict[str, Any]:
    range_quality = long_short_range.get("data_quality") or {}
    components = dict(base_quality.get("components") or {})
    components["next_day_long_short_range"] = "ready" if long_short_range.get("items") else "missing"
    if (range_quality.get("components") or {}).get("credit_balance"):
        components["credit_balance"] = (range_quality.get("components") or {}).get("credit_balance")
    messages = _unique_messages(
        list(base_quality.get("messages") or []) + list(range_quality.get("messages") or [])
    )
    status = base_quality.get("status") or "partial_ready"
    if not long_short_range.get("items"):
        status = "partial_ready" if status == "ready" else status
    elif status == "ready" and long_short_range.get("status") != "ready":
        status = "partial_ready"
    return {
        **base_quality,
        "status": status,
        "components": components,
        "messages": messages[:10],
    }


def _market_credit_features(conn, asof: date | None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not table_exists(conn, "market_credit_balance_daily"):
        return {}, {"status": "missing", "messages": ["신용잔고 테이블이 없습니다."]}
    params: list[Any] = []
    where = ""
    if asof is not None:
        where = "WHERE date <= ?"
        params.append(asof)
    frame = conn.execute(
        f"""
        SELECT *
        FROM market_credit_balance_daily
        {where}
        ORDER BY market, date
        """,
        params,
    ).fetchdf()
    if frame.empty:
        return {}, {"status": "missing", "messages": ["신용잔고 데이터가 없어 중립 pressure로 숏·롱 범위를 계산했습니다."]}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    output: dict[str, dict[str, Any]] = {}
    for market, group in frame.groupby("market"):
        latest = group.sort_values("date").iloc[-1].to_dict()
        feature = _credit_feature(latest)
        output[str(market)] = feature
    return output, {"status": "ready", "messages": []}


def _credit_feature(row: dict[str, Any]) -> dict[str, Any]:
    balance = _safe_float(row.get("credit_loan_balance_krw"))
    delta_1d = _safe_float(row.get("credit_loan_delta_1d_krw"))
    delta_5d = _safe_float(row.get("credit_loan_delta_5d_krw"))
    delta_20d = _safe_float(row.get("credit_loan_delta_20d_krw"))
    delta_1d_pct = _delta_pct(delta_1d, balance)
    delta_5d_pct = _delta_pct(delta_5d, balance)
    delta_20d_pct = _delta_pct(delta_20d, balance)
    overheat = _clamp(0.5 + 20.0 * max(delta_5d_pct or 0.0, 0.0) + 10.0 * max(delta_20d_pct or 0.0, 0.0), 0.0, 1.0)
    deleveraging = _clamp(0.5 + 20.0 * max(-(delta_5d_pct or 0.0), 0.0) + 10.0 * max(-(delta_20d_pct or 0.0), 0.0), 0.0, 1.0)
    pressure = max(overheat, deleveraging)
    return {
        "date": _date_string(row.get("date")),
        "market": row.get("market"),
        "credit_loan_balance_krw": balance,
        "credit_delta_1d_pct": delta_1d_pct,
        "credit_delta_5d_pct": delta_5d_pct,
        "credit_delta_20d_pct": delta_20d_pct,
        "credit_overheat_score": overheat,
        "credit_deleveraging_score": deleveraging,
        "credit_pressure_score": pressure,
        "source": row.get("source"),
    }


def _missing_credit_feature(market: str) -> dict[str, Any]:
    return {
        "date": None,
        "market": market,
        "credit_loan_balance_krw": None,
        "credit_delta_1d_pct": None,
        "credit_delta_5d_pct": None,
        "credit_delta_20d_pct": None,
        "credit_overheat_score": 0.5,
        "credit_deleveraging_score": 0.5,
        "credit_pressure_score": 0.5,
        "source": None,
    }


def _delta_pct(delta: float | None, balance: float | None) -> float | None:
    if delta is None or balance is None:
        return None
    denominator = balance - delta
    if abs(denominator) < 1e-9:
        return None
    return float(delta / denominator)


def _clamp(value: float, low: float, high: float) -> float:
    return float(max(low, min(high, value)))


def _next_day_range_multiplier(config: dict[str, Any] | None = None) -> float:
    raw = ((config or {}).get("market_outlook") or {}).get(
        "next_day_range_multiplier",
        NEXT_DAY_RANGE_MULTIPLIER,
    )
    try:
        return float(max(1.0, float(raw)))
    except (TypeError, ValueError):
        return NEXT_DAY_RANGE_MULTIPLIER


def _next_trading_day_calendar(
    asof: date | None,
    config: dict[str, Any] | None = None,
) -> tuple[date | None, str, list[str]]:
    if asof is None:
        return None, "missing_asof", ["다음 거래일 계산을 위한 기준일이 없습니다."]
    market_cfg = (config or {}).get("market_outlook") or {}
    holidays = market_outlook_holidays(config)
    use_pykrx = bool(market_cfg.get("use_pykrx_calendar", True))
    fallback = target_dates_for_run(asof, today=asof + timedelta(days=1), holidays=holidays)["TODAY"]
    if holidays:
        source = "config_holidays"
    else:
        source = "weekday_fallback"
    messages: list[str] = []
    if not use_pykrx:
        return fallback, source, messages
    if not (os.getenv("KRX_ID") and os.getenv("KRX_PW")):
        if not holidays:
            messages.append("pykrx business-day helper를 위한 KRX_ID/PW가 없어 weekday fallback으로 다음 거래일을 계산했습니다.")
        return fallback, source, messages
    try:
        from pykrx import stock

        raw = stock.get_nearest_business_day_in_a_week(
            (asof + timedelta(days=1)).strftime("%Y%m%d"),
            prev=False,
        )
        parsed = _as_date(raw)
        if parsed is not None and parsed > asof:
            return parsed, "pykrx_business_day", messages
    except Exception as exc:  # pragma: no cover - depends on live pykrx/KRX state
        messages.append(f"pykrx business-day helper 실패: {exc}")
    return fallback, source, messages


def _first_item_date(items: list[dict[str, Any]], key: str) -> date | None:
    for item in items:
        parsed = _as_date(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _unique_messages(messages: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for message in messages:
        text = str(message)
        if text and text not in seen:
            output.append(text)
            seen.add(text)
    return output


def get_sector_linkage(
    conn,
    *,
    date: str = "latest",
    sector: str = "all",
    limit: int = 50,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return _get_sector_linkage_payload(conn, date=date, sector=sector, limit=limit, config=config)


def get_market_move_explanations(
    conn,
    *,
    date: str = "latest",
    scope: str | None = None,
    limit: int = 100,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    frame = _stored_market_move_explanations(conn, date=date, scope=scope, limit=limit)
    source = "market_move_explanations"
    if frame.empty:
        frame = build_market_move_explanations(conn, config, asof_date=date)
        if scope and scope != "all" and not frame.empty:
            frame = frame[frame["scope"].astype(str).eq(scope)]
        source = "computed_live"
    if frame.empty:
        freshness = price_freshness_report(conn).to_dict()
        return {
            "asof_date": None,
            "status": "not_collected",
            "source": source,
            "summary": {"market_count": 0, "top50_count": 0, "triggered_count": 0},
            "market": [],
            "top50": [],
            "items": [],
            "data_quality": {
                "status": "not_collected",
                "messages": ["가격 데이터 부족", *freshness.get("messages", [])],
            },
            "freshness": freshness,
        }
    frame = frame.copy()
    frame["_abs_move"] = pd.to_numeric(frame["move_pct"], errors="coerce").abs().fillna(0)
    frame["_scope_order"] = frame["scope"].astype(str).map({"market": 0, "top50": 1}).fillna(2)
    frame = frame.sort_values(["_scope_order", "triggered", "_abs_move", "market", "symbol"], ascending=[True, False, False, True, True])
    if limit:
        frame = frame.head(int(limit))
    items = _market_move_records(frame.drop(columns=["_abs_move", "_scope_order"], errors="ignore"))
    market_items = [item for item in items if item.get("scope") == "market"]
    top50_items = [item for item in items if item.get("scope") == "top50"]
    triggered_items = [item for item in top50_items if item.get("triggered")]
    data_quality = _loads(frame.iloc[0].get("data_quality_json"), {}) if "data_quality_json" in frame.columns else {}
    freshness = price_freshness_report(conn).to_dict()
    if freshness.get("stale"):
        data_quality = {**data_quality}
        data_quality["status"] = "partial_ready"
        data_quality["messages"] = [
            *list(data_quality.get("messages") or []),
            *list(freshness.get("messages") or []),
        ]
        components = dict(data_quality.get("components") or {})
        components["prices"] = "stale"
        data_quality["components"] = components
    return {
        "asof_date": _date_string(frame["asof_date"].max()),
        "status": data_quality.get("status") or "partial_ready",
        "source": source,
        "summary": {
            "market_count": len(market_items),
            "top50_count": len(top50_items),
            "triggered_count": len(triggered_items),
        },
        "market": market_items,
        "top50": triggered_items,
        "items": items,
        "data_quality": data_quality,
        "freshness": freshness,
    }


def _stored_market_move_explanations(
    conn,
    *,
    date: str,
    scope: str | None,
    limit: int,
) -> pd.DataFrame:
    if not table_exists(conn, "market_move_explanations"):
        return pd.DataFrame()
    params: list[Any] = []
    target_date = date
    if not target_date or target_date == "latest":
        row = conn.execute("SELECT MAX(asof_date) FROM market_move_explanations").fetchone()
        if not row or row[0] is None:
            return pd.DataFrame()
        target_date = _date_string(row[0]) or "latest"
    where = ["asof_date = ?"]
    params.append(pd.to_datetime(target_date).date())
    if scope and scope != "all":
        where.append("scope = ?")
        params.append(scope)
    params.append(int(max(1, limit or 100)))
    return conn.execute(
        f"""
        SELECT *
        FROM market_move_explanations
        WHERE {" AND ".join(where)}
        ORDER BY
          CASE WHEN scope = 'market' THEN 0 WHEN scope = 'top50' THEN 1 ELSE 2 END,
          triggered DESC,
          ABS(COALESCE(move_pct, 0)) DESC,
          market,
          symbol
        LIMIT ?
        """,
        params,
    ).fetchdf()


def _market_move_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in _records(frame):
        item["evidence"] = _loads(item.get("evidence_json"), [])
        item["prediction_context"] = _loads(item.get("prediction_context_json"), {})
        item["market_index_trigger"] = _loads(item.get("market_index_trigger_json"), {})
        item["data_quality"] = _loads(item.get("data_quality_json"), {})
        records.append(item)
    return records


def get_latest_news(
    conn,
    symbol: str | None = None,
    symbols: tuple[str, ...] | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    if not table_exists(conn, "news_articles"):
        return []
    where = ""
    params: list[Any] = []
    if symbol:
        where = "WHERE symbol = ?"
        params.append(str(symbol).zfill(6))
    elif symbols:
        normalized = [str(item).zfill(6) for item in symbols]
        placeholders = ", ".join(["?"] * len(normalized))
        where = f"WHERE symbol IN ({placeholders})"
        params.extend(normalized)
    frame = conn.execute(
        f"""
        SELECT *
        FROM news_articles
        {where}
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        LIMIT ?
        """,
        [*params, int(limit) * max(1, len(symbols or [symbol or "all"]))],
    ).fetchdf()
    return _records(frame)


def get_focus_stocks_demo(
    conn,
    symbols: tuple[str, ...] = FOCUS_DEMO_SYMBOLS,
    horizon: str = "3M",
) -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    regime = get_current_market_regime(conn)
    global_markets = get_latest_global_markets(conn)
    items = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).zfill(6)
        detail = get_stock_detail(conn, symbol=symbol, horizon=horizon)
        history = get_prediction_history(conn, symbol=symbol, horizon=horizon)
        cluster = get_stock_cluster(conn, symbol=symbol, horizon=horizon)
        recommendation = detail.get("recommendation") or {}
        prediction = detail.get("prediction") or {}
        display_score = recommendation.get("final_score")
        score_source = "recommendation"
        if display_score is None:
            display_score = prediction.get("pred_prob_top20")
            score_source = "prediction_probability"
        items.append(
            {
                "symbol": symbol,
                "name": detail.get("name"),
                "market": detail.get("market"),
                "sector": detail.get("sector"),
                "horizon": horizon,
                "latest_price": detail.get("latest_price") or {},
                "prediction": prediction,
                "recommendation": recommendation,
                "display_score": display_score,
                "score_source": score_source,
                "is_top20": bool(detail.get("is_top20")),
                "cluster": cluster.get("cluster"),
                "peers": cluster.get("peers", []),
                "history": history[:20],
                "data_status": "데이터 미수집" if not detail.get("latest_price") else "실데이터",
                "global_sensitivity": GLOBAL_SENSITIVITY.get(symbol, []),
                "global_adjustment": _global_adjustment(symbol, display_score, regime),
            }
        )
    return {
        "horizon": horizon,
        "items": items,
        "regime": regime,
        "global_markets": global_markets,
        "disclaimer": "연구·정보제공용 글로벌 위험 보정입니다. 모델 예측값과 투자 결과를 보장하지 않습니다.",
    }


def get_four_stock_demo(
    conn,
    symbols: tuple[str, ...] = FOUR_STOCK_DEMO_SYMBOLS,
    horizon: str = "3M",
) -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    regime = get_current_market_regime(conn)
    price_gap = build_prediction_price_gap(
        conn,
        lookback_days=30,
        target_days=30,
        horizon=horizon,
        symbols=[str(symbol).zfill(6) for symbol in symbols],
        limit=200,
    )
    price_gap_by_symbol = _latest_price_gap_by_symbol(price_gap.get("items", []))
    items = []
    for raw_symbol in symbols:
        symbol = str(raw_symbol).zfill(6)
        detail = get_stock_detail(conn, symbol=symbol, horizon=horizon)
        history = get_prediction_history(conn, symbol=symbol, horizon=horizon)
        cluster = get_stock_cluster(conn, symbol=symbol, horizon=horizon)
        recommendation = detail.get("recommendation") or {}
        prediction = detail.get("prediction") or {}
        display_score = recommendation.get("final_score")
        score_source = "recommendation"
        if display_score is None:
            display_score = prediction.get("pred_prob_top20")
            score_source = "prediction_probability"
        items.append(
            {
                "symbol": symbol,
                "name": detail.get("name"),
                "market": detail.get("market"),
                "sector": detail.get("sector"),
                "horizon": horizon,
                "latest_price": detail.get("latest_price") or {},
                "prediction": prediction,
                "recommendation": recommendation,
                "display_score": display_score,
                "score_source": score_source,
                "is_top20": bool(detail.get("is_top20")),
                "top20_status": "Top20 포함" if detail.get("is_top20") else "Top20 밖 / 예측값 있음",
                "cluster": cluster.get("cluster"),
                "peers": cluster.get("peers", []),
                "history": history[:12],
                "price_gap": price_gap_by_symbol.get(symbol, {}),
                "data_status": "가격 미수집" if not detail.get("latest_price") else "실데이터",
                "global_sensitivity": GLOBAL_SENSITIVITY.get(symbol, []),
                "global_adjustment": _global_adjustment(symbol, display_score, regime),
            }
        )
    return {
        "horizon": horizon,
        "items": items,
        "regime": regime,
        "price_gap_summary": price_gap.get("summary", {}),
        "disclaimer": "연구·정보제공용 네 종목 예측 데모입니다. 매수·매도 지시나 수익 보장 정보가 아닙니다.",
    }


def _latest_price_gap_by_symbol(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for item in sorted(items, key=lambda row: str(row.get("prediction_date") or ""), reverse=True):
        symbol = str(item.get("symbol") or "").zfill(6)
        if symbol and symbol not in selected:
            selected[symbol] = item
    return selected


def _latest_focus_prices(conn, focus_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for item in focus_items:
        symbol = str(item["symbol"]).zfill(6)
        domestic = conn.execute(
            """
            SELECT p.date, p.symbol, s.name, s.market, s.sector, p.close, p.volume,
                   p.trading_value, p.source, p.collected_at
            FROM prices_daily AS p
            LEFT JOIN symbols AS s ON p.symbol = s.symbol
            WHERE p.symbol = ?
            ORDER BY p.date DESC
            LIMIT 1
            """,
            [symbol],
        ).fetchdf()
        if domestic.empty:
            rows.append(
                {
                    "symbol": symbol,
                    "name": item.get("name") or symbol,
                    "status": "데이터 미수집",
                    "source": None,
                }
            )
            continue
        record = _json_safe(domestic.iloc[0].to_dict())
        record["status"] = "ready"
        record["name"] = record.get("name") or item.get("name") or symbol
        rows.append(record)
    return rows


def _latest_yahoo_prices(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "yahoo_prices_daily"):
        return []
    frame = conn.execute(
        """
        WITH latest AS (
          SELECT yahoo_symbol, MAX(date) AS date
          FROM yahoo_prices_daily
          GROUP BY yahoo_symbol
        )
        SELECT y.*
        FROM yahoo_prices_daily AS y
        JOIN latest AS l
          ON y.yahoo_symbol = l.yahoo_symbol
         AND y.date = l.date
        ORDER BY y.asset_type, y.yahoo_symbol
        """
    ).fetchdf()
    records = _records(frame)
    for record in records:
        record.setdefault("source", "yahoo_unofficial")
        record.setdefault("freshness_status", "ready")
    return records


def _resolve_yahoo_prices(
    conn,
    focus_items: list[dict[str, Any]],
    focus_prices: list[dict[str, Any]],
    global_markets: dict[str, Any],
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    yahoo_prices = _latest_yahoo_prices(conn)
    reference_date = _today_reference_date(conn, focus_prices, global_markets)
    max_age_days = _today_freshness_config(config)["max_yahoo_age_days"]
    if yahoo_prices and _rows_fresh_by_date(yahoo_prices, key="date", reference_date=reference_date, max_age_days=max_age_days):
        return yahoo_prices
    fallback_reason = "stale" if yahoo_prices else "missing"
    return _fallback_yahoo_prices(focus_items, focus_prices, global_markets, fallback_reason=fallback_reason)


def _fallback_yahoo_prices(
    focus_items: list[dict[str, Any]],
    focus_prices: list[dict[str, Any]],
    global_markets: dict[str, Any],
    *,
    fallback_reason: str,
) -> list[dict[str, Any]]:
    yahoo_by_symbol = {
        str(item.get("symbol", "")).zfill(6): item
        for item in (focus_items or [])
        if item.get("yahoo_symbol")
    }
    rows: list[dict[str, Any]] = []
    for item in focus_prices:
        if item.get("status") != "ready":
            continue
        symbol = str(item.get("symbol") or "").zfill(6)
        mapping = yahoo_by_symbol.get(symbol, {})
        rows.append(
            {
                "yahoo_symbol": mapping.get("yahoo_symbol") or symbol,
                "symbol": symbol,
                "asset_type": "stock",
                "date": item.get("date"),
                "close": item.get("close"),
                "currency": mapping.get("currency") or "KRW",
                "source": "domestic_prices_daily",
                "underlying_source": item.get("source"),
                "freshness_status": f"fallback_{fallback_reason}",
            }
        )

    seen = {row["yahoo_symbol"] for row in rows}
    for market in global_markets.get("items") or []:
        yahoo_symbol = str(market.get("symbol") or "")
        if not yahoo_symbol or yahoo_symbol in seen:
            continue
        rows.append(
            {
                "yahoo_symbol": yahoo_symbol,
                "symbol": yahoo_symbol,
                "asset_type": market.get("market_group") or "index",
                "date": market.get("trade_date"),
                "close": market.get("close"),
                "currency": "USD",
                "source": "global_market_daily",
                "underlying_source": market.get("source_name"),
                "freshness_status": f"fallback_{fallback_reason}",
            }
        )
        seen.add(yahoo_symbol)
    return rows


def _build_market_context(
    regime: dict[str, Any],
    global_markets: dict[str, Any],
    macro_news: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = list(_macro_context_items(macro_news or []))
    for reason in regime.get("reasons") or []:
        items.append(
            {
                "kind": "regime",
                "title": str(reason),
                "source_name": "global_regime",
            }
        )
    for market in (global_markets.get("items") or [])[:8]:
        return_1d = market.get("return_1d")
        if return_1d is None:
            continue
        label = market.get("display_name") or market.get("symbol") or "시장"
        items.append(
            {
                "kind": "market",
                "symbol": market.get("symbol"),
                "title": f"{label} 1D {float(return_1d) * 100:+.2f}%",
                "source_name": market.get("source_name") or "global_market_daily",
            }
        )
    return items


def _macro_context_items(macro_news: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for article in macro_news[:8]:
        items.append(
            {
                "kind": "macro_news",
                "title": article.get("title") or "거시 뉴스",
                "source_name": article.get("source") or "market_news_feed",
                "link": article.get("link"),
                "pub_date": article.get("pub_date"),
                "category": article.get("category"),
            }
        )
    return items


def _today_freshness_config(config: dict[str, Any] | None) -> dict[str, int]:
    raw = dict(TODAY_FRESHNESS_DEFAULTS)
    raw.update((config or {}).get("freshness") or {})
    return {
        "max_yahoo_age_days": max(0, int(raw.get("max_yahoo_age_days", 3))),
        "max_macro_news_age_hours": max(0, int(raw.get("max_macro_news_age_hours", 24))),
        "max_supply_age_days": max(0, int(raw.get("max_supply_age_days", 5))),
    }


def _today_reference_date(conn, focus_prices: list[dict[str, Any]], global_markets: dict[str, Any]) -> date:
    candidates: list[date] = []
    try:
        freshness = price_freshness_report(conn).to_dict()
        latest = _as_date(freshness.get("latest_date"))
        if latest:
            candidates.append(latest)
    except Exception:
        pass
    for item in focus_prices or []:
        value = _as_date(item.get("date"))
        if value:
            candidates.append(value)
    for item in global_markets.get("items") or []:
        value = _as_date(item.get("trade_date") or item.get("date"))
        if value:
            candidates.append(value)
    return max(candidates) if candidates else _local_today()


def _rows_fresh_by_date(
    rows: list[dict[str, Any]],
    *,
    key: str,
    reference_date: date,
    max_age_days: int,
) -> bool:
    latest = max((_as_date(row.get(key)) for row in rows), default=None)
    return latest is not None and (reference_date - latest).days <= max_age_days


def _yahoo_component_status(yahoo_prices: list[dict[str, Any]]) -> str:
    if not yahoo_prices:
        return "missing"
    statuses = {str(row.get("freshness_status") or "") for row in yahoo_prices}
    if "fallback_stale" in statuses:
        return "stale"
    if "fallback_missing" in statuses:
        return "missing"
    return "ready"


def _macro_news_status(conn, macro_news: list[dict[str, Any]], freshness_config: dict[str, int]) -> str:
    if not macro_news:
        return "missing"
    latest = None
    if table_exists(conn, "market_news_feed"):
        row = conn.execute("SELECT MAX(pub_date) FROM market_news_feed").fetchone()
        latest = _as_datetime(row[0]) if row else None
    if latest is None:
        latest = max((_as_datetime(item.get("pub_date")) for item in macro_news), default=None)
    if latest is None:
        return "missing"
    age_hours = (_utcnow() - latest).total_seconds() / 3600
    return "ready" if age_hours <= freshness_config["max_macro_news_age_hours"] else "stale"


def _news_component_status(news: list[dict[str, Any]], macro_news_status: str) -> str:
    if news:
        return "ready"
    if macro_news_status == "ready":
        return "ready"
    if macro_news_status == "stale":
        return "stale"
    return "missing"


def _supply_flow_status(conn, reference_date: date, freshness_config: dict[str, int]) -> str:
    latest_dates: list[date] = []
    for table in ("investor_flows_daily", "market_metrics_daily"):
        if not table_exists(conn, table):
            return "missing"
        row = conn.execute(f"SELECT MAX(date), COUNT(*) FROM {table}").fetchone()
        if not row or int(row[1] or 0) == 0:
            return "missing"
        latest = _as_date(row[0])
        if latest is None:
            return "missing"
        latest_dates.append(latest)
    oldest_latest = min(latest_dates)
    return "ready" if (reference_date - oldest_latest).days <= freshness_config["max_supply_age_days"] else "stale"


def _supply_flow_message(conn, status: str) -> str:
    reason = "수급 데이터 미수집" if status == "missing" else "수급 데이터 최신성 지연"
    row = conn.execute(
        """
        SELECT error_message
        FROM collection_failures
        WHERE step IN ('collect_investor_flows', 'collect_market_metrics')
        ORDER BY collected_at DESC
        LIMIT 1
        """
    ).fetchone()
    if row and row[0]:
        return f"{reason}: {str(row[0])[:160]}"
    return reason


def _today_data_quality(
    conn,
    focus_prices: list[dict[str, Any]],
    yahoo_prices: list[dict[str, Any]],
    regime: dict[str, Any],
    global_markets: dict[str, Any],
    news: list[dict[str, Any]],
    macro_news: list[dict[str, Any]] | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    messages = []
    freshness_config = _today_freshness_config(config)
    reference_date = _today_reference_date(conn, focus_prices, global_markets)
    domestic_ready = any(item.get("status") == "ready" for item in focus_prices)
    freshness = price_freshness_report(conn).to_dict()
    domestic_fresh = domestic_ready and not freshness.get("stale")
    yahoo_status = _yahoo_component_status(yahoo_prices)
    yahoo_ready = yahoo_status == "ready"
    regime_ready = regime.get("status") == "ready"
    global_ready = global_markets.get("status") == "ready"
    macro_news_status = _macro_news_status(conn, macro_news or [], freshness_config)
    news_status = _news_component_status(news, macro_news_status)
    news_ready = news_status == "ready"
    supply_status = _supply_flow_status(conn, reference_date, freshness_config)
    supply_ready = supply_status == "ready"
    if not domestic_ready:
        messages.append("국내 가격 데이터 미수집")
    elif freshness.get("stale"):
        messages.extend(freshness.get("messages") or [])
    if yahoo_status == "stale":
        messages.append("Yahoo/yfinance 원본 가격이 오래되어 국내/글로벌 최신 데이터로 대체 표시")
    elif not yahoo_ready:
        messages.append("Yahoo/yfinance opt-in 미실행 또는 수집 실패")
    if not regime_ready:
        messages.append("글로벌 레짐 미생성")
    if not global_ready:
        messages.append("해외시장 일봉 미수집")
    if news_status == "stale":
        messages.append("거시·수급 뉴스가 최신 기준보다 오래됨")
    elif not news_ready:
        last_news_failure = conn.execute(
            """
            SELECT error_message
            FROM collection_failures
            WHERE step = 'collect_naver_news'
            ORDER BY collected_at DESC
            LIMIT 1
            """
        ).fetchone()
        if last_news_failure and "NAVER_CLIENT_ID" in str(last_news_failure[0]):
            messages.append("뉴스 API 키 미설정")
        else:
            messages.append("뉴스 미수집")
    elif not news and macro_news:
        messages.append("거시 뉴스 피드 기준(종목별 네이버 뉴스 없음)")
    if supply_status != "ready":
        messages.append(_supply_flow_message(conn, supply_status))
    ready_count = sum([domestic_fresh, yahoo_ready, regime_ready, global_ready, news_ready, supply_ready])
    if ready_count == 6:
        status = "ready"
    elif ready_count > 0:
        status = "partial_ready"
    else:
        status = "not_collected"
    return {
        "status": status,
        "components": {
            "domestic_prices": "ready" if domestic_fresh else ("stale" if domestic_ready else "missing"),
            "yahoo_prices": yahoo_status,
            "supply_flows": supply_status,
            "market_regime": "ready" if regime_ready else "missing",
            "global_markets": "ready" if global_ready else "missing",
            "news": news_status,
        },
        "messages": messages,
        "freshness": freshness,
    }


def _global_adjustment(symbol: str, display_score, regime: dict[str, Any]) -> dict[str, Any]:
    if display_score is None:
        return {
            "status": "waiting_domestic_prediction",
            "message": "국내 예측 점수가 아직 없습니다.",
            "regime_adjusted_score": None,
            "global_risk_penalty": None,
            "confidence": None,
            "suggested_weight_cap": None,
            "cash_ratio": regime.get("recommended_cash_ratio"),
            "global_reasons": ["국내 예측 생성 대기"],
        }
    if regime.get("status") != "ready" or regime.get("global_risk_score") is None:
        return {
            "status": "waiting_global_data",
            "message": "글로벌 레짐 수집 후 보정 점수가 계산됩니다.",
            "regime_adjusted_score": None,
            "global_risk_penalty": None,
            "confidence": None,
            "suggested_weight_cap": None,
            "cash_ratio": None,
            "global_reasons": regime.get("reasons") or ["글로벌 레짐 데이터 수집 대기"],
        }
    risk_score = max(0.0, min(100.0, float(regime.get("global_risk_score") or 0.0)))
    sensitivity = GLOBAL_SENSITIVITY.get(symbol, [])
    sensitivity_multiplier = 1.15 if {"SOX", "Nasdaq"} & set(sensitivity) else 1.0
    penalty = min(0.35, (risk_score / 100.0) * 0.22 * sensitivity_multiplier)
    original_score = float(display_score)
    regime_name = str(regime.get("regime") or "neutral")
    reasons = list(regime.get("reasons") or [])
    reasons.append("민감 입력: " + ", ".join(sensitivity) if sensitivity else "민감 입력 미지정")
    return {
        "status": "ready",
        "message": "원 예측값은 유지하고 데모 표시용 보정 점수만 계산했습니다.",
        "regime": regime_name,
        "regime_adjusted_score": max(0.0, original_score - penalty),
        "global_risk_penalty": penalty,
        "confidence": max(0.1, 1.0 - (risk_score / 100.0) * 0.5),
        "suggested_weight_cap": REGIME_WEIGHT_CAP.get(regime_name, 0.12),
        "cash_ratio": regime.get("recommended_cash_ratio"),
        "global_reasons": reasons,
    }


def _latest_prediction_for_symbol(conn, symbol: str, horizon: str) -> dict[str, Any]:
    predictions = conn.execute(
        """
        SELECT *
        FROM predictions
        WHERE horizon = ?
          AND asof_date = (SELECT MAX(asof_date) FROM predictions WHERE horizon = ?)
        """,
        [horizon, horizon],
    ).fetchdf()
    if predictions.empty:
        return {}
    predictions["symbol"] = predictions["symbol"].astype(str).str.zfill(6)
    predictions["rank"] = predictions["pred_prob_top20"].rank(ascending=False, method="first")
    selected = predictions[predictions["symbol"].eq(symbol)]
    if selected.empty:
        return {}
    return _json_safe(selected.sort_values("pred_prob_top20", ascending=False).iloc[0].to_dict())


def build_position_summary(recommendations: pd.DataFrame, performance: pd.DataFrame) -> dict[str, Any]:
    avg_score = float(recommendations["final_score"].mean()) if not recommendations.empty else 0.5
    avg_excess = float(performance["avg_excess_return"].dropna().mean()) if not performance.empty else 0.0
    return {
        "quant": {
            "long_term": _direction(avg_excess),
            "short_term": _direction(avg_score - 0.5),
            "cash_ratio": 0.15 if avg_score >= 0.5 else 0.30,
        },
        "ai_robo": {
            "kospi": "LONG" if avg_score >= 0.55 else "NEUTRAL",
            "kosdaq": "LONG" if avg_score >= 0.60 else "NEUTRAL",
            "sp500": "NEUTRAL",
            "cash_ratio": 0.10 if avg_score >= 0.55 else 0.25,
        },
    }


def build_theme_data(recommendations: pd.DataFrame) -> dict[str, Any]:
    if recommendations.empty:
        return {"sectors": [], "top_sector": None}
    frame = recommendations.copy()
    frame["sector"] = frame["sector"].fillna(frame.get("market", "기타")).fillna("기타")
    grouped = (
        frame.groupby("sector")
        .agg(score=("final_score", "mean"), count=("symbol", "count"), avg_risk=("risk_score", "mean"))
        .reset_index()
        .sort_values("score", ascending=False)
    )
    sectors = _records(grouped)
    return {"top_sector": sectors[0]["sector"] if sectors else None, "sectors": sectors}


def build_ai_recommendations(recommendations: pd.DataFrame) -> list[dict[str, Any]]:
    if recommendations.empty:
        return []
    frame = recommendations.sort_values("rank").head(20).copy()
    frame["up_probability"] = pd.to_numeric(frame.get("pred_prob_top20"), errors="coerce").fillna(0.5)
    frame["expected_return"] = pd.to_numeric(frame.get("pred_return"), errors="coerce").fillna(0.0)
    frame["risk"] = pd.to_numeric(frame.get("risk_score"), errors="coerce").fillna(0.5)
    return _records(
        frame[
            [
                "rank",
                "symbol",
                "name",
                "market",
                "sector",
                "final_score",
                "up_probability",
                "expected_return",
                "risk",
                "reason_json",
                "risk_flags_json",
            ]
        ]
    )


def build_top20_upside_recommendations(
    recommendations: pd.DataFrame,
    limit: int = 20,
) -> list[dict[str, Any]]:
    if recommendations.empty:
        return []
    frame = recommendations.copy()
    limit = int(max(1, min(int(limit), 100)))
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    frame["sector"] = frame["sector"].fillna(frame.get("market", "기타")).fillna("기타")
    frame["up_probability"] = pd.to_numeric(frame.get("pred_prob_top20"), errors="coerce").fillna(0.5)
    frame["upside_return"] = pd.to_numeric(frame.get("pred_return"), errors="coerce").fillna(0.0)
    frame["upside_rank_score"] = frame["upside_return"].rank(pct=True, method="average").fillna(0.5)
    frame["combined_score"] = (
        0.60 * frame["up_probability"].clip(0.0, 1.0)
        + 0.40 * frame["upside_rank_score"].clip(0.0, 1.0)
    )
    frame["risk_score"] = pd.to_numeric(frame.get("risk_score"), errors="coerce").fillna(0.5)
    frame["target_upside_score"] = pd.to_numeric(frame.get("target_upside_score"), errors="coerce").fillna(0.5)
    frame = frame.sort_values(
        ["combined_score", "up_probability", "upside_return"],
        ascending=[False, False, False],
    )
    frame = frame.drop_duplicates(["symbol"], keep="first").head(limit).copy()
    frame["rank"] = range(1, len(frame) + 1)
    columns = [
        "rank",
        "symbol",
        "name",
        "market",
        "sector",
        "asof_date",
        "horizon",
        "up_probability",
        "upside_return",
        "upside_rank_score",
        "combined_score",
        "final_score",
        "risk_score",
        "target_upside_score",
        "model_version",
        "reason_json",
        "risk_flags_json",
    ]
    return _records(frame[[column for column in columns if column in frame.columns]])


def _top20_upside_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {
            "count": 0,
            "average_up_probability": None,
            "average_upside_return": None,
            "average_combined_score": None,
        }
    frame = pd.DataFrame(items)
    return {
        "count": int(len(frame)),
        "average_up_probability": _safe_float(pd.to_numeric(frame["up_probability"], errors="coerce").mean()),
        "average_upside_return": _safe_float(pd.to_numeric(frame["upside_return"], errors="coerce").mean()),
        "average_combined_score": _safe_float(pd.to_numeric(frame["combined_score"], errors="coerce").mean()),
    }


def build_core_portfolio(recommendations: pd.DataFrame) -> list[dict[str, Any]]:
    if recommendations.empty:
        return []
    frame = recommendations.sort_values("final_score", ascending=False).head(8).copy()
    frame["sector"] = frame["sector"].fillna(frame.get("market", "기타")).fillna("기타")
    frame["upside"] = pd.to_numeric(frame.get("target_upside_score"), errors="coerce").fillna(0.5)
    frame["current_return"] = pd.to_numeric(frame.get("pred_return"), errors="coerce").fillna(0.0)
    frame["current_price"] = np.nan
    frame["rating"] = (pd.to_numeric(frame["final_score"], errors="coerce").fillna(0.5) * 5).round(1)
    return _records(
        frame[
            [
                "symbol",
                "name",
                "sector",
                "rating",
                "upside",
                "current_return",
                "current_price",
                "risk_score",
                "final_score",
            ]
        ].rename(columns={"symbol": "ticker", "risk_score": "risk_level"})
    )


def build_qual_portfolio(conn) -> dict[str, Any]:
    reports = conn.execute(
        """
        SELECT symbol, stock_name, COUNT(*) AS report_count, AVG(upside_pct_at_report) AS avg_upside
        FROM analyst_reports
        GROUP BY symbol, stock_name
        ORDER BY report_count DESC, avg_upside DESC
        LIMIT 10
        """
    ).fetchdf()
    return {"theme": "애널리스트/공시 기반 정성 후보", "items": _records(reports)}


def build_upside_ranking(recommendations: pd.DataFrame) -> list[dict[str, Any]]:
    if recommendations.empty:
        return []
    score = "target_upside_score" if "target_upside_score" in recommendations.columns else "final_score"
    frame = recommendations.sort_values(score, ascending=False).head(10)
    return _records(frame[["symbol", "name", "market", score]].rename(columns={score: "upside_score"}))


def build_analyst_reports(conn) -> list[dict[str, Any]]:
    reports = conn.execute(
        """
        SELECT report_date, symbol, stock_name, broker_name, analyst_name, report_title,
               investment_rating, target_price, target_change_pct, upside_pct_at_report, source_name
        FROM analyst_reports
        ORDER BY report_date DESC
        LIMIT 20
        """
    ).fetchdf()
    return _records(reports)


def _latest_recommendations(conn, horizon: str) -> pd.DataFrame:
    horizon = _normalize_horizon(horizon)
    frame = conn.execute(
        """
        SELECT
          r.*,
          s.name,
          s.market,
          COALESCE(s.sector, s.market, '기타') AS sector,
          p.pred_return,
          p.pred_prob_top20,
          f.target_upside_score,
          f.risk_score,
          f.supply_demand_score,
          f.momentum_score,
          f.trading_value_ma20
        FROM recommendations AS r
        LEFT JOIN symbols AS s ON r.symbol = s.symbol
        LEFT JOIN predictions AS p
          ON r.asof_date = p.asof_date
         AND r.symbol = p.symbol
         AND r.horizon = p.horizon
         AND r.model_version = p.model_version
        LEFT JOIN features_daily AS f
          ON r.asof_date = f.date
         AND r.symbol = f.symbol
         AND r.horizon = f.horizon
        WHERE r.horizon = ?
          AND r.asof_date = (SELECT MAX(asof_date) FROM recommendations WHERE horizon = ?)
        ORDER BY r.rank
        """,
        [horizon, horizon],
    ).fetchdf()
    if frame.empty:
        return frame
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame


def _backtest_summary(conn, horizon: str) -> dict[str, Any]:
    horizon = _normalize_horizon(horizon)
    perf = conn.execute(
        """
        SELECT *
        FROM model_performance_daily
        WHERE horizon = ?
        ORDER BY eval_date DESC, production_weight DESC, created_at DESC
        LIMIT 1
        """,
        [horizon],
    ).fetchdf()
    if perf.empty:
        return {
            "horizon": horizon,
            "horizon_days": _horizon_days(horizon),
            "sample_count": 0,
            "hit_ratio": None,
            "precision_top20": None,
            "avg_excess_return": None,
            "mdd": None,
            "sharpe": None,
            "rank_ic": None,
            "weekly_returns": [],
        }
    row = perf.iloc[0].to_dict()
    weekly = conn.execute(
        """
        SELECT prediction_date, AVG(actual_return) AS return
        FROM backtest_results
        WHERE horizon = ?
        GROUP BY prediction_date
        ORDER BY prediction_date DESC
        LIMIT 12
        """,
        [horizon],
    ).fetchdf()
    return {
        **_json_safe(row),
        "weekly_returns": [
            {"label": str(record["prediction_date"]), "return": _safe_float(record["return"])}
            for record in weekly.to_dict(orient="records")
        ],
    }


def _model_accuracy(conn) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT
          eval_date,
          model_name,
          model_version,
          horizon,
          horizon_days,
          sample_count,
          hit_ratio,
          precision_top20,
          avg_excess_return,
          mdd,
          sharpe,
          rank_ic,
          production_weight,
          gate_status
        FROM model_performance_daily
        ORDER BY eval_date DESC, production_weight DESC, model_name
        LIMIT 50
        """
    ).fetchdf()


def _snapshot_date(recommendations: pd.DataFrame, performance: pd.DataFrame):
    if not recommendations.empty:
        return pd.to_datetime(recommendations["asof_date"]).max().date()
    if not performance.empty:
        return pd.to_datetime(performance["eval_date"]).max().date()
    return _local_today()


def _normalize_horizon(horizon: str | int) -> str:
    if isinstance(horizon, int) or str(horizon).isdigit():
        days = int(horizon)
        if days <= 20:
            return "1M"
        if days <= 90:
            return "3M"
        if days <= 180:
            return "6M"
        if days <= 360:
            return "1Y"
        return "2Y"
    return str(horizon)


def _horizon_days(horizon: str) -> int:
    return {"1M": 20, "2M": 42, "3M": 63, "6M": 126, "9M": 189, "1Y": 252, "2Y": 504}.get(horizon, 63)


def _direction(value: float) -> str:
    if value > 0.03:
        return "상방"
    if value > 0:
        return "약상방"
    if value < -0.03:
        return "하방"
    return "중립"


def _records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    return [_json_safe(row) for row in frame.to_dict(orient="records")]


def _json(value) -> str:
    return json.dumps(_sanitize_json(value), ensure_ascii=False, allow_nan=False)


def _loads(value, default):
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except (TypeError, ValueError):
        pass
    try:
        return _sanitize_json(json.loads(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _json_safe(row: dict) -> dict:
    return {key: _json_default(value) for key, value in row.items()}


def _json_default(value):
    if value is None:
        return None
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.ndarray):
        return [_json_default(item) for item in value.tolist()]
    if isinstance(value, dict):
        return {key: _json_default(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_default(item) for item in value]
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return None if np.isnan(value) else float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _sanitize_json(value):
    if isinstance(value, dict):
        return {key: _sanitize_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, np.ndarray):
        return [_sanitize_json(item) for item in value.tolist()]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return None if not np.isfinite(value) else float(value)
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _safe_float(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    return float(value)


def _date_string(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(pd.to_datetime(value).date())


def _as_date(value) -> date | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, str) and value.isdigit() and len(value) == 8:
        try:
            return datetime.strptime(value, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return pd.to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _as_datetime(value) -> datetime | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        parsed = pd.to_datetime(value)
    except (TypeError, ValueError):
        return None
    if getattr(parsed, "tzinfo", None) is not None:
        parsed = parsed.tz_convert(UTC) if hasattr(parsed, "tz_convert") else parsed.astimezone(UTC)
    return parsed.to_pydatetime().replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _local_today():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()

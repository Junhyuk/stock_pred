from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from roboquant.db import append_dedup_table, table_exists
from roboquant.us_sector_linkage import normalize_domestic_sector

DEFAULT_THRESHOLD = 0.02
DEFAULT_LOOKBACK_DAYS = 14
SEMICONDUCTOR_SYMBOLS = {"005930", "000660", "009150", "267260", "010120", "066570"}
GLOBAL_TECH_SYMBOLS = {"^SOX", "^IXIC", "QQQ", "SPY", "^GSPC"}
MARKET_INDEX_NAMES = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ"}


def refresh_market_move_explanations(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    explanations = build_market_move_explanations(conn, config, asof_date=asof_date)
    append_dedup_table(conn, "market_move_explanations", explanations, ["asof_date", "scope", "symbol"])
    return explanations


def build_market_move_explanations(
    conn,
    config: dict[str, Any] | None = None,
    *,
    asof_date: str = "latest",
) -> pd.DataFrame:
    settings = dict((config or {}).get("market_move_explanations", {}))
    threshold = float(settings.get("threshold", DEFAULT_THRESHOLD))
    lookback_days = int(settings.get("lookback_days", DEFAULT_LOOKBACK_DAYS))
    prices = _price_moves(conn, asof_date=asof_date)
    if prices.empty:
        return _empty_frame()

    target_date = pd.to_datetime(prices["asof_date"].max()).date()
    symbols = prices["symbol"].astype(str).tolist()
    flows = _flow_summary(conn, symbols, target_date)
    metrics = _metric_summary(conn, symbols, target_date)
    regime = _latest_regime(conn)
    global_markets = _latest_global_markets(conn, target_date)
    market_index_moves = _market_index_moves(conn, target_date, threshold=-abs(threshold))
    koru_context = _latest_koru_context(conn, target_date)
    telegram_news = _telegram_market_news(conn, target_date, lookback_days=lookback_days)
    macro_news = [*_market_news(conn, target_date, lookback_days=lookback_days), *telegram_news]
    stock_news = _stock_news(conn, symbols, target_date, lookback_days=lookback_days)
    prediction_horizon = str(settings.get("prediction_horizon", "2M"))
    prediction_contexts = _prediction_contexts(
        conn,
        symbols,
        target_date,
        preferred_horizon=prediction_horizon,
    )
    market_prediction_contexts = _market_prediction_contexts(
        conn,
        target_date,
        preferred_horizon=prediction_horizon,
    )
    sector_linkages = _sector_linkage_contexts(conn, target_date)
    quality = _data_quality(
        prices,
        flows,
        metrics,
        regime,
        global_markets,
        macro_news,
        stock_news,
        prediction_contexts,
    )
    market_flows = _aggregate_market_flows(flows, prices)
    created_at = _utcnow()

    rows: list[dict[str, Any]] = []
    market_names = set(str(item) for item in prices["market"].dropna().unique())
    market_names.update(market_index_moves)
    for market in sorted(market_names):
        market_frame = prices[prices["market"].astype(str).eq(market)]
        if market_frame.empty and market not in market_index_moves:
            continue
        market_row = _market_row(market_frame, market, target_date, threshold=threshold)
        market_row.update(market_index_moves.get(market, {}))
        if koru_context:
            market_row["koru_context"] = koru_context
        if sector_linkages:
            market_row["us_sector_context"] = sector_linkages.get("broad", {})
        rows.append(
            _explanation_row(
                market_row,
                flow=market_flows.get(market, {}),
                metric={},
                regime=regime,
                global_markets=global_markets,
                macro_news=macro_news,
                stock_news=[],
                prediction_context=market_prediction_contexts.get(market, {}),
                threshold=threshold,
                quality=quality,
                created_at=created_at,
            )
        )

    for _, price_row in prices.sort_values(["market", "symbol"]).iterrows():
        symbol = str(price_row["symbol"]).zfill(6)
        price_dict = price_row.to_dict()
        sector_key = normalize_domestic_sector(price_dict.get("sector"))
        if sector_linkages:
            price_dict["us_sector_context"] = sector_linkages.get(sector_key, sector_linkages.get("broad", {}))
        rows.append(
            _explanation_row(
                price_dict,
                flow=flows.get(symbol, {}),
                metric=metrics.get(symbol, {}),
                regime=regime,
                global_markets=global_markets,
                macro_news=macro_news,
                stock_news=stock_news.get(symbol, []),
                prediction_context=prediction_contexts.get(symbol, {}),
                threshold=threshold,
                quality=quality,
                created_at=created_at,
            )
        )

    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows, columns=_empty_frame().columns)


def _price_moves(conn, *, asof_date: str) -> pd.DataFrame:
    if not table_exists(conn, "prices_daily"):
        return pd.DataFrame()
    universe = _load_universe(conn)
    if universe.empty:
        return pd.DataFrame()
    symbols = universe["symbol"].astype(str).str.zfill(6).drop_duplicates().tolist()
    placeholders = ", ".join(["?"] * len(symbols))
    params: list[Any] = symbols.copy()
    date_filter = ""
    if asof_date and asof_date != "latest":
        date_filter = "AND p.date <= ?"
        params.append(pd.to_datetime(asof_date).date())
    prices = conn.execute(
        f"""
        SELECT p.date, p.symbol, p.close, p.volume, p.trading_value, p.source
        FROM prices_daily AS p
        WHERE p.symbol IN ({placeholders})
          AND p.close IS NOT NULL
          {date_filter}
        ORDER BY p.symbol, p.date
        """,
        params,
    ).fetchdf()
    if prices.empty:
        return pd.DataFrame()

    target_date = (
        pd.to_datetime(asof_date).date()
        if asof_date and asof_date != "latest"
        else pd.to_datetime(prices["date"]).max().date()
    )
    rows: list[dict[str, Any]] = []
    universe_by_symbol = {
        str(row["symbol"]).zfill(6): row.to_dict() for _, row in universe.drop_duplicates("symbol").iterrows()
    }
    for symbol, group in prices.groupby("symbol", sort=True):
        normalized_symbol = str(symbol).zfill(6)
        ordered = group.copy()
        ordered["date"] = pd.to_datetime(ordered["date"]).dt.date
        ordered = ordered[ordered["date"] <= target_date].sort_values("date")
        if ordered.empty:
            continue
        latest = ordered.iloc[-1]
        previous = ordered.iloc[-2] if len(ordered) >= 2 else None
        close = _safe_float(latest.get("close"))
        previous_close = None if previous is None else _safe_float(previous.get("close"))
        move_pct = None
        if close is not None and previous_close not in (None, 0):
            move_pct = close / previous_close - 1.0
        base_21 = ordered.iloc[-22] if len(ordered) >= 22 else None
        ret_21d = None
        if base_21 is not None and close is not None:
            base_close = _safe_float(base_21.get("close"))
            if base_close not in (None, 0):
                ret_21d = close / base_close - 1.0
        meta = universe_by_symbol.get(normalized_symbol, {})
        rows.append(
            {
                "asof_date": target_date,
                "scope": "top50",
                "symbol": normalized_symbol,
                "market": meta.get("market") or "UNKNOWN",
                "name": meta.get("name") or normalized_symbol,
                "sector": meta.get("sector"),
                "price_date": latest.get("date"),
                "close": close,
                "previous_close": previous_close,
                "volume": _safe_float(latest.get("volume")),
                "trading_value": _safe_float(latest.get("trading_value")),
                "move_pct": move_pct,
                "ret_21d": ret_21d,
                "price_source": latest.get("source") or "prices_daily",
            }
        )
    return pd.DataFrame(rows)


def _load_universe(conn) -> pd.DataFrame:
    queries = [
        """
        SELECT
          u.symbol,
          u.name,
          u.market,
          COALESCE(s.sector, u.security_type) AS sector
        FROM current_prediction_universe AS u
        LEFT JOIN symbols AS s ON s.symbol = u.symbol
        WHERE u.universe_rule = 'prediction_top_market_cap'
          AND u.is_enabled = TRUE
        ORDER BY u.market, u.prediction_rank
        """,
        """
        SELECT symbol, name, market, sector
        FROM symbols
        WHERE COALESCE(is_active, TRUE) = TRUE
        ORDER BY market, symbol
        LIMIT 80
        """,
    ]
    for query in queries:
        try:
            frame = conn.execute(query).fetchdf()
        except Exception:
            continue
        if not frame.empty:
            frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
            return frame.drop_duplicates("symbol")
    try:
        frame = conn.execute(
            """
            SELECT DISTINCT p.symbol, p.symbol AS name, 'UNKNOWN' AS market, NULL AS sector
            FROM prices_daily AS p
            ORDER BY p.symbol
            LIMIT 80
            """
        ).fetchdf()
    except Exception:
        return pd.DataFrame(columns=["symbol", "name", "market", "sector"])
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return frame


def _flow_summary(conn, symbols: list[str], asof_date) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "investor_flows_daily") or not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    cutoff = pd.Timestamp(asof_date) - pd.Timedelta(days=45)
    frame = conn.execute(
        f"""
        SELECT *
        FROM investor_flows_daily
        WHERE symbol IN ({placeholders})
          AND date >= ?
          AND date <= ?
        ORDER BY symbol, date
        """,
        [*symbols, cutoff.date(), asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    result: dict[str, dict[str, Any]] = {}
    for symbol, group in frame.groupby("symbol", sort=True):
        ordered = group.sort_values("date")
        item: dict[str, Any] = {"latest_date": _date_string(ordered["date"].max())}
        for investor in ("foreign", "institution", "retail", "pension"):
            column = f"{investor}_net_value"
            if column not in ordered.columns:
                continue
            values = pd.to_numeric(ordered[column], errors="coerce")
            for window in (1, 5, 20):
                item[f"{investor}_net_value_{window}d_sum"] = _safe_float(values.tail(window).sum(min_count=1))
        result[str(symbol).zfill(6)] = item
    return result


def _metric_summary(conn, symbols: list[str], asof_date) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "market_metrics_daily") or not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        SELECT *
        FROM market_metrics_daily
        WHERE symbol IN ({placeholders})
          AND date <= ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY date DESC) = 1
        """,
        [*symbols, asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    return {str(row["symbol"]).zfill(6): row.to_dict() for _, row in frame.iterrows()}


def _latest_regime(conn) -> dict[str, Any]:
    if not table_exists(conn, "market_regime_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM market_regime_daily
        ORDER BY prediction_date DESC, prediction_cutoff DESC, created_at DESC
        LIMIT 1
        """
    ).fetchdf()
    if frame.empty:
        return {}
    row = frame.iloc[0].to_dict()
    row["reasons"] = _json_list(row.get("reasons_json"))
    row["signals"] = _json_dict(row.get("signals_json"))
    return row


def _latest_global_markets(conn, asof_date) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "global_market_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM global_market_daily
        WHERE trade_date <= ?
        QUALIFY ROW_NUMBER() OVER (PARTITION BY symbol, source_name ORDER BY trade_date DESC) = 1
        """,
        [asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    return {str(row["symbol"]): row.to_dict() for _, row in frame.iterrows()}


def _market_index_moves(conn, asof_date, *, threshold: float) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "benchmark_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT date, benchmark, close, volume, trading_value
        FROM benchmark_daily
        WHERE benchmark IN ('KOSPI', 'KOSDAQ')
          AND date <= ?
        ORDER BY benchmark, date
        """,
        [asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["return_1d"] = frame.groupby("benchmark")["close"].pct_change(1)
    latest = (
        frame.sort_values(["benchmark", "date"])
        .groupby("benchmark", as_index=False, sort=True)
        .tail(1)
        .copy()
    )
    markets: dict[str, dict[str, Any]] = {}
    for _, row in latest.iterrows():
        market = str(row.get("benchmark"))
        move_pct = _safe_float(row.get("return_1d"))
        markets[market] = {
            "return_1d": move_pct,
            "triggered": move_pct is not None and move_pct <= threshold,
        }
    trigger = {
        "threshold": threshold,
        "triggered": any(item["triggered"] for item in markets.values()),
        "markets": markets,
    }
    result: dict[str, dict[str, Any]] = {}
    for _, row in latest.iterrows():
        market = str(row.get("benchmark"))
        move_pct = _safe_float(row.get("return_1d"))
        result[market] = {
            "asof_date": pd.to_datetime(row.get("date")).date(),
            "scope": "market",
            "symbol": market,
            "market": market,
            "name": MARKET_INDEX_NAMES.get(market, market),
            "sector": "market_index",
            "price_date": row.get("date"),
            "close": _safe_float(row.get("close")),
            "volume": _safe_float(row.get("volume")),
            "trading_value": _safe_float(row.get("trading_value")),
            "move_pct": move_pct,
            "ret_21d": None,
            "price_source": "benchmark_daily",
            "market_index_trigger": trigger,
        }
    return result


def _latest_koru_context(conn, asof_date) -> dict[str, Any]:
    if not table_exists(conn, "koru_korea_linkage"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM koru_korea_linkage
        WHERE trade_date <= ?
        ORDER BY trade_date DESC, created_at DESC
        LIMIT 1
        """,
        [asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    row = frame.iloc[0].to_dict()
    return {
        "trade_date": _date_string(row.get("trade_date")),
        "us_signal_date": _date_string(row.get("us_signal_date")),
        "koru_return_1d": _safe_float(row.get("koru_return_1d")),
        "ewy_return_1d": _safe_float(row.get("ewy_return_1d")),
        "spy_return_1d": _safe_float(row.get("spy_return_1d")),
        "qqq_return_1d": _safe_float(row.get("qqq_return_1d")),
        "koru_ewy_spread_1d": _safe_float(row.get("koru_ewy_spread_1d")),
        "koru_leverage_drift_1d": _safe_float(row.get("koru_leverage_drift_1d")),
        "koru_impact_score": _safe_float(row.get("koru_impact_score")),
        "koru_market_shock_flag": bool(row.get("koru_market_shock_flag")),
        "market_index_trigger": _json_dict(row.get("market_index_trigger_json")),
        "causes": _json_list(row.get("causes_json")),
        "data_quality": _json_dict(row.get("data_quality_json")),
    }


def _sector_linkage_contexts(conn, asof_date) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "us_sector_linkage_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM us_sector_linkage_daily
        WHERE trade_date = (
            SELECT MAX(trade_date)
            FROM us_sector_linkage_daily
            WHERE trade_date <= ?
        )
        ORDER BY domestic_sector
        """,
        [asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    contexts: dict[str, dict[str, Any]] = {}
    for row in frame.to_dict(orient="records"):
        sector = str(row.get("domestic_sector") or "broad")
        contexts[sector] = {
            "trade_date": _date_string(row.get("trade_date")),
            "domestic_sector": sector,
            "primary_proxy": row.get("primary_proxy"),
            "proxy_symbols": _json_list(row.get("proxy_symbols_json")),
            "us_sector_return_1d": _safe_float(row.get("us_sector_return_1d")),
            "us_sector_return_5d": _safe_float(row.get("us_sector_return_5d")),
            "us_sector_zscore_20d": _safe_float(row.get("us_sector_zscore_20d")),
            "us_sector_beta_60d": _safe_float(row.get("us_sector_beta_60d")),
            "us_sector_corr_60d": _safe_float(row.get("us_sector_corr_60d")),
            "us_sector_impact_score": _safe_float(row.get("us_sector_impact_score")),
            "us_sector_direction_agreement": _safe_float(row.get("us_sector_direction_agreement")),
            "data_quality": _json_dict(row.get("data_quality_json")),
        }
    return contexts


def _market_news(conn, asof_date, *, lookback_days: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_news_feed"):
        return []
    cutoff = pd.Timestamp(asof_date) - pd.Timedelta(days=lookback_days)
    frame = conn.execute(
        """
        SELECT *
        FROM market_news_feed
        WHERE pub_date >= ?
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        LIMIT 40
        """,
        [cutoff.to_pydatetime()],
    ).fetchdf()
    return [_news_row(row.to_dict(), theme_key="themes_json") for _, row in frame.iterrows()]


def _telegram_market_news(conn, asof_date, *, lookback_days: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "telegram_posts"):
        return []
    cutoff = pd.Timestamp(asof_date) - pd.Timedelta(days=lookback_days)
    frame = conn.execute(
        """
        SELECT *
        FROM telegram_posts
        WHERE date_utc >= ?
        ORDER BY date_utc DESC NULLS LAST, source_weight DESC NULLS LAST, urgency_score DESC NULLS LAST
        LIMIT 40
        """,
        [cutoff.to_pydatetime()],
    ).fetchdf()
    if frame.empty:
        return []
    items: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        text = str(row.get("text_excerpt") or row.get("text") or "")
        channel = str(row.get("channel") or "telegram")
        items.append(
            {
                "kind": "telegram",
                "title": text[:120] if text else f"Telegram {channel}",
                "summary": text,
                "description": text,
                "source": f"telegram:{channel}",
                "link": row.get("telegram_url"),
                "pub_date": _date_string(row.get("date_utc")),
                "themes": _json_list(row.get("themes_json")),
                "risk_keywords": _json_list(row.get("risk_keywords_json")),
                "sentiment_score": _safe_float(row.get("sentiment_score")),
                "urgency_score": _safe_float(row.get("urgency_score")),
            }
        )
    return items


def _stock_news(conn, symbols: list[str], asof_date, *, lookback_days: int) -> dict[str, list[dict[str, Any]]]:
    if not table_exists(conn, "news_articles") or not symbols:
        return {}
    placeholders = ", ".join(["?"] * len(symbols))
    cutoff = pd.Timestamp(asof_date) - pd.Timedelta(days=lookback_days)
    frame = conn.execute(
        f"""
        SELECT *
        FROM news_articles
        WHERE symbol IN ({placeholders})
          AND pub_date >= ?
        ORDER BY symbol, pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        """,
        [*symbols, cutoff.to_pydatetime()],
    ).fetchdf()
    result: dict[str, list[dict[str, Any]]] = {}
    for _, row in frame.iterrows():
        symbol = str(row.get("symbol")).zfill(6)
        result.setdefault(symbol, []).append(_news_row(row.to_dict(), theme_key=None))
    return result


def _prediction_contexts(
    conn,
    symbols: list[str],
    asof_date,
    *,
    preferred_horizon: str,
) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "predictions") or not symbols:
        return {}
    symbols = [str(symbol).zfill(6) for symbol in symbols]
    placeholders = ", ".join(["?"] * len(symbols))
    frame = conn.execute(
        f"""
        SELECT *
        FROM predictions
        WHERE symbol IN ({placeholders})
          AND asof_date <= ?
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY symbol
          ORDER BY
            asof_date DESC,
            CASE
              WHEN horizon = ? THEN 0
              WHEN horizon = '2M' THEN 1
              WHEN horizon = '3M' THEN 2
              WHEN horizon = '6M' THEN 3
              ELSE 9
            END
        ) = 1
        """,
        [*symbols, asof_date, preferred_horizon],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    recommendation_map = _latest_recommendation_context(conn, asof_date)
    up_down_map = _latest_up_down_context(conn, asof_date)
    long_short_map = _latest_long_short_context(conn, asof_date)
    gate_map = _latest_gate_status(conn)

    contexts: dict[str, dict[str, Any]] = {}
    for _, row in frame.iterrows():
        symbol = str(row["symbol"]).zfill(6)
        horizon = str(row.get("horizon") or preferred_horizon)
        rec = recommendation_map.get((symbol, horizon), {})
        up_down = up_down_map.get((symbol, horizon), {})
        long_short = long_short_map.get((symbol, horizon), {})
        model_version = row.get("model_version")
        side = up_down.get("side") or _predicted_side(row.to_dict()) or _long_short_to_direction(long_short.get("side"))
        rank = rec.get("rank") or up_down.get("rank") or long_short.get("leg_rank")
        contexts[symbol] = {
            "status": "ready",
            "asof_date": _date_string(row.get("asof_date")),
            "horizon": horizon,
            "pred_return": _safe_float(row.get("pred_return")),
            "pred_prob_top20": _safe_float(row.get("pred_prob_top20")),
            "pred_prob_bottom20": _safe_float(row.get("pred_prob_bottom20")),
            "rank": _safe_int(rank),
            "side": side,
            "long_short_side": long_short.get("side"),
            "market_up_down_side": up_down.get("side"),
            "final_score": _safe_float(rec.get("final_score")),
            "model_version": None if model_version is None else str(model_version),
            "gate_status": gate_map.get((str(model_version), horizon)) or gate_map.get((None, horizon)),
            "source": "predictions/recommendations/market_up_down/long_short",
        }
    return contexts


def _latest_recommendation_context(conn, asof_date) -> dict[tuple[str, str], dict[str, Any]]:
    if not table_exists(conn, "recommendations"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM recommendations
        WHERE asof_date <= ?
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY symbol, horizon
          ORDER BY asof_date DESC, rank ASC
        ) = 1
        """,
        [asof_date],
    ).fetchdf()
    return _context_map(frame, rank_column="rank")


def _latest_up_down_context(conn, asof_date) -> dict[tuple[str, str], dict[str, Any]]:
    if not table_exists(conn, "market_up_down_recommendations"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM market_up_down_recommendations
        WHERE asof_date <= ?
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY symbol, horizon
          ORDER BY asof_date DESC, rank ASC
        ) = 1
        """,
        [asof_date],
    ).fetchdf()
    return _context_map(frame, rank_column="rank")


def _latest_long_short_context(conn, asof_date) -> dict[tuple[str, str], dict[str, Any]]:
    if not table_exists(conn, "long_short_recommendations"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM long_short_recommendations
        WHERE asof_date <= ?
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY symbol, horizon
          ORDER BY asof_date DESC, leg_rank ASC
        ) = 1
        """,
        [asof_date],
    ).fetchdf()
    return _context_map(frame, rank_column="leg_rank")


def _market_prediction_contexts(conn, asof_date, *, preferred_horizon: str) -> dict[str, dict[str, Any]]:
    if not table_exists(conn, "market_up_down_recommendations"):
        return {}
    frame = conn.execute(
        """
        SELECT *
        FROM market_up_down_recommendations
        WHERE asof_date <= ?
        """,
        [asof_date],
    ).fetchdf()
    if frame.empty:
        return {}
    frame["asof_date"] = pd.to_datetime(frame["asof_date"]).dt.date
    latest_date = frame["asof_date"].max()
    latest = frame[frame["asof_date"].eq(latest_date)].copy()
    preferred = latest[latest["horizon"].astype(str).eq(preferred_horizon)]
    if not preferred.empty:
        latest = preferred
    gate_map = _latest_gate_status(conn)
    contexts: dict[str, dict[str, Any]] = {}
    for market, group in latest.groupby("market", sort=True):
        side_counts = group["side"].astype(str).value_counts().to_dict()
        up_count = int(side_counts.get("UP", 0))
        down_count = int(side_counts.get("DOWN", 0))
        dominant_side = "UP" if up_count >= down_count else "DOWN"
        horizon = str(group["horizon"].mode().iloc[0]) if "horizon" in group and not group.empty else preferred_horizon
        model_version = str(group["model_version"].mode().iloc[0]) if "model_version" in group and not group.empty else None
        top_up = (
            group[group["side"].astype(str).eq("UP")]
            .sort_values("rank")
            .head(3)["symbol"]
            .astype(str)
            .str.zfill(6)
            .tolist()
        )
        top_down = (
            group[group["side"].astype(str).eq("DOWN")]
            .sort_values("rank")
            .head(3)["symbol"]
            .astype(str)
            .str.zfill(6)
            .tolist()
        )
        contexts[str(market)] = {
            "status": "ready",
            "asof_date": _date_string(latest_date),
            "horizon": horizon,
            "side": dominant_side,
            "up_count": up_count,
            "down_count": down_count,
            "top_up_symbols": top_up,
            "top_down_symbols": top_down,
            "model_version": model_version,
            "gate_status": gate_map.get((model_version, horizon)) or gate_map.get((None, horizon)),
            "source": "market_up_down_recommendations",
        }
    return contexts


def _latest_gate_status(conn) -> dict[tuple[str | None, str], str]:
    if not table_exists(conn, "model_performance_daily"):
        return {}
    frame = conn.execute(
        """
        SELECT model_version, horizon, gate_status
        FROM model_performance_daily
        WHERE gate_status IS NOT NULL
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY model_version, horizon
          ORDER BY eval_date DESC, created_at DESC
        ) = 1
        """
    ).fetchdf()
    result: dict[tuple[str | None, str], str] = {}
    for _, row in frame.iterrows():
        horizon = str(row.get("horizon"))
        status = str(row.get("gate_status"))
        version = row.get("model_version")
        result[(None if version is None else str(version), horizon)] = status
        result.setdefault((None, horizon), status)
    return result


def _context_map(frame: pd.DataFrame, *, rank_column: str) -> dict[tuple[str, str], dict[str, Any]]:
    if frame.empty:
        return {}
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for _, row in frame.iterrows():
        item = row.to_dict()
        item[rank_column] = _safe_int(item.get(rank_column))
        result[(str(item["symbol"]).zfill(6), str(item.get("horizon")))] = item
    return result


def _market_row(frame: pd.DataFrame, market: str, asof_date, *, threshold: float) -> dict[str, Any]:
    moves = pd.to_numeric(frame["move_pct"], errors="coerce").dropna()
    move_pct = None if moves.empty else float(moves.median())
    up_count = int((moves >= threshold).sum()) if not moves.empty else 0
    down_count = int((moves <= -threshold).sum()) if not moves.empty else 0
    return {
        "asof_date": asof_date,
        "scope": "market",
        "symbol": market,
        "market": market,
        "name": f"{market} Top50",
        "sector": "market",
        "move_pct": move_pct,
        "ret_21d": None,
        "price_source": "prices_daily_top50_median",
        "up_count": up_count,
        "down_count": down_count,
        "sample_count": int(len(frame)),
    }


def _aggregate_market_flows(flows: dict[str, dict[str, Any]], prices: pd.DataFrame) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not flows:
        return result
    for market, frame in prices.groupby("market"):
        item: dict[str, Any] = {}
        symbols = [str(symbol).zfill(6) for symbol in frame["symbol"].tolist()]
        for key in (
            "foreign_net_value_1d_sum",
            "foreign_net_value_5d_sum",
            "foreign_net_value_20d_sum",
            "institution_net_value_1d_sum",
            "institution_net_value_5d_sum",
            "institution_net_value_20d_sum",
            "pension_net_value_1d_sum",
            "pension_net_value_5d_sum",
            "pension_net_value_20d_sum",
        ):
            values = [_safe_float(flows.get(symbol, {}).get(key)) for symbol in symbols]
            filtered = [value for value in values if value is not None]
            if filtered:
                item[key] = float(sum(filtered))
        result[str(market)] = item
    return result


def _explanation_row(
    row: dict[str, Any],
    *,
    flow: dict[str, Any],
    metric: dict[str, Any],
    regime: dict[str, Any],
    global_markets: dict[str, dict[str, Any]],
    macro_news: list[dict[str, Any]],
    stock_news: list[dict[str, Any]],
    prediction_context: dict[str, Any],
    threshold: float,
    quality: dict[str, Any],
    created_at: datetime,
) -> dict[str, Any]:
    move_pct = _safe_float(row.get("move_pct"))
    direction = _direction(move_pct)
    market_index_trigger = row.get("market_index_trigger") or {}
    if row.get("scope") == "market" and market_index_trigger:
        market_trigger = (market_index_trigger.get("markets") or {}).get(str(row.get("market")), {})
        triggered = bool(market_trigger.get("triggered"))
    else:
        triggered = move_pct is not None and abs(move_pct) >= threshold
    evidence: list[dict[str, Any]] = [
        {
            "kind": "price",
            "label": "1D 변동률",
            "value": move_pct,
            "source": row.get("price_source") or "prices_daily",
        }
    ]
    if row.get("scope") == "market":
        evidence.append(
            {
                "kind": "breadth",
                "label": "2% 이상 변동 종목 수",
                "value": {"up": row.get("up_count", 0), "down": row.get("down_count", 0)},
                "source": "Top50 universe",
            }
        )
        if market_index_trigger:
            evidence.append(
                {
                    "kind": "market_index_trigger",
                    "label": "KOSPI/KOSDAQ -2% 시장충격",
                    "value": market_index_trigger,
                    "source": "benchmark_daily",
                }
            )
        if row.get("koru_context"):
            evidence.append(
                {
                    "kind": "koru",
                    "label": "KORU 레버리지 심리",
                    "value": _koru_evidence_value(row.get("koru_context") or {}),
                    "source": "koru_korea_linkage",
                }
            )
    if prediction_context:
        evidence.append(
            {
                "kind": "prediction",
                "label": "재학습 모델 방향",
                "value": _prediction_evidence_value(prediction_context),
                "source": prediction_context.get("source") or "predictions/recommendations",
            }
        )
    us_sector_context = row.get("us_sector_context") or {}
    if us_sector_context.get("primary_proxy"):
        evidence.append(
            {
                "kind": "us_sector",
                "label": "미국 유사섹터 영향",
                "value": _us_sector_evidence_value(us_sector_context),
                "source": "us_sector_linkage_daily/global_market_daily",
            }
        )
    if not triggered:
        if row.get("scope") == "market" and market_index_trigger:
            primary_reason = "KOSPI/KOSDAQ -2% 시장충격 없음" if move_pct is not None else "지수 비교 데이터 부족"
        else:
            primary_reason = "2% 이상 변동 없음" if move_pct is not None else "가격 비교 데이터 부족"
        confidence = 0.25 if move_pct is not None else 0.1
    else:
        candidates = _reason_candidates(
            row,
            direction=direction,
            flow=flow,
            metric=metric,
            regime=regime,
            global_markets=global_markets,
            macro_news=macro_news,
            stock_news=stock_news,
        )
        if not candidates:
            primary_reason = (
                "가격 변동은 임계치를 넘었지만 수급·뉴스·글로벌 증거가 부족해 가격 기반 변동으로 분류"
            )
            confidence = 0.35
        else:
            candidates = sorted(candidates, key=lambda item: item["score"], reverse=True)
            primary_reason = str(candidates[0]["reason"])
            evidence.extend(candidate["evidence"] for candidate in candidates[:5])
            confidence = min(0.95, 0.42 + 0.12 * len(candidates) + min(abs(move_pct or 0), 0.08))
    if quality.get("messages"):
        evidence.append({"kind": "data_quality", "label": "데이터 품질", "value": quality["messages"][:4]})
    return {
        "asof_date": row.get("asof_date"),
        "scope": row.get("scope") or "top50",
        "symbol": row.get("symbol"),
        "market": row.get("market"),
        "name": row.get("name"),
        "move_pct": move_pct,
        "direction": direction,
        "triggered": bool(triggered),
        "primary_reason": primary_reason,
        "evidence_json": _json(evidence),
        "prediction_context_json": _json(prediction_context or {}),
        "market_index_trigger_json": _json(market_index_trigger),
        "confidence": confidence,
        "data_quality_json": _json(quality),
        "created_at": created_at,
    }


def _reason_candidates(
    row: dict[str, Any],
    *,
    direction: str,
    flow: dict[str, Any],
    metric: dict[str, Any],
    regime: dict[str, Any],
    global_markets: dict[str, dict[str, Any]],
    macro_news: list[dict[str, Any]],
    stock_news: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    is_down = direction == "DOWN"
    is_up = direction == "UP"
    symbol = str(row.get("symbol") or "").zfill(6)
    market = str(row.get("market") or "")
    name = str(row.get("name") or symbol)
    sector = str(row.get("sector") or "")
    tech_signal = _global_tech_move(global_markets)
    macro_text = " ".join(str(item.get("title") or "") + " " + str(item.get("summary") or "") for item in macro_news)
    stock_text = " ".join(str(item.get("title") or "") + " " + str(item.get("description") or "") for item in stock_news)
    telegram_items = [item for item in macro_news if item.get("kind") == "telegram"]
    telegram_text = " ".join(str(item.get("title") or "") + " " + str(item.get("summary") or "") for item in telegram_items)
    koru_context = row.get("koru_context") or {}
    koru_return = _safe_float(koru_context.get("koru_return_1d"))
    ewy_return = _safe_float(koru_context.get("ewy_return_1d"))
    koru_score = _safe_float(koru_context.get("koru_impact_score"))
    us_sector_context = row.get("us_sector_context") or {}
    us_sector_return = _safe_float(us_sector_context.get("us_sector_return_1d"))
    us_sector_impact = _safe_float(us_sector_context.get("us_sector_impact_score"))

    if is_down and us_sector_return is not None and us_sector_return <= -0.015:
        candidates.append(
            _candidate(
                0.76,
                "미국 유사섹터 약세가 국내 섹터 심리에 부담",
                "us_sector",
                _us_sector_evidence_value(us_sector_context),
                "us_sector_linkage_daily/global_market_daily",
            )
        )
    if is_up and (us_sector_return is not None and us_sector_return >= 0.015 or us_sector_impact is not None and us_sector_impact >= 0.65):
        candidates.append(
            _candidate(
                0.62,
                "미국 유사섹터 강세가 국내 섹터 심리를 보강",
                "us_sector",
                _us_sector_evidence_value(us_sector_context),
                "us_sector_linkage_daily/global_market_daily",
            )
        )

    if is_down and (koru_return is not None and koru_return <= -0.03 or ewy_return is not None and ewy_return <= -0.01):
        candidates.append(
            _candidate(
                0.80,
                "KORU/EWY 미국 상장 한국 ETF 약세로 단기 한국시장 심리 악화",
                "koru",
                _koru_evidence_value(koru_context),
                "koru_korea_linkage/global_market_intraday_snapshot",
            )
        )
    if is_up and (koru_score is not None and koru_score >= 0.70 or ewy_return is not None and ewy_return >= 0.01):
        candidates.append(
            _candidate(
                0.62,
                "KORU/EWY 한국 ETF 심리가 우호적으로 전환",
                "koru",
                _koru_evidence_value(koru_context),
                "koru_korea_linkage/global_market_intraday_snapshot",
            )
        )

    if is_down and (_is_semiconductor(symbol, name, sector, market) or row.get("scope") == "market"):
        if tech_signal["negative"] or _contains_any(macro_text, ["반도체", "기술주", "AI", "chip", "semiconductor", "selloff"]):
            candidates.append(
                _candidate(
                    0.92,
                    "미국 기술주·반도체 약세가 국내 대형 기술주로 전이",
                    "global_tech",
                    tech_signal["evidence"] or "반도체/기술주 뉴스 키워드 감지",
                    "global_market_daily/market_news_feed",
                )
            )
    if telegram_items and is_down and _contains_any(
        telegram_text,
        ["급락", "하락", "약세", "차익실현", "외국인", "반도체", "환율", "레버리지", "selloff", "risk"],
    ):
        candidates.append(
            _candidate(
                0.68,
                "Telegram 속보/전략 코멘트에서 하락 원인 키워드 감지",
                "telegram",
                telegram_items[:3],
                "telegram_posts",
            )
        )
    if telegram_items and is_up and _contains_any(
        telegram_text,
        ["반등", "강세", "순매수", "호조", "상향", "수혜", "risk-on", "bullish"],
    ):
        candidates.append(
            _candidate(
                0.58,
                "Telegram 속보/전략 코멘트에서 반등·강세 키워드 감지",
                "telegram",
                telegram_items[:3],
                "telegram_posts",
            )
        )
    if is_up and _is_semiconductor(symbol, name, sector, market) and tech_signal["positive"]:
        candidates.append(
            _candidate(
                0.82,
                "해외 반도체·기술주 반등과 동조",
                "global_tech",
                tech_signal["evidence"],
                "global_market_daily",
            )
        )

    foreign_1d = _safe_float(flow.get("foreign_net_value_1d_sum"))
    foreign_5d = _safe_float(flow.get("foreign_net_value_5d_sum"))
    inst_1d = _safe_float(flow.get("institution_net_value_1d_sum"))
    pension_5d = _safe_float(flow.get("pension_net_value_5d_sum"))
    if is_down and _negative(foreign_1d, foreign_5d):
        candidates.append(
            _candidate(0.86, "외국인 순매도·차익실현 압력", "flow", {"1d": foreign_1d, "5d": foreign_5d}, "investor_flows_daily")
        )
    if is_up and _positive(foreign_1d, foreign_5d):
        candidates.append(
            _candidate(0.82, "외국인 순매수 유입", "flow", {"1d": foreign_1d, "5d": foreign_5d}, "investor_flows_daily")
        )
    if is_down and _negative(inst_1d, pension_5d):
        candidates.append(
            _candidate(
                0.74,
                "기관·연기금 수급 부담",
                "flow",
                {"institution_1d": inst_1d, "pension_5d": pension_5d},
                "investor_flows_daily",
            )
        )
    if is_up and _positive(inst_1d, pension_5d):
        candidates.append(
            _candidate(
                0.72,
                "기관·연기금 매수 우위",
                "flow",
                {"institution_1d": inst_1d, "pension_5d": pension_5d},
                "investor_flows_daily",
            )
        )

    regime_reasons = [str(item) for item in regime.get("reasons") or []]
    risk_score = _safe_float(regime.get("global_risk_score"))
    if is_down and (risk_score is not None and risk_score >= 45 or regime_reasons):
        candidates.append(
            _candidate(
                0.70,
                "글로벌 위험 레짐 부담",
                "regime",
                {"risk_score": risk_score, "reasons": regime_reasons[:3]},
                "market_regime_daily",
            )
        )
    if is_up and regime.get("regime") == "risk_on":
        candidates.append(
            _candidate(
                0.58,
                "글로벌 레짐이 위험선호 쪽으로 기울어 매수 심리 보강",
                "regime",
                {"risk_score": risk_score, "regime": regime.get("regime")},
                "market_regime_daily",
            )
        )

    fx_evidence = _fx_evidence(global_markets, macro_text, regime_reasons)
    if is_down and fx_evidence:
        candidates.append(_candidate(0.66, "원화 약세·환율 부담", "fx", fx_evidence, "global_market_daily/market_news_feed"))

    if is_down and _contains_any(macro_text, ["레버리지", "leveraged", "ETF", "sidecar", "circuit breaker"]):
        candidates.append(
            _candidate(0.62, "레버리지 ETF·시장 변동성 확대 경계", "market_structure", "레버리지/ETF/거래중단 키워드", "market_news_feed")
        )

    ret_21d = _safe_float(row.get("ret_21d"))
    if is_down and ret_21d is not None and ret_21d >= 0.10:
        candidates.append(
            _candidate(0.60, "최근 상승 이후 차익실현 가능성", "momentum", {"ret_21d": ret_21d}, "prices_daily")
        )
    if is_up and ret_21d is not None and ret_21d <= -0.08:
        candidates.append(
            _candidate(0.55, "최근 낙폭 이후 기술적 반등 가능성", "momentum", {"ret_21d": ret_21d}, "prices_daily")
        )

    if stock_news:
        sentiment = _average_sentiment(stock_news)
        if is_down and (sentiment is not None and sentiment < 0.45 or _contains_any(stock_text, ["하향", "부진", "쇼크", "급락", "약세"])):
            candidates.append(
                _candidate(0.64, "종목 뉴스의 부정적 이벤트 반영", "stock_news", stock_news[:3], "news_articles")
            )
        if is_up and (sentiment is not None and sentiment > 0.55 or _contains_any(stock_text, ["상향", "호조", "수혜", "강세"])):
            candidates.append(
                _candidate(0.64, "종목 뉴스의 긍정적 이벤트 반영", "stock_news", stock_news[:3], "news_articles")
            )

    if is_down and _contains_any(macro_text, ["외국인", "차익실현", "take profit", "profit-taking"]):
        candidates.append(
            _candidate(0.58, "시장 뉴스에서 외국인 차익실현 압력 감지", "macro_news", "외국인/차익실현 키워드", "market_news_feed")
        )
    if is_up and _contains_any(macro_text, ["순매수", "반등", "risk-on", "강세"]):
        candidates.append(_candidate(0.50, "시장 뉴스의 위험선호·반등 키워드", "macro_news", "순매수/반등 키워드", "market_news_feed"))
    return candidates


def _data_quality(
    prices: pd.DataFrame,
    flows: dict[str, dict[str, Any]],
    metrics: dict[str, dict[str, Any]],
    regime: dict[str, Any],
    global_markets: dict[str, dict[str, Any]],
    macro_news: list[dict[str, Any]],
    stock_news: dict[str, list[dict[str, Any]]],
    prediction_contexts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    components = {
        "prices": "ready" if not prices.empty else "missing",
        "investor_flows": "ready" if flows else "missing",
        "market_metrics": "ready" if metrics else "missing",
        "global_regime": "ready" if regime else "missing",
        "global_markets": "ready" if global_markets else "missing",
        "market_news": "ready" if macro_news else "missing",
        "telegram_news": "ready" if any(item.get("kind") == "telegram" for item in macro_news) else "missing",
        "stock_news": "ready" if any(stock_news.values()) else "missing",
        "predictions": "ready" if prediction_contexts else "missing",
    }
    messages = [label for label, status in components.items() if status != "ready"]
    ready_count = sum(1 for status in components.values() if status == "ready")
    status = "ready" if ready_count == len(components) else ("partial_ready" if ready_count else "not_collected")
    return {
        "status": status,
        "components": components,
        "messages": [f"{label} 데이터 부족" for label in messages],
    }


def _global_tech_move(global_markets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    evidence: list[dict[str, Any]] = []
    negative = False
    positive = False
    for symbol, item in global_markets.items():
        if symbol not in GLOBAL_TECH_SYMBOLS:
            continue
        value = _safe_float(item.get("return_1d"))
        if value is None:
            continue
        evidence.append({"symbol": symbol, "return_1d": value})
        negative = negative or value <= -0.015
        positive = positive or value >= 0.015
    return {"negative": negative, "positive": positive, "evidence": evidence}


def _fx_evidence(
    global_markets: dict[str, dict[str, Any]],
    macro_text: str,
    regime_reasons: list[str],
) -> dict[str, Any] | None:
    for symbol, item in global_markets.items():
        if "KRW" not in symbol.upper() and "USD" not in symbol.upper():
            continue
        value = _safe_float(item.get("return_1d"))
        if value is not None and value >= 0.004:
            return {"symbol": symbol, "return_1d": value}
    combined = " ".join([macro_text, " ".join(regime_reasons)])
    if _contains_any(combined, ["원화 약세", "환율", "USD/KRW", "달러"]):
        return {"keyword": "환율/원화 약세"}
    return None


def _news_row(row: dict[str, Any], *, theme_key: str | None) -> dict[str, Any]:
    return {
        "title": row.get("title"),
        "summary": row.get("summary") or row.get("description"),
        "description": row.get("description"),
        "source": row.get("source") or row.get("source_name"),
        "link": row.get("link") or row.get("originallink"),
        "pub_date": _date_string(row.get("pub_date")),
        "themes": _json_list(row.get(theme_key)) if theme_key else [],
        "sentiment_score": _safe_float(row.get("sentiment_score")),
    }


def _candidate(score: float, reason: str, kind: str, value: Any, source: str) -> dict[str, Any]:
    return {
        "score": float(score),
        "reason": reason,
        "evidence": {"kind": kind, "label": reason, "value": value, "source": source},
    }


def _is_semiconductor(symbol: str, name: str, sector: str, market: str) -> bool:
    text = " ".join([symbol, name, sector, market])
    return symbol in SEMICONDUCTOR_SYMBOLS or _contains_any(text, ["반도체", "전기", "하이닉스", "삼성전자", "HBM"])


def _contains_any(text: str, keywords: list[str]) -> bool:
    lowered = str(text or "").lower()
    return any(keyword.lower() in lowered for keyword in keywords)


def _negative(*values: float | None) -> bool:
    return any(value is not None and value < 0 for value in values)


def _positive(*values: float | None) -> bool:
    return any(value is not None and value > 0 for value in values)


def _average_sentiment(items: list[dict[str, Any]]) -> float | None:
    values = [_safe_float(item.get("sentiment_score")) for item in items]
    filtered = [value for value in values if value is not None]
    if not filtered:
        return None
    return float(sum(filtered) / len(filtered))


def _prediction_evidence_value(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "horizon": context.get("horizon"),
        "side": context.get("side"),
        "pred_return": context.get("pred_return"),
        "pred_prob_top20": context.get("pred_prob_top20"),
        "pred_prob_bottom20": context.get("pred_prob_bottom20"),
        "rank": context.get("rank"),
        "gate_status": context.get("gate_status"),
    }


def _koru_evidence_value(context: dict[str, Any]) -> dict[str, Any]:
    quality = context.get("data_quality") or {}
    return {
        "trade_date": context.get("trade_date"),
        "us_signal_date": context.get("us_signal_date"),
        "koru_return_1d": context.get("koru_return_1d"),
        "ewy_return_1d": context.get("ewy_return_1d"),
        "koru_ewy_spread_1d": context.get("koru_ewy_spread_1d"),
        "koru_impact_score": context.get("koru_impact_score"),
        "signal_sources": quality.get("signal_sources"),
    }


def _us_sector_evidence_value(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "trade_date": context.get("trade_date"),
        "domestic_sector": context.get("domestic_sector"),
        "primary_proxy": context.get("primary_proxy"),
        "proxy_symbols": context.get("proxy_symbols"),
        "us_sector_return_1d": context.get("us_sector_return_1d"),
        "us_sector_return_5d": context.get("us_sector_return_5d"),
        "us_sector_zscore_20d": context.get("us_sector_zscore_20d"),
        "us_sector_beta_60d": context.get("us_sector_beta_60d"),
        "us_sector_corr_60d": context.get("us_sector_corr_60d"),
        "us_sector_impact_score": context.get("us_sector_impact_score"),
    }


def _predicted_side(row: dict[str, Any]) -> str | None:
    top = _safe_float(row.get("pred_prob_top20"))
    bottom = _safe_float(row.get("pred_prob_bottom20"))
    pred_return = _safe_float(row.get("pred_return"))
    if top is not None and bottom is not None and abs(top - bottom) >= 0.05:
        return "UP" if top > bottom else "DOWN"
    if pred_return is not None:
        if pred_return > 0:
            return "UP"
        if pred_return < 0:
            return "DOWN"
    return None


def _long_short_to_direction(side: Any) -> str | None:
    value = None if side is None else str(side).upper()
    if value == "LONG":
        return "UP"
    if value == "SHORT":
        return "DOWN"
    return value if value in {"UP", "DOWN"} else None


def _direction(move_pct: float | None) -> str:
    if move_pct is None:
        return "UNKNOWN"
    if move_pct >= DEFAULT_THRESHOLD:
        return "UP"
    if move_pct <= -DEFAULT_THRESHOLD:
        return "DOWN"
    return "FLAT"


def _safe_int(value) -> int | None:
    number = _safe_float(value)
    if number is None:
        return None
    return int(number)


def _safe_float(value) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json(value: Any) -> str:
    return json.dumps(_sanitize(value), ensure_ascii=False, allow_nan=False)


def _json_list(value) -> list[Any]:
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except (TypeError, ValueError):
        pass
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _json_dict(value) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        if pd.isna(value):
            return {}
    except (TypeError, ValueError):
        pass
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sanitize(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_sanitize(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _sanitize(value.item())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _date_string(value) -> str | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return str(pd.to_datetime(value).date())


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "asof_date",
            "scope",
            "symbol",
            "market",
            "name",
            "move_pct",
            "direction",
            "triggered",
            "primary_reason",
            "evidence_json",
            "prediction_context_json",
            "market_index_trigger_json",
            "confidence",
            "data_quality_json",
            "created_at",
        ]
    )

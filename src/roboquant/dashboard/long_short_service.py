from __future__ import annotations

import json
from typing import Any

import pandas as pd

from roboquant.db import table_exists
from roboquant.long_short import LONG_SHORT_DISCLAIMER

MARKET_ORDER = ("KOSPI", "KOSDAQ")


def get_latest_long_short(
    conn,
    horizon: str = "2M",
    market: str | None = None,
) -> dict[str, Any]:
    horizon = str(horizon)
    if not table_exists(conn, "long_short_recommendations"):
        return _empty_latest(horizon)
    latest = conn.execute(
        "SELECT MAX(asof_date) FROM long_short_recommendations WHERE horizon = ?",
        [horizon],
    ).fetchone()[0]
    if latest is None:
        return _empty_latest(horizon)

    frame = conn.execute(
        """
        SELECT
          r.*,
          COALESCE(r.market, s.market, u.market) AS market,
          COALESCE(s.name, u.name) AS name,
          COALESCE(s.sector, s.market, u.market, '기타') AS sector
        FROM long_short_recommendations AS r
        LEFT JOIN symbols AS s ON r.symbol = s.symbol
        LEFT JOIN current_prediction_universe AS u ON r.symbol = u.symbol
        WHERE r.horizon = ?
          AND r.asof_date = ?
        ORDER BY COALESCE(r.market, s.market, u.market), r.side, r.leg_rank
        """,
        [horizon, latest],
    ).fetchdf()
    if frame.empty:
        return _empty_latest(horizon)
    frame["symbol"] = frame["symbol"].astype(str).str.zfill(6)
    if "market" in frame.columns:
        frame["market"] = frame["market"].fillna("UNKNOWN").astype(str)
    records = [_recommendation_record(row) for _, row in frame.iterrows()]
    markets = _ensure_market_buckets(_group_by_market(records))
    long_leg = [record for record in records if record["side"] == "LONG"]
    short_leg = [record for record in records if record["side"] == "SHORT"]
    if market:
        normalized = str(market).upper()
        markets = {key: value for key, value in markets.items() if key == normalized}
        long_leg = [record for record in long_leg if record.get("market") == normalized]
        short_leg = [record for record in short_leg if record.get("market") == normalized]
    return {
        "horizon": horizon,
        "asof_date": _date_string(latest),
        "markets": markets,
        "long_leg": long_leg,
        "short_leg": short_leg,
        "disclaimer": LONG_SHORT_DISCLAIMER,
    }


def get_long_short_backtest(
    conn,
    horizon: str = "2M",
    market: str | None = None,
) -> dict[str, Any]:
    horizon = str(horizon)
    if not table_exists(conn, "long_short_backtest_results"):
        return _empty_backtest(horizon)
    params: list[Any] = [horizon]
    where = "WHERE horizon = ?"
    if market:
        where += " AND market = ?"
        params.append(str(market).upper())
    frame = conn.execute(
        f"""
        SELECT *
        FROM long_short_backtest_results
        {where}
        ORDER BY asof_date, market NULLS FIRST
        """,
        params,
    ).fetchdf()
    if frame.empty:
        return _empty_backtest(horizon)
    summary = _json(frame["metrics_json"].dropna().iloc[-1]) if frame["metrics_json"].notna().any() else {}
    return {
        "horizon": horizon,
        "market": str(market).upper() if market else None,
        "summary": summary,
        "curve": [_backtest_record(row) for _, row in frame.iterrows()],
        "disclaimer": LONG_SHORT_DISCLAIMER,
    }


def _ensure_market_buckets(
    markets: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    for market in MARKET_ORDER:
        markets.setdefault(market, {"long_leg": [], "short_leg": []})
    ordered: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for market in MARKET_ORDER:
        ordered[market] = markets[market]
    for market, bucket in markets.items():
        if market not in ordered:
            ordered[market] = bucket
    return ordered


def _group_by_market(records: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for record in records:
        market = str(record.get("market") or "UNKNOWN")
        bucket = grouped.setdefault(market, {"long_leg": [], "short_leg": []})
        if record["side"] == "LONG":
            bucket["long_leg"].append(record)
        else:
            bucket["short_leg"].append(record)
    ordered: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for market in MARKET_ORDER:
        if market in grouped:
            ordered[market] = grouped[market]
    for market in sorted(grouped):
        if market not in ordered:
            ordered[market] = grouped[market]
    return ordered


def _recommendation_record(row: pd.Series) -> dict[str, Any]:
    return {
        "asof_date": _date_string(row.get("asof_date")),
        "horizon": row.get("horizon"),
        "symbol": row.get("symbol"),
        "name": row.get("name") if pd.notna(row.get("name")) else "",
        "market": row.get("market") if pd.notna(row.get("market")) else "",
        "sector": row.get("sector") if pd.notna(row.get("sector")) else "",
        "side": row.get("side"),
        "rank": _number(row.get("leg_rank"), integer=True),
        "weight": _number(row.get("weight")),
        "long_score": _number(row.get("long_score")),
        "short_score": _number(row.get("short_score")),
        "pred_return": _number(row.get("pred_return")),
        "pred_prob_top20": _number(row.get("pred_prob_top20")),
        "pred_prob_bottom20": _number(row.get("pred_prob_bottom20")),
        "risk_score": _number(row.get("risk_score")),
        "confidence": _number(row.get("confidence")),
        "reasons": _json(row.get("reason_json"), []),
        "risk_flags": _json(row.get("risk_flags_json"), []),
        "model_version": row.get("model_version"),
    }


def _backtest_record(row: pd.Series) -> dict[str, Any]:
    return {
        "asof_date": _date_string(row.get("asof_date")),
        "horizon": row.get("horizon"),
        "market": row.get("market") if pd.notna(row.get("market")) else None,
        "long_symbols": _split_symbols(row.get("long_symbols")),
        "short_symbols": _split_symbols(row.get("short_symbols")),
        "long_return": _number(row.get("long_return")),
        "short_return": _number(row.get("short_return")),
        "gross_spread_return": _number(row.get("gross_spread_return")),
        "transaction_cost": _number(row.get("transaction_cost")),
        "net_return": _number(row.get("net_return")),
        "turnover": _number(row.get("turnover")),
        "equity": _number(row.get("equity")),
        "model_version": row.get("model_version"),
    }


def _empty_latest(horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "asof_date": None,
        "markets": {market: {"long_leg": [], "short_leg": []} for market in MARKET_ORDER},
        "long_leg": [],
        "short_leg": [],
        "disclaimer": LONG_SHORT_DISCLAIMER,
    }


def _empty_backtest(horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "market": None,
        "summary": {},
        "curve": [],
        "disclaimer": LONG_SHORT_DISCLAIMER,
    }


def _json(value, default=None):
    default = [] if default is None else default
    if value is None or pd.isna(value):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _split_symbols(value) -> list[str]:
    if value is None or pd.isna(value):
        return []
    return [part for part in str(value).split(",") if part]


def _number(value, integer: bool = False):
    if value is None or pd.isna(value):
        return None
    return int(value) if integer else float(value)


def _date_string(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(pd.to_datetime(value).date())

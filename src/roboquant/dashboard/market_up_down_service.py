from __future__ import annotations

import json
from typing import Any

import pandas as pd

from roboquant.db import table_exists
from roboquant.market_up_down import MARKET_UP_DOWN_DISCLAIMER

MARKET_ORDER = ("KOSPI", "KOSDAQ")


def get_latest_market_up_down(
    conn,
    horizon: str = "2M",
    market: str | None = None,
) -> dict[str, Any]:
    horizon = str(horizon)
    if not table_exists(conn, "market_up_down_recommendations"):
        return _empty_latest(horizon)
    latest = conn.execute(
        "SELECT MAX(asof_date) FROM market_up_down_recommendations WHERE horizon = ?",
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
        FROM market_up_down_recommendations AS r
        LEFT JOIN symbols AS s ON r.symbol = s.symbol
        LEFT JOIN current_prediction_universe AS u ON r.symbol = u.symbol
        WHERE r.horizon = ?
          AND r.asof_date = ?
        ORDER BY COALESCE(r.market, s.market, u.market), r.side, r.rank
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
    upside = [record for record in records if record["side"] == "UP"]
    downside = [record for record in records if record["side"] == "DOWN"]
    if market:
        normalized = str(market).upper()
        markets = {key: value for key, value in markets.items() if key == normalized}
        upside = [record for record in upside if record.get("market") == normalized]
        downside = [record for record in downside if record.get("market") == normalized]
    return {
        "horizon": horizon,
        "asof_date": _date_string(latest),
        "markets": markets,
        "upside": upside,
        "downside": downside,
        "disclaimer": MARKET_UP_DOWN_DISCLAIMER,
    }


def _ensure_market_buckets(
    markets: dict[str, dict[str, list[dict[str, Any]]]],
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    for market in MARKET_ORDER:
        markets.setdefault(market, {"upside": [], "downside": []})
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
        bucket = grouped.setdefault(market, {"upside": [], "downside": []})
        if record["side"] == "UP":
            bucket["upside"].append(record)
        else:
            bucket["downside"].append(record)
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
        "rank": _number(row.get("rank"), integer=True),
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


def _empty_latest(horizon: str) -> dict[str, Any]:
    return {
        "horizon": horizon,
        "asof_date": None,
        "markets": {market: {"upside": [], "downside": []} for market in MARKET_ORDER},
        "upside": [],
        "downside": [],
        "disclaimer": MARKET_UP_DOWN_DISCLAIMER,
    }


def _json(value, default=None):
    default = [] if default is None else default
    if value is None or pd.isna(value):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _number(value, integer: bool = False):
    if value is None or pd.isna(value):
        return None
    return int(value) if integer else float(value)


def _date_string(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(pd.to_datetime(value).date())

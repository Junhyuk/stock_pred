from __future__ import annotations

import json
from typing import Any

import pandas as pd

from roboquant.db import table_exists

FLOW_THEMES = {"PENSION_FLOW", "FLOW"}


def get_recent_market_news(
    conn,
    *,
    lookback_days: int = 14,
    limit: int = 12,
) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_news_feed"):
        return []
    cutoff = (pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=int(lookback_days))).to_pydatetime()
    frame = conn.execute(
        """
        SELECT *
        FROM market_news_feed
        WHERE pub_date >= ?
        ORDER BY pub_date DESC NULLS LAST, collected_at DESC NULLS LAST
        LIMIT ?
        """,
        [cutoff, int(limit)],
    ).fetchdf()
    if frame.empty:
        return []
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        records.append(
            {
                "article_id": row.get("article_id"),
                "source": row.get("source"),
                "category": row.get("category"),
                "title": row.get("title"),
                "summary": row.get("summary"),
                "link": row.get("link"),
                "pub_date": _date_string(row.get("pub_date")),
                "themes": _json_list(row.get("themes_json")),
                "sentiment_score": _number(row.get("sentiment_score")),
            }
        )
    return records


def active_flow_themes(conn, *, lookback_days: int = 21) -> set[str]:
    themes: set[str] = set()
    for item in get_recent_market_news(conn, lookback_days=lookback_days, limit=30):
        for theme in item.get("themes") or []:
            themes.add(str(theme))
    return themes


def apply_flow_news_risk_flags(recommendations: pd.DataFrame, themes: set[str]) -> pd.DataFrame:
    if recommendations.empty or not themes.intersection(FLOW_THEMES):
        return recommendations
    out = recommendations.copy()
    flags: list[str] = []
    if "PENSION_FLOW" in themes:
        flags.append("국민연금 리밸런싱 매도 압력(거시)")
    elif "FLOW" in themes:
        flags.append("수급/기관 매매 이슈(거시)")

    def _merge_flags(raw: object, side: str, market: object) -> str:
        existing = _json_list(raw)
        extra = list(flags)
        if side in {"LONG", "UP"} and str(market or "") == "KOSPI" and "PENSION_FLOW" in themes:
            extra.append("대형주 순매도 우려")
        merged = existing + [flag for flag in extra if flag not in existing]
        return json.dumps(merged, ensure_ascii=False)

    out["risk_flags_json"] = [
        _merge_flags(row.get("risk_flags_json"), row.get("side"), row.get("market"))
        for _, row in out.iterrows()
    ]
    return out


def _json_list(value) -> list[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _number(value):
    if value is None or pd.isna(value):
        return None
    return float(value)


def _date_string(value) -> str | None:
    if value is None or pd.isna(value):
        return None
    return str(pd.to_datetime(value).date())

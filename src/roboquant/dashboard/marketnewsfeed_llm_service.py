"""Read-only payloads for the local MarketNewsFeed LLM shadow view."""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from roboquant.db import table_exists

DISCLAIMER = "로컬 LLM 요약과 신호는 연구·정보제공용 shadow 검증이며 production 추천에는 반영되지 않습니다."


def get_marketnewsfeed_llm_status(conn, *, limit: int = 30, aggregate_only: bool = False) -> dict[str, Any]:
    if not table_exists(conn, "marketnewsfeed_llm_runs"):
        return _empty("LLM schema is unavailable")
    latest_run = _latest_run(conn)
    counts = _counts(conn)
    signals = _signal_summary(conn)
    shadows = _shadow_summary(conn)
    market_ranges = _market_range_summary(conn)
    status = str(latest_run.get("status") or "not_ready")
    payload = {
        "status": status,
        "production_weight": 0.0,
        "latest_run": latest_run,
        "counts": counts,
        "signals": signals,
        "shadow": shadows,
        "market_range_shadow": market_ranges,
        "disclaimer": DISCLAIMER,
    }
    if aggregate_only:
        return _aggregate_payload(payload)
    payload["items"] = _items(conn, limit=max(1, min(int(limit), 30)))
    return payload


def _aggregate_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep GitHub Pages to counts and gate state, never local paths or signals."""
    gates = [
        {
            key: item.get(key)
            for key in ("horizon", "signal_days", "mature_prediction_dates", "gate_status", "fail_reason", "production_weight")
        }
        for item in payload.get("shadow", [])
    ]
    ranges = [
        {
            key: item.get(key)
            for key in ("market", "coverage_status", "range_low", "range_high", "production_weight")
        }
        for item in payload.get("market_range_shadow", [])
    ]
    latest = payload.get("latest_run") or {}
    return {
        "status": payload["status"],
        "production_weight": 0.0,
        "latest_run": {
            key: latest.get(key)
            for key in ("model_name", "prompt_version", "input_tokens", "output_tokens", "status", "completed_at")
        },
        "counts": payload["counts"],
        "signals": {"status": (payload.get("signals") or {}).get("status", "not_ready")},
        "shadow": gates,
        "market_range_shadow": ranges,
        "disclaimer": payload["disclaimer"],
    }


def _latest_run(conn) -> dict[str, Any]:
    frame = conn.execute(
        """
        SELECT *
        FROM marketnewsfeed_llm_runs
        ORDER BY completed_at DESC NULLS LAST, started_at DESC
        LIMIT 1
        """
    ).fetchdf()
    return _record(frame.iloc[0].to_dict()) if not frame.empty else {"status": "not_ready"}


def _counts(conn) -> dict[str, int]:
    tables = {
        "headlines": "marketnewsfeed_headlines",
        "assessments": "marketnewsfeed_llm_assessments",
        "signal_days": "marketnewsfeed_llm_signal_daily",
    }
    values: dict[str, int] = {}
    for key, table in tables.items():
        if not table_exists(conn, table):
            values[key] = 0
            continue
        if key == "signal_days":
            values[key] = int(conn.execute(f"SELECT COUNT(DISTINCT signal_date) FROM {table}").fetchone()[0])
        else:
            values[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return values


def _signal_summary(conn) -> dict[str, Any]:
    if not table_exists(conn, "marketnewsfeed_llm_signal_daily"):
        return {"status": "not_ready", "items": []}
    frame = conn.execute(
        """
        SELECT *
        FROM marketnewsfeed_llm_signal_daily
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY signal_date, scope, symbol
          ORDER BY created_at DESC
        ) = 1
        ORDER BY signal_date DESC, scope, symbol
        LIMIT 24
        """
    ).fetchdf()
    return {"status": "ready" if not frame.empty else "not_ready", "items": [_record(row) for row in frame.to_dict(orient="records")]}


def _shadow_summary(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "marketnewsfeed_llm_shadow_runs"):
        return []
    frame = conn.execute(
        """
        SELECT *
        FROM marketnewsfeed_llm_shadow_runs
        QUALIFY ROW_NUMBER() OVER (PARTITION BY horizon ORDER BY created_at DESC) = 1
        ORDER BY horizon
        """
    ).fetchdf()
    records = []
    for row in frame.to_dict(orient="records"):
        item = _record(row)
        item["baseline_metrics"] = _loads(item.pop("baseline_metrics_json", None), {})
        item["candidate_metrics"] = _loads(item.pop("candidate_metrics_json", None), {})
        records.append(item)
    return records


def _market_range_summary(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "marketnewsfeed_llm_market_shadow"):
        return []
    frame = conn.execute(
        """
        SELECT *
        FROM marketnewsfeed_llm_market_shadow
        QUALIFY ROW_NUMBER() OVER (PARTITION BY market ORDER BY signal_date DESC, created_at DESC) = 1
        ORDER BY CASE market WHEN 'KOSPI' THEN 1 WHEN 'KOSDAQ' THEN 2 ELSE 3 END
        """
    ).fetchdf()
    return [_record(row) for row in frame.to_dict(orient="records")]


def _items(conn, limit: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "marketnewsfeed_llm_assessments"):
        return []
    frame = conn.execute(
        """
        SELECT
          a.headline_id,
          h.title AS headline,
          h.source_url,
          h.published_at,
          a.summary_ko,
          a.event_type,
          a.direction,
          a.importance,
          a.confidence,
          a.themes_json,
          a.mapped_symbols_json,
          a.model_name,
          a.model_digest,
          a.prompt_version,
          a.input_tokens,
          a.output_tokens,
          a.latency_ms,
          a.thinking_used,
          a.status,
          a.error_message,
          a.assessed_at
        FROM marketnewsfeed_llm_assessments AS a
        INNER JOIN marketnewsfeed_headlines AS h ON h.headline_id = a.headline_id
        ORDER BY h.published_at DESC NULLS LAST, a.assessed_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchdf()
    records = []
    for row in frame.to_dict(orient="records"):
        item = _record(row)
        item["themes"] = _loads(item.pop("themes_json", None), [])
        item["mapped_symbols"] = _loads(item.pop("mapped_symbols_json", None), [])
        records.append(item)
    return records


def _empty(reason: str) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "production_weight": 0.0,
        "latest_run": {"status": "not_ready", "error_message": reason},
        "counts": {"headlines": 0, "assessments": 0, "signal_days": 0},
        "signals": {"status": "not_ready", "items": []},
        "shadow": [],
        "market_range_shadow": [],
        "items": [],
        "disclaimer": DISCLAIMER,
    }


def _record(value: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, pd.Timestamp):
            output[key] = item.isoformat()
        elif hasattr(item, "isoformat"):
            output[key] = item.isoformat()
        elif pd.isna(item) if not isinstance(item, (list, dict, str, bool)) else False:
            output[key] = None
        else:
            output[key] = item
    return output


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed

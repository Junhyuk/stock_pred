"""Read-only local LLM news payloads for the local web and aggregate export."""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

from roboquant.db import table_exists
from roboquant.market_outlook_ml_shadow import get_market_outlook_ml_shadow

DISCLAIMER = "로컬 LLM 신호와 LightGBM 범위는 연구·정보제공용 shadow 검증이며 production 시장 예측을 변경하지 않습니다."


def get_local_llm_news_status(conn, *, limit: int = 30, aggregate_only: bool = False) -> dict[str, Any]:
    if not table_exists(conn, "local_llm_news_runs"):
        return _empty("local LLM schema is unavailable")
    latest_run = _latest_run(conn)
    payload = {
        "status": str(latest_run.get("status") or "not_ready"),
        "production_weight": 0.0,
        "latest_run": latest_run,
        "counts": _counts(conn),
        "sources": _sources(conn),
        "signals": _signals(conn),
        "brief": _brief(conn),
        "shadow": get_market_outlook_ml_shadow(conn),
        "attributions": _attributions(conn),
        "disclaimer": DISCLAIMER,
    }
    if aggregate_only:
        return _aggregate(payload)
    payload["items"] = _items(conn, limit=max(1, min(int(limit), 30)))
    return payload


def _latest_run(conn) -> dict[str, Any]:
    frame = conn.execute(
        """
        SELECT * FROM local_llm_news_runs
        ORDER BY completed_at DESC NULLS LAST, started_at DESC
        LIMIT 1
        """
    ).fetchdf()
    return _record(frame.iloc[0].to_dict()) if not frame.empty else {"status": "not_ready"}


def _counts(conn) -> dict[str, int]:
    query_map = {
        "documents": "local_llm_news_documents",
        "assessments": "local_llm_news_assessments",
        "signal_days": "local_llm_news_signal_daily",
        "briefs": "local_llm_news_briefs",
    }
    values: dict[str, int] = {}
    for key, table in query_map.items():
        if not table_exists(conn, table):
            values[key] = 0
        elif key == "signal_days":
            values[key] = int(conn.execute(f"SELECT COUNT(DISTINCT signal_date) FROM {table}").fetchone()[0])
        else:
            values[key] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    return values


def _sources(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "local_llm_news_documents"):
        return []
    frame = conn.execute(
        """
        SELECT
          d.source,
          COUNT(*) AS document_count,
          COUNT(a.assessment_id) FILTER (WHERE a.status = 'ready') AS assessment_count,
          MAX(d.available_at) AS latest_available_at
        FROM local_llm_news_documents AS d
        LEFT JOIN local_llm_news_assessments AS a ON a.document_id = d.document_id
        GROUP BY d.source
        ORDER BY d.source
        """
    ).fetchdf()
    return [_record(row) for row in frame.to_dict(orient="records")]


def _signals(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "local_llm_news_signal_daily"):
        return []
    frame = conn.execute(
        """
        SELECT *
        FROM local_llm_news_signal_daily
        WHERE scope = 'market' AND symbol = 'ALL'
        QUALIFY ROW_NUMBER() OVER (PARTITION BY signal_date ORDER BY created_at DESC) = 1
        ORDER BY signal_date DESC
        LIMIT 10
        """
    ).fetchdf()
    records = []
    for row in frame.to_dict(orient="records"):
        item = _record(row)
        item["source_coverage"] = _loads(item.pop("source_coverage_json", None), {})
        item.pop("evidence_json", None)
        records.append(item)
    return records


def _brief(conn) -> dict[str, Any]:
    if not table_exists(conn, "local_llm_news_briefs"):
        return {"status": "not_ready"}
    frame = conn.execute(
        """
        SELECT * FROM local_llm_news_briefs
        ORDER BY brief_date DESC, created_at DESC
        LIMIT 1
        """
    ).fetchdf()
    return _record(frame.iloc[0].to_dict()) if not frame.empty else {"status": "not_ready"}


def _items(conn, limit: int) -> list[dict[str, Any]]:
    if not table_exists(conn, "local_llm_news_assessments"):
        return []
    frame = conn.execute(
        """
        SELECT
          a.source, d.title, d.source_url, d.available_at,
          a.summary_ko, a.event_type, a.direction, a.importance, a.confidence,
          a.themes_json, a.mapped_symbols_json, a.input_tokens, a.output_tokens,
          a.latency_ms, a.thinking_used, a.status, a.error_message, a.assessed_at
        FROM local_llm_news_assessments AS a
        INNER JOIN local_llm_news_documents AS d ON d.document_id = a.document_id
        ORDER BY d.available_at DESC NULLS LAST, a.assessed_at DESC
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


def _attributions(conn) -> list[dict[str, Any]]:
    if not table_exists(conn, "market_outlook_feature_attributions"):
        return []
    frame = conn.execute(
        """
        SELECT *
        FROM market_outlook_feature_attributions
        WHERE component IN ('ridge_index', 'ridge_breadth', 'lgbm_q50_shadow')
        QUALIFY ROW_NUMBER() OVER (
          PARTITION BY market, component, feature_name
          ORDER BY asof_date DESC, created_at DESC
        ) = 1
        ORDER BY market, component, ABS(COALESCE(contribution, 0)) DESC
        LIMIT 80
        """
    ).fetchdf()
    return [_record(row) for row in frame.to_dict(orient="records")]


def _aggregate(payload: dict[str, Any]) -> dict[str, Any]:
    latest = payload.get("latest_run") or {}
    shadow = payload.get("shadow") or {}
    return {
        "status": payload["status"],
        "production_weight": 0.0,
        "latest_run": {
            key: latest.get(key)
            for key in (
                "stage",
                "model_name",
                "model_digest",
                "prompt_version",
                "requested_document_count",
                "assessed_document_count",
                "cached_document_count",
                "input_tokens",
                "output_tokens",
                "thinking_probe_count",
                "status",
                "completed_at",
            )
        },
        "counts": payload.get("counts") or {},
        "sources": [
            {key: item.get(key) for key in ("source", "document_count", "assessment_count")}
            for item in payload.get("sources") or []
        ],
        "shadow": {
            "status": shadow.get("status"),
            "production_weight": 0.0,
            "items": [
                {
                    key: item.get(key)
                    for key in (
                        "market",
                        "gate_status",
                        "calibration_count",
                        "feature_coverage",
                        "production_weight",
                    )
                }
                for item in shadow.get("items") or []
            ],
            "runs": [
                {
                    key: item.get(key)
                    for key in ("market", "signal_days", "mature_prediction_dates", "gate_status", "production_weight", "fail_reason")
                }
                for item in shadow.get("runs") or []
            ],
        },
        "disclaimer": payload["disclaimer"],
    }


def _empty(reason: str) -> dict[str, Any]:
    return {
        "status": "not_ready",
        "production_weight": 0.0,
        "latest_run": {"status": "not_ready", "error_message": reason},
        "counts": {"documents": 0, "assessments": 0, "signal_days": 0, "briefs": 0},
        "sources": [],
        "signals": [],
        "brief": {"status": "not_ready"},
        "shadow": {"status": "not_ready", "items": [], "runs": [], "production_weight": 0.0},
        "attributions": [],
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
        elif isinstance(item, float) and (math.isnan(item) or math.isinf(item)):
            output[key] = None
        else:
            output[key] = item
    return output


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default

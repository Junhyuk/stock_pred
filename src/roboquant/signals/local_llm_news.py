"""Local-only structured news signals from approved, already-collected metadata.

This module never fetches article bodies or follows links. It accepts only the
title, short source summary, timestamp, source metadata, and deterministic
symbol tags already present in DuckDB. Local Ollama output is persisted only
after schema validation; hidden reasoning and raw completions are discarded.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.config import load_config
from roboquant.db import append_dedup_table, table_exists
from roboquant.signals.marketnewsfeed_llm import (
    ALLOWED_DIRECTIONS,
    ALLOWED_EVENT_TYPES,
    ALLOWED_THEMES,
    RISK_THINKING_WORDS,
    LocalOllamaClient,
    OllamaUnavailableError,
)
from roboquant.signals.news_signals import DEFAULT_LOCAL_TICKER_ALIASES
from roboquant.signals.telegram_text import classify_themes, extract_tickers

KST = ZoneInfo("Asia/Seoul")
AI_THEME_PATTERN = re.compile(r"\b(?:ai|gpu|llm|nvidia)\b|인공지능|데이터센터|엔비디아", re.IGNORECASE)


@dataclass(frozen=True)
class LocalLlmNewsSettings:
    enabled: bool = True
    ollama_host: str = "http://127.0.0.1:11434"
    model_name: str = "roboquant-news:qwen3.5-27b-q4"
    prompt_version: str = "local-news-v1"
    request_timeout_seconds: float = 180.0
    market_open_cutoff_kst: str = "08:00"
    document_max_chars: int = 320
    batch_size: int = 6
    num_ctx: int = 4096
    max_output_tokens: int = 180
    thinking_mode: str = "auto"
    thinking_importance_threshold: int = 4
    max_thinking_probes_per_day: int = 4
    close_max_documents: int = 30
    premarket_max_documents: int = 12
    daily_brief_enabled: bool = True
    daily_brief_max_output_tokens: int = 160
    sources: Mapping[str, bool] | None = None


def load_local_llm_news_config(path: str | Path = "configs/local_llm_news.yaml") -> dict[str, Any]:
    """Layer the bounded local-LLM config over the regular market config."""
    overlay = load_config(path)
    raw = overlay.get("local_llm_news") or {}
    base_ref = str(overlay.get("base_config") or raw.get("base_config") or "").strip()
    if not base_ref:
        return overlay
    root = Path(overlay["_root"])
    candidate = Path(base_ref).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate if candidate.parts[:1] == ("configs",) else root / "configs" / candidate
    base = load_config(candidate)
    patch = {key: value for key, value in overlay.items() if key != "_root"}
    merged = _deep_merge(base, patch)
    merged["_root"] = overlay["_root"]
    return merged


def settings_from_config(config: Mapping[str, Any] | None) -> LocalLlmNewsSettings:
    raw = dict((config or {}).get("local_llm_news") or {})
    defaults = LocalLlmNewsSettings()
    values = {
        "enabled": raw.get("enabled", defaults.enabled),
        "ollama_host": raw.get("ollama_host", defaults.ollama_host),
        "model_name": raw.get("model_name", defaults.model_name),
        "prompt_version": raw.get("prompt_version", defaults.prompt_version),
        "request_timeout_seconds": raw.get("request_timeout_seconds", defaults.request_timeout_seconds),
        "market_open_cutoff_kst": raw.get("market_open_cutoff_kst", defaults.market_open_cutoff_kst),
        "document_max_chars": raw.get("document_max_chars", defaults.document_max_chars),
        "batch_size": raw.get("batch_size", defaults.batch_size),
        "num_ctx": raw.get("num_ctx", defaults.num_ctx),
        "max_output_tokens": raw.get("max_output_tokens", defaults.max_output_tokens),
        "thinking_mode": raw.get("thinking_mode", defaults.thinking_mode),
        "thinking_importance_threshold": raw.get("thinking_importance_threshold", defaults.thinking_importance_threshold),
        "max_thinking_probes_per_day": raw.get("max_thinking_probes_per_day", defaults.max_thinking_probes_per_day),
        "close_max_documents": raw.get("close_max_documents", defaults.close_max_documents),
        "premarket_max_documents": raw.get("premarket_max_documents", defaults.premarket_max_documents),
        "daily_brief_enabled": dict(raw.get("daily_brief") or {}).get("enabled", defaults.daily_brief_enabled),
        "daily_brief_max_output_tokens": dict(raw.get("daily_brief") or {}).get(
            "max_output_tokens", defaults.daily_brief_max_output_tokens
        ),
        "sources": dict(raw.get("sources") or {}),
    }
    values["request_timeout_seconds"] = max(1.0, float(values["request_timeout_seconds"]))
    values["document_max_chars"] = max(80, min(int(values["document_max_chars"]), 320))
    values["batch_size"] = max(1, min(int(values["batch_size"]), 6))
    values["num_ctx"] = max(512, min(int(values["num_ctx"]), 4096))
    values["max_output_tokens"] = max(48, min(int(values["max_output_tokens"]), 180))
    values["thinking_importance_threshold"] = max(1, min(int(values["thinking_importance_threshold"]), 5))
    values["max_thinking_probes_per_day"] = max(0, min(int(values["max_thinking_probes_per_day"]), 4))
    values["close_max_documents"] = max(1, min(int(values["close_max_documents"]), 30))
    values["premarket_max_documents"] = max(1, min(int(values["premarket_max_documents"]), 12))
    values["daily_brief_max_output_tokens"] = max(48, min(int(values["daily_brief_max_output_tokens"]), 160))
    return LocalLlmNewsSettings(**values)


def refresh_local_llm_documents(conn, config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize approved source metadata into a small, auditable local queue."""
    settings = settings_from_config(config)
    now = _utcnow()
    if not settings.enabled:
        return {"status": "disabled", "rows": 0, "sources": {}}
    frames = _source_frames(conn, settings)
    records: list[dict[str, Any]] = []
    source_counts: Counter[str] = Counter()
    for source, frame in frames:
        if frame.empty:
            continue
        for row in frame.to_dict(orient="records"):
            title = _clip_text(row.get("title"), settings.document_max_chars)
            summary = _clip_text(row.get("summary"), settings.document_max_chars)
            if not title:
                continue
            source_id = str(row.get("source_document_id") or "").strip()
            if not source_id:
                continue
            digest = _digest_text(f"{title}\n{summary}")
            # A changed source item is a new cacheable document version.
            versioned_source_id = f"{source_id}:{digest[:16]}"
            tags = _loads(row.get("source_tags_json"), {})
            document_id = _digest_text(f"{source}|{versioned_source_id}")
            records.append(
                {
                    "document_id": document_id,
                    "source": source,
                    "source_document_id": versioned_source_id,
                    "published_at": _to_utc_naive(row.get("published_at")),
                    "collected_at": _to_utc_naive(row.get("collected_at")),
                    "available_at": _latest_available_at(row.get("published_at"), row.get("collected_at")),
                    "title": title,
                    "summary": summary or None,
                    "source_url": _clean_url(row.get("source_url")),
                    "source_tags_json": _json(tags if isinstance(tags, Mapping) else {}),
                    "content_digest": digest,
                    "status": "ready",
                    "created_at": now,
                }
            )
            source_counts[source] += 1
    if records:
        append_dedup_table(
            conn,
            "local_llm_news_documents",
            pd.DataFrame(records),
            ["document_id"],
        )
    return {"status": "ready", "rows": len(records), "sources": dict(sorted(source_counts.items()))}


def summarize_local_llm_news(
    conn,
    config: Mapping[str, Any] | None,
    *,
    stage: str,
    limit: int | None = None,
    client: LocalOllamaClient | None = None,
) -> dict[str, Any]:
    """Classify newly queued metadata with local Ollama and persist final JSON only."""
    settings = settings_from_config(config)
    run_id = uuid4().hex
    started = _utcnow()
    stage = _validate_stage(stage)
    if not settings.enabled:
        return _record_run(conn, run_id, settings, stage, "disabled", started, error_message="local_llm_news.enabled=false")
    if not table_exists(conn, "local_llm_news_documents"):
        return _record_run(conn, run_id, settings, stage, "not_ready", started, error_message="local_llm_news_documents unavailable")

    local_client = client or LocalOllamaClient(settings.ollama_host, settings.request_timeout_seconds)
    try:
        capability = local_client.probe(settings.model_name)
    except (OllamaUnavailableError, ValueError) as exc:
        status = "model_import_failed" if "model_import_failed" in str(exc) else "unavailable"
        return _record_run(conn, run_id, settings, stage, status, started, error_message=str(exc))

    model_digest = str(capability["model_digest"])
    max_documents = settings.close_max_documents if stage == "close" else settings.premarket_max_documents
    requested_limit = max(1, min(int(limit or max_documents), max_documents))
    documents = _load_uncached_documents(conn, settings, model_digest, requested_limit)
    cached_count = _cached_count(conn, settings, model_digest)
    if documents.empty:
        return _record_run(
            conn,
            run_id,
            settings,
            stage,
            "ready",
            started,
            model_digest=model_digest,
            cached_count=cached_count,
        )

    known_symbols = _known_local_symbols(conn)
    today_probe_count = _today_thinking_probe_count(conn, settings)
    chunks = _thinking_chunks(
        documents.to_dict(orient="records"),
        settings,
        remaining_probes=max(0, settings.max_thinking_probes_per_day - today_probe_count),
    )
    records: list[dict[str, Any]] = []
    failures: list[str] = []
    input_tokens = output_tokens = latency_ms = thinking_probe_count = 0
    for uses_thinking, chunk in chunks:
        try:
            for actual_thinking, actual_chunk, payload, usage, elapsed in _chat_assessment_batch(
                local_client,
                chunk,
                settings,
                thinking=uses_thinking,
            ):
                input_tokens += int(usage.get("input_tokens") or 0)
                output_tokens += int(usage.get("output_tokens") or 0)
                latency_ms += elapsed
                thinking_probe_count += int(actual_thinking)
                records.extend(
                    _assessment_records(
                        actual_chunk,
                        payload,
                        run_id=run_id,
                        settings=settings,
                        model_digest=model_digest,
                        known_symbols=known_symbols,
                        input_tokens=int(usage.get("input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                        latency_ms=elapsed,
                        thinking_used=actual_thinking,
                    )
                )
        except (OllamaUnavailableError, ValueError, KeyError, TypeError) as exc:
            failures.append(str(exc))

    if records:
        append_dedup_table(conn, "local_llm_news_assessments", pd.DataFrame(records), ["assessment_id"])
    status = "ready" if records and not failures else "partial_ready" if records else "failed"
    return _record_run(
        conn,
        run_id,
        settings,
        stage,
        status,
        started,
        model_digest=model_digest,
        requested_count=len(documents),
        assessed_count=len(records),
        cached_count=cached_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        thinking_probe_count=thinking_probe_count,
        error_message="; ".join(failures[:3]) or None,
    )


def build_local_llm_news_signal_daily(
    conn,
    config: Mapping[str, Any] | None,
    *,
    feature_version: str | None = None,
) -> pd.DataFrame:
    """Build cutoff-safe market and stock signals without inventing missing rows."""
    settings = settings_from_config(config)
    version = feature_version or f"{settings.prompt_version}-signals-v1"
    if not table_exists(conn, "local_llm_news_assessments") or not table_exists(conn, "local_llm_news_documents"):
        return pd.DataFrame(columns=_signal_columns())
    frame = conn.execute(
        """
        SELECT
          a.document_id, a.source, a.direction, a.importance, a.confidence,
          a.themes_json, a.mapped_symbols_json, d.title, d.summary, d.available_at
        FROM local_llm_news_assessments AS a
        INNER JOIN local_llm_news_documents AS d ON d.document_id = a.document_id
        WHERE a.status = 'ready'
        ORDER BY d.available_at, a.assessed_at
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=_signal_columns())
    frame[["title", "summary"]] = frame[["title", "summary"]].fillna("")
    frame["themes_json"] = frame.apply(
        lambda row: _json(_validated_themes(_loads(row["themes_json"], []), f"{row['title']} {row['summary']}")),
        axis=1,
    )
    trading_dates = _price_dates(conn)
    if not trading_dates:
        return pd.DataFrame(columns=_signal_columns())
    cutoff = _cutoff_time(settings.market_open_cutoff_kst)
    frame["signal_date"] = frame["available_at"].map(
        lambda value: _next_available_trade_date(value, cutoff=cutoff, trading_dates=trading_dates)
    )
    frame = frame[frame["signal_date"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=_signal_columns())
    rows = _build_signal_rows(frame, version)
    signals = pd.DataFrame(rows, columns=_signal_columns())
    if not signals.empty:
        append_dedup_table(
            conn,
            "local_llm_news_signal_daily",
            signals,
            ["signal_date", "scope", "symbol", "feature_version"],
        )
    return signals


def generate_local_llm_market_brief(
    conn,
    config: Mapping[str, Any] | None,
    *,
    stage: str,
    market_context: Mapping[str, Any] | None = None,
    client: LocalOllamaClient | None = None,
) -> dict[str, Any]:
    """Create one concise Korean brief from structured signals and ML context only."""
    settings = settings_from_config(config)
    stage = _validate_stage(stage)
    now = _utcnow()
    if not settings.daily_brief_enabled:
        return {"status": "disabled"}
    local_client = client or LocalOllamaClient(settings.ollama_host, settings.request_timeout_seconds)
    try:
        capability = local_client.probe(settings.model_name)
    except (OllamaUnavailableError, ValueError) as exc:
        return _record_brief(conn, settings, stage, now.date(), "unavailable", error_message=str(exc))
    latest = _latest_structured_signal_payload(conn)
    if not latest["items"]:
        return _record_brief(conn, settings, stage, now.date(), "not_ready", model_digest=str(capability["model_digest"]))
    messages = [
        {
            "role": "system",
            "content": "Summarize structured market signals in concise Korean. Do not give buy/sell advice or reveal hidden reasoning.",
        },
        {
            "role": "user",
            "content": _json({"signals": latest["items"], "market_ml": dict(market_context or {})}),
        },
    ]
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary_ko"],
        "properties": {"summary_ko": {"type": "string", "maxLength": 500}},
    }
    try:
        payload, usage = local_client.chat(
            model_name=settings.model_name,
            messages=messages,
            schema=schema,
            num_ctx=settings.num_ctx,
            max_output_tokens=settings.daily_brief_max_output_tokens,
            thinking=False,
        )
    except (OllamaUnavailableError, ValueError, KeyError, TypeError) as exc:
        return _record_brief(conn, settings, stage, now.date(), "failed", model_digest=str(capability["model_digest"]), error_message=str(exc))
    summary = _clip_text(payload.get("summary_ko"), 500)
    return _record_brief(
        conn,
        settings,
        stage,
        now.date(),
        "ready" if summary else "failed",
        model_digest=str(capability["model_digest"]),
        summary_ko=summary or None,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        error_message=None if summary else "empty structured brief",
    )


def _source_frames(conn, settings: LocalLlmNewsSettings) -> list[tuple[str, pd.DataFrame]]:
    enabled = dict(settings.sources or {})
    output: list[tuple[str, pd.DataFrame]] = []
    if enabled.get("marketnewsfeed_headlines", True) and table_exists(conn, "marketnewsfeed_headlines"):
        output.append(
            (
                "marketnewsfeed",
                conn.execute(
                    """
                    SELECT headline_id AS source_document_id, title, NULL::VARCHAR AS summary,
                           source_url, published_at, collected_at,
                           json_object('symbols', COALESCE(tickers_json, '[]'), 'themes', '[]') AS source_tags_json
                    FROM marketnewsfeed_headlines
                    WHERE title IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    if enabled.get("market_news_feed", True) and table_exists(conn, "market_news_feed"):
        output.append(
            (
                "market_news_feed",
                conn.execute(
                    """
                    SELECT article_id AS source_document_id, title, summary, link AS source_url,
                           pub_date AS published_at, collected_at,
                           json_object('symbols', COALESCE(tickers_json, '[]'), 'themes', COALESCE(themes_json, '[]')) AS source_tags_json
                    FROM market_news_feed
                    WHERE title IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    if enabled.get("naver_news", True) and table_exists(conn, "news_articles"):
        output.append(
            (
                "naver_news",
                conn.execute(
                    """
                    SELECT article_id AS source_document_id, title, description AS summary,
                           COALESCE(originallink, link) AS source_url, pub_date AS published_at, collected_at,
                           json_object('symbols', CASE WHEN symbol IS NULL THEN '[]' ELSE json_array(symbol) END, 'themes', '[]') AS source_tags_json
                    FROM news_articles
                    WHERE title IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    if enabled.get("gdelt", True) and table_exists(conn, "global_news_events"):
        output.append(
            (
                "gdelt",
                conn.execute(
                    """
                    SELECT event_id AS source_document_id, title, query AS summary, url AS source_url,
                           seendate AS published_at, collected_at,
                           json_object('symbols', '[]', 'themes', '[]') AS source_tags_json
                    FROM global_news_events
                    WHERE title IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    if enabled.get("bigkinds", True) and table_exists(conn, "bigkinds_issue_daily"):
        output.append(
            (
                "bigkinds",
                conn.execute(
                    """
                    SELECT concat_ws('|', CAST(issue_date AS VARCHAR), period, CAST(rank AS VARCHAR)) AS source_document_id,
                           title, CONCAT('articles=', COALESCE(CAST(article_count AS VARCHAR), '0')) AS summary,
                           NULL::VARCHAR AS source_url, CAST(issue_date AS TIMESTAMP) AS published_at, collected_at,
                           json_object('symbols', '[]', 'themes', '[]') AS source_tags_json
                    FROM bigkinds_issue_daily
                    WHERE title IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    if enabled.get("dart", True) and table_exists(conn, "dart_disclosures"):
        output.append(
            (
                "opendart",
                conn.execute(
                    """
                    SELECT disclosure_id AS source_document_id, report_name AS title,
                           concat_ws(' ', corp_name, disclosure_type) AS summary,
                           disclosure_url AS source_url, CAST(receipt_date AS TIMESTAMP) AS published_at, collected_at,
                           json_object('symbols', CASE WHEN symbol IS NULL THEN '[]' ELSE json_array(symbol) END, 'themes', '[]') AS source_tags_json
                    FROM dart_disclosures
                    WHERE report_name IS NOT NULL
                    """
                ).fetchdf(),
            )
        )
    return output


def _load_uncached_documents(conn, settings: LocalLlmNewsSettings, model_digest: str, limit: int) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT d.*
        FROM local_llm_news_documents AS d
        WHERE d.status = 'ready'
          AND NOT EXISTS (
            SELECT 1
            FROM local_llm_news_assessments AS a
            WHERE a.document_id = d.document_id
              AND a.model_digest = ?
              AND a.prompt_version = ?
              AND a.status = 'ready'
          )
        ORDER BY
          CASE WHEN lower(COALESCE(d.title, '')) LIKE '%crash%' OR lower(COALESCE(d.title, '')) LIKE '%급락%' THEN 0 ELSE 1 END,
          d.available_at DESC NULLS LAST,
          d.created_at DESC
        LIMIT ?
        """,
        [model_digest, settings.prompt_version, int(limit)],
    ).fetchdf()


def _assessment_messages(documents: Sequence[Mapping[str, Any]], settings: LocalLlmNewsSettings) -> list[dict[str, str]]:
    items = []
    for document in documents:
        items.append(
            {
                "id": str(document.get("document_id")),
                "source": str(document.get("source")),
                "available_at": _iso(document.get("available_at")),
                "title": _clip_text(document.get("title"), settings.document_max_chars),
                "summary": _clip_text(document.get("summary"), settings.document_max_chars),
                "source_tags": _loads(document.get("source_tags_json"), {}),
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "Classify only supplied public news metadata. Return Korean summaries and JSON only. "
                "Each summary_ko must be a Korean noun phrase of 12 characters or fewer. Use at most one theme. "
                "Do not give investment advice, do not invent stock codes, and do not reveal reasoning."
            ),
        },
        {
            "role": "user",
            "content": _json(
                {
                    "allowed_event_types": sorted(ALLOWED_EVENT_TYPES),
                    "allowed_directions": sorted(ALLOWED_DIRECTIONS),
                    "allowed_themes": sorted(ALLOWED_THEMES),
                    "documents": items,
                }
            ),
        },
    ]


def _assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "summary_ko", "event_type", "direction", "importance", "confidence", "themes"],
                    "properties": {
                        "id": {"type": "string"},
                        "summary_ko": {"type": "string", "maxLength": 36},
                        "event_type": {"type": "string", "enum": sorted(ALLOWED_EVENT_TYPES)},
                        "direction": {"type": "string", "enum": sorted(ALLOWED_DIRECTIONS)},
                        "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "themes": {"type": "array", "items": {"type": "string"}, "maxItems": 1},
                    },
                },
                "maxItems": 6,
            }
        },
    }


def _assessment_records(
    documents: Sequence[Mapping[str, Any]],
    payload: Mapping[str, Any],
    *,
    run_id: str,
    settings: LocalLlmNewsSettings,
    model_digest: str,
    known_symbols: set[str],
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    thinking_used: bool,
) -> list[dict[str, Any]]:
    by_id = {str(item.get("id")): item for item in payload.get("items", []) if isinstance(item, Mapping)}
    now = _utcnow()
    output: list[dict[str, Any]] = []
    count = max(1, len(documents))
    for document in documents:
        doc_id = str(document["document_id"])
        item = by_id.get(doc_id, {})
        text = f"{document.get('title') or ''} {document.get('summary') or ''}"
        themes = _validated_themes(item.get("themes"), text)
        source_symbols = _source_symbols(document.get("source_tags_json"))
        mapped_symbols = _deterministic_symbols(text, source_symbols, known_symbols)
        output.append(
            {
                "assessment_id": _digest_text(f"{doc_id}|{model_digest}|{settings.prompt_version}"),
                "run_id": run_id,
                "document_id": doc_id,
                "source": str(document.get("source") or "unknown"),
                "model_name": settings.model_name,
                "model_digest": model_digest,
                "prompt_version": settings.prompt_version,
                "document_digest": str(document.get("content_digest") or ""),
                "summary_ko": _clip_text(item.get("summary_ko"), 320) or None,
                "event_type": _allowed(item.get("event_type"), ALLOWED_EVENT_TYPES, "other"),
                "direction": _allowed(item.get("direction"), ALLOWED_DIRECTIONS, "uncertain"),
                "importance": int(max(1, min(5, _number(item.get("importance"), 1)))),
                "confidence": _clip(_number(item.get("confidence"), 0.0)),
                "themes_json": _json(themes),
                "mapped_symbols_json": _json(mapped_symbols),
                "input_tokens": int(math.ceil(input_tokens / count)),
                "output_tokens": int(math.ceil(output_tokens / count)),
                "latency_ms": int(math.ceil(latency_ms / count)),
                "thinking_used": bool(thinking_used),
                "status": "ready" if item else "partial_ready",
                "error_message": None if item else "structured item missing",
                "assessed_at": now,
            }
        )
    return output


def _thinking_chunks(
    documents: Sequence[Mapping[str, Any]],
    settings: LocalLlmNewsSettings,
    *,
    remaining_probes: int,
) -> list[tuple[bool, list[Mapping[str, Any]]]]:
    batch_size = _structured_batch_size(settings)
    high = [item for item in documents if _is_thinking_candidate(item)]
    regular = [item for item in documents if item not in high]
    chunks: list[tuple[bool, list[Mapping[str, Any]]]] = []
    if settings.thinking_mode == "auto" and remaining_probes > 0:
        for index in range(0, len(high), batch_size):
            if remaining_probes <= 0:
                regular.extend(high[index:])
                break
            chunks.append((True, high[index : index + batch_size]))
            remaining_probes -= 1
    else:
        regular.extend(high)
    for index in range(0, len(regular), batch_size):
        chunks.append((False, regular[index : index + batch_size]))
    return [(thinking, chunk) for thinking, chunk in chunks if chunk]


def _structured_batch_size(settings: LocalLlmNewsSettings) -> int:
    """Fit complete JSON objects inside the bounded output budget before retrying.

    Six is the configured maximum queue batch. A compact assessment still needs
    roughly 80 output tokens, so a 180-token response budget safely carries two
    documents. This avoids repeated malformed-output splits that waste local GPU
    time and tokens while preserving the hard cap on every individual request.
    """
    output_capacity = max(1, int(settings.max_output_tokens) // 80)
    return max(1, min(int(settings.batch_size), output_capacity))


def _chat_assessment_batch(
    client: LocalOllamaClient,
    chunk: list[Mapping[str, Any]],
    settings: LocalLlmNewsSettings,
    *,
    thinking: bool,
) -> list[tuple[bool, list[Mapping[str, Any]], Mapping[str, Any], Mapping[str, int], int]]:
    """Retry malformed bounded output by splitting only that batch."""
    started = time.monotonic()
    try:
        payload, usage = client.chat(
            model_name=settings.model_name,
            messages=_assessment_messages(chunk, settings),
            schema=_assessment_schema(),
            num_ctx=settings.num_ctx,
            max_output_tokens=settings.max_output_tokens,
            thinking=thinking,
        )
    except OllamaUnavailableError:
        if len(chunk) <= 1:
            raise
        midpoint = max(1, len(chunk) // 2)
        return [
            *_chat_assessment_batch(client, chunk[:midpoint], settings, thinking=False),
            *_chat_assessment_batch(client, chunk[midpoint:], settings, thinking=False),
        ]
    elapsed = int((time.monotonic() - started) * 1000)
    return [(thinking, chunk, payload, usage, elapsed)]


def _is_thinking_candidate(document: Mapping[str, Any]) -> bool:
    text = f"{document.get('title') or ''} {document.get('summary') or ''}".lower()
    return any(word.lower() in text for word in RISK_THINKING_WORDS)


def _build_signal_rows(frame: pd.DataFrame, feature_version: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal_date, group in frame.groupby("signal_date", sort=True):
        rows.append(_aggregate_signal(group, signal_date, "market", "ALL", feature_version))
        for symbol, stock_group in _stock_groups(group).items():
            rows.append(_aggregate_signal(stock_group, signal_date, "stock", symbol, feature_version))
    return rows


def _aggregate_signal(group: pd.DataFrame, signal_date: date, scope: str, symbol: str, feature_version: str) -> dict[str, Any]:
    importance = pd.to_numeric(group["importance"], errors="coerce").fillna(1.0).clip(1.0, 5.0) / 5.0
    confidence = pd.to_numeric(group["confidence"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    weights = (importance * confidence).clip(lower=0.05)
    directions = group["direction"].map({"risk_on": 1.0, "risk_off": 0.0, "neutral": 0.5, "uncertain": 0.5}).fillna(0.5)
    risks = group["direction"].map({"risk_off": 1.0, "uncertain": 0.5, "neutral": 0.2, "risk_on": 0.0}).fillna(0.5)
    themes = group["themes_json"].map(lambda value: _loads(value, []))
    denominator = max(float(weights.sum()), 1e-9)

    def theme_score(theme: str) -> float:
        return float(sum(weight for weight, values in zip(weights, themes, strict=False) if theme in values) / denominator)

    source_coverage = Counter(group["source"].fillna("unknown").astype(str).tolist())
    return {
        "signal_date": signal_date,
        "scope": scope,
        "symbol": symbol,
        "llm_news_attention": _clip(float(weights.sum()) / 3.0),
        "llm_news_risk": _clip(float((risks * weights).sum() / denominator)),
        "llm_news_direction": _clip(float((directions * weights).sum() / denominator)),
        "llm_news_semiconductor": _clip(theme_score("SEMICONDUCTOR")),
        "llm_news_macro": _clip(max(theme_score("MACRO"), theme_score("RATE"), theme_score("FX"))),
        "llm_news_confidence": _clip(float((confidence * weights).sum() / denominator)),
        "llm_news_coverage": _clip(float(len(group)) / 6.0),
        "source_coverage_json": _json(dict(sorted(source_coverage.items()))),
        "assessment_count": int(len(group)),
        "evidence_json": _json([str(value) for value in group["document_id"].tolist()[:12]]),
        "feature_version": feature_version,
        "created_at": _utcnow(),
    }


def _stock_groups(group: pd.DataFrame) -> dict[str, pd.DataFrame]:
    indexes: dict[str, list[int]] = {}
    for index, raw_symbols in group["mapped_symbols_json"].items():
        for symbol in _loads(raw_symbols, []):
            normalized = str(symbol).zfill(6)
            if normalized.isdigit() and len(normalized) == 6:
                indexes.setdefault(normalized, []).append(index)
    return {symbol: group.loc[rows] for symbol, rows in indexes.items()}


def _latest_structured_signal_payload(conn) -> dict[str, Any]:
    if not table_exists(conn, "local_llm_news_signal_daily"):
        return {"items": []}
    frame = conn.execute(
        """
        SELECT signal_date, llm_news_attention, llm_news_risk, llm_news_direction,
               llm_news_semiconductor, llm_news_macro, llm_news_confidence,
               llm_news_coverage, assessment_count, source_coverage_json
        FROM local_llm_news_signal_daily
        WHERE scope = 'market' AND symbol = 'ALL'
        ORDER BY signal_date DESC
        LIMIT 3
        """
    ).fetchdf()
    return {"items": [_record(row) for row in frame.to_dict(orient="records")]}


def _record_run(
    conn,
    run_id: str,
    settings: LocalLlmNewsSettings,
    stage: str,
    status: str,
    started: datetime,
    *,
    model_digest: str | None = None,
    requested_count: int = 0,
    assessed_count: int = 0,
    cached_count: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    latency_ms: int = 0,
    thinking_probe_count: int = 0,
    error_message: str | None = None,
) -> dict[str, Any]:
    record = {
        "run_id": run_id,
        "stage": stage,
        "model_name": settings.model_name,
        "model_digest": model_digest,
        "prompt_version": settings.prompt_version,
        "requested_document_count": int(requested_count),
        "assessed_document_count": int(assessed_count),
        "cached_document_count": int(cached_count),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "latency_ms": int(latency_ms),
        "thinking_probe_count": int(thinking_probe_count),
        "status": status,
        "error_message": error_message,
        "started_at": started,
        "completed_at": _utcnow(),
    }
    append_dedup_table(conn, "local_llm_news_runs", pd.DataFrame([record]), ["run_id"])
    return {key: _record({key: value})[key] for key, value in record.items()}


def _record_brief(
    conn,
    settings: LocalLlmNewsSettings,
    stage: str,
    brief_date: date,
    status: str,
    *,
    model_digest: str | None = None,
    summary_ko: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    error_message: str | None = None,
) -> dict[str, Any]:
    record = {
        "brief_date": brief_date,
        "stage": stage,
        "model_name": settings.model_name,
        "model_digest": model_digest,
        "prompt_version": settings.prompt_version,
        "summary_ko": summary_ko,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "status": status,
        "error_message": error_message,
        "created_at": _utcnow(),
    }
    append_dedup_table(
        conn,
        "local_llm_news_briefs",
        pd.DataFrame([record]),
        ["brief_date", "stage", "model_name", "prompt_version"],
    )
    return {key: _record({key: value})[key] for key, value in record.items()}


def _cached_count(conn, settings: LocalLlmNewsSettings, model_digest: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM local_llm_news_assessments
            WHERE model_digest = ? AND prompt_version = ? AND status = 'ready'
            """,
            [model_digest, settings.prompt_version],
        ).fetchone()[0]
    )


def _today_thinking_probe_count(conn, settings: LocalLlmNewsSettings) -> int:
    if not table_exists(conn, "local_llm_news_runs"):
        return 0
    today_start = datetime.now(KST).date()
    return int(
        conn.execute(
            """
            SELECT COALESCE(SUM(thinking_probe_count), 0)
            FROM local_llm_news_runs
            WHERE model_name = ? AND prompt_version = ?
              AND CAST(started_at AT TIME ZONE 'Asia/Seoul' AS DATE) = ?
            """,
            [settings.model_name, settings.prompt_version, today_start],
        ).fetchone()[0]
    )


def _known_local_symbols(conn) -> set[str]:
    if not table_exists(conn, "symbols"):
        return set()
    return {
        str(row[0]).zfill(6)
        for row in conn.execute("SELECT symbol FROM symbols WHERE symbol IS NOT NULL").fetchall()
        if str(row[0]).strip().isdigit()
    }


def _deterministic_symbols(text: str, source_symbols: Sequence[str], known_symbols: set[str]) -> list[str]:
    candidates = {str(symbol).zfill(6) for symbol in source_symbols if str(symbol).strip().isdigit()}
    candidates.update(
        str(symbol).zfill(6)
        for symbol in extract_tickers(text, ticker_aliases=DEFAULT_LOCAL_TICKER_ALIASES)
        if str(symbol).strip().isdigit()
    )
    return sorted(symbol for symbol in candidates if symbol in known_symbols)


def _source_symbols(raw: Any) -> list[str]:
    tags = _loads(raw, {})
    symbols = tags.get("symbols", []) if isinstance(tags, Mapping) else []
    if isinstance(symbols, str):
        symbols = _loads(symbols, [])
    return [str(value) for value in symbols] if isinstance(symbols, Sequence) and not isinstance(symbols, str) else []


def _validated_themes(raw: Any, text: str) -> list[str]:
    values = {str(value).upper() for value in raw or [] if str(value).upper() in ALLOWED_THEMES}
    values.update(str(value).upper() for value in classify_themes(text) if str(value).upper() in ALLOWED_THEMES)
    if "AI" in values and not AI_THEME_PATTERN.search(text or ""):
        values.remove("AI")
    return sorted(values)[:5]


def _price_dates(conn) -> list[date]:
    if not table_exists(conn, "prices_daily"):
        return []
    values = conn.execute("SELECT DISTINCT date FROM prices_daily WHERE date IS NOT NULL ORDER BY date").fetchall()
    return [pd.to_datetime(value[0]).date() for value in values]


def _next_available_trade_date(value: Any, *, cutoff: clock_time, trading_dates: Sequence[date]) -> date | None:
    timestamp = _to_utc_naive(value)
    if timestamp is None:
        return None
    kst_value = timestamp.replace(tzinfo=UTC).astimezone(KST)
    candidate = kst_value.date() if kst_value.timetz().replace(tzinfo=None) <= cutoff else kst_value.date() + timedelta(days=1)
    known = next((item for item in trading_dates if item >= candidate), None)
    if known is not None:
        return known
    # The next session can legitimately be outside the locally collected price
    # history during a premarket run. Preserve that date for target-date joins.
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def _cutoff_time(value: str) -> clock_time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", 1))
        return clock_time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return clock_time(hour=8, minute=0)


def _latest_available_at(*values: Any) -> datetime | None:
    parsed = [item for item in (_to_utc_naive(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


def _to_utc_naive(value: Any) -> datetime | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    timestamp = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(timestamp):
        return None
    return timestamp.to_pydatetime().replace(tzinfo=None)


def _validate_stage(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized not in {"close", "premarket"}:
        raise ValueError("stage must be close or premarket")
    return normalized


def _allowed(value: Any, allowed: set[str], default: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else default


def _number(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _clip(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


def _clip_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _clean_url(value: Any) -> str | None:
    raw = str(value or "").strip()
    return raw if raw.startswith(("http://", "https://")) else None


def _digest_text(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _iso(value: Any) -> str | None:
    timestamp = _to_utc_naive(value)
    return timestamp.isoformat() if timestamp else None


def _record(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, pd.Timestamp):
            output[key] = item.isoformat()
        elif hasattr(item, "isoformat"):
            output[key] = item.isoformat()
        elif isinstance(item, float) and math.isnan(item):
            output[key] = None
        else:
            output[key] = item
    return output


def _signal_columns() -> list[str]:
    return [
        "signal_date",
        "scope",
        "symbol",
        "llm_news_attention",
        "llm_news_risk",
        "llm_news_direction",
        "llm_news_semiconductor",
        "llm_news_macro",
        "llm_news_confidence",
        "llm_news_coverage",
        "source_coverage_json",
        "assessment_count",
        "evidence_json",
        "feature_version",
        "created_at",
    ]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    output = dict(base)
    for key, value in patch.items():
        if isinstance(value, Mapping) and isinstance(output.get(key), Mapping):
            output[key] = _deep_merge(dict(output[key]), value)
        else:
            output[key] = value
    return output

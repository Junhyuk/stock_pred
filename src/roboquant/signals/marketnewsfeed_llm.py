"""Local-only, structured LLM assessments for public MarketNewsFeed headlines.

This module deliberately accepts only rows already captured from the visible
MarketNewsFeed DOM. It never follows a headline URL, calls a site-private API,
or persists model reasoning content.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.db import append_dedup_table, table_exists
from roboquant.signals.news_signals import DEFAULT_LOCAL_TICKER_ALIASES
from roboquant.signals.telegram_text import classify_themes, extract_tickers

KST = ZoneInfo("Asia/Seoul")
LLM_FEATURE_COLUMNS = [
    "llm_news_attention_score",
    "llm_news_risk_score",
    "llm_news_direction_score",
    "llm_news_semiconductor_score",
    "llm_news_macro_score",
    "llm_news_confidence_score",
    "llm_news_coverage_score",
]
LLM_FEATURE_DEFAULTS = {column: 0.0 for column in LLM_FEATURE_COLUMNS}
ALLOWED_DIRECTIONS = {"risk_on", "risk_off", "neutral", "uncertain"}
ALLOWED_EVENT_TYPES = {
    "macro",
    "central_bank",
    "geopolitics",
    "earnings",
    "sector",
    "company",
    "policy",
    "market",
    "other",
}
ALLOWED_THEMES = {
    "AI",
    "SEMICONDUCTOR",
    "RATE",
    "ENERGY",
    "BIO",
    "DEFENSE",
    "CRYPTO",
    "MACRO",
    "FX",
    "AUTO",
    "KOREA",
}
RISK_THINKING_WORDS = (
    "crash",
    "sell-off",
    "selloff",
    "war",
    "tariff",
    "sanction",
    "default",
    "bankrupt",
    "emergency",
    "급락",
    "전쟁",
    "관세",
    "제재",
    "파산",
    "긴급",
)


class OllamaUnavailableError(RuntimeError):
    """Raised when the explicitly local Ollama service cannot be used."""


@dataclass(frozen=True)
class MarketNewsFeedLlmSettings:
    enabled: bool = True
    source: str = "marketnewsfeed_site_shadow"
    ollama_host: str = "http://127.0.0.1:11434"
    model_name: str = "roboquant-news:qwen3.5-27b-q4"
    gguf_path: str = "llm_models/gguf/Qwen3.5-27B-Q4_K_M.gguf"
    prompt_version: str = "marketnewsfeed-v1"
    request_timeout_seconds: float = 90.0
    max_headlines_per_run: int = 30
    batch_size: int = 6
    max_headline_chars: int = 280
    num_ctx: int = 4096
    max_output_tokens: int = 360
    thinking_mode: str = "auto"
    thinking_importance_threshold: int = 4
    market_open_cutoff_kst: str = "08:00"
    min_signal_days_for_shadow: int = 60


def settings_from_config(config: Mapping[str, Any] | None) -> MarketNewsFeedLlmSettings:
    raw = dict((config or {}).get("marketnewsfeed_llm") or {})
    defaults = MarketNewsFeedLlmSettings()
    values = {name: raw.get(name, getattr(defaults, name)) for name in defaults.__dataclass_fields__}
    values["max_headlines_per_run"] = max(1, min(int(values["max_headlines_per_run"]), 30))
    values["batch_size"] = max(1, min(int(values["batch_size"]), 6))
    values["max_headline_chars"] = max(1, min(int(values["max_headline_chars"]), 280))
    values["num_ctx"] = max(512, min(int(values["num_ctx"]), 4096))
    values["max_output_tokens"] = max(32, min(int(values["max_output_tokens"]), 360))
    values["thinking_importance_threshold"] = max(1, min(int(values["thinking_importance_threshold"]), 5))
    values["min_signal_days_for_shadow"] = max(1, int(values["min_signal_days_for_shadow"]))
    values["request_timeout_seconds"] = max(1.0, float(values["request_timeout_seconds"]))
    return MarketNewsFeedLlmSettings(**values)


class LocalOllamaClient:
    """Small stdlib client that refuses non-loopback Ollama endpoints."""

    def __init__(self, host: str, timeout_seconds: float = 90.0) -> None:
        parsed = urlparse(str(host).strip())
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port not in {None, 11434}:
            raise ValueError("Ollama host must be the local http://127.0.0.1:11434 endpoint.")
        self.host = f"http://127.0.0.1:{parsed.port or 11434}"
        self.timeout_seconds = float(timeout_seconds)

    def probe(self, model_name: str) -> dict[str, Any]:
        version = self._request("/api/version", payload=None)
        try:
            show = self._request("/api/show", payload={"name": model_name})
        except OllamaUnavailableError as exc:
            if "404" in str(exc):
                raise OllamaUnavailableError(f"model_import_failed: local model {model_name!r} is not registered") from exc
            raise
        return {
            "server_version": str(version.get("version") or "unknown"),
            "model_digest": str(show.get("digest") or _digest_text(model_name)),
            "details": show.get("details") if isinstance(show.get("details"), dict) else {},
        }

    def chat(
        self,
        *,
        model_name: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        num_ctx: int,
        max_output_tokens: int,
        thinking: bool,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        payload: dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": int(num_ctx), "num_predict": int(max_output_tokens)},
        }
        # Qwen-family GGUF imports can default to hidden thinking when this is
        # omitted. Explicit false keeps ordinary headline batches bounded.
        payload["think"] = bool(thinking)
        response: dict[str, Any] | None = None
        retry_usage = {"input_tokens": 0, "output_tokens": 0}
        try:
            response = self._request("/api/chat", payload=payload)
            parsed = _parse_structured_content(response.get("message", {}).get("content"))
        except OllamaUnavailableError:
            # Some local builds reject thinking, and some think-capable GGUF
            # models leave the final channel empty after hidden reasoning.
            if not thinking:
                raise
            if response is not None:
                retry_usage = {
                    "input_tokens": _safe_int(response.get("prompt_eval_count")) or _estimate_tokens(messages),
                    "output_tokens": _safe_int(response.get("eval_count")) or 0,
                }
            payload["think"] = False
            response = self._request("/api/chat", payload=payload)
            parsed = _parse_structured_content(response.get("message", {}).get("content"))
        if not isinstance(parsed, dict):
            raise OllamaUnavailableError("Ollama structured output must be a JSON object.")
        usage = {
            "input_tokens": retry_usage["input_tokens"]
            + (_safe_int(response.get("prompt_eval_count")) or _estimate_tokens(messages)),
            "output_tokens": retry_usage["output_tokens"]
            + (_safe_int(response.get("eval_count")) or _estimate_tokens(parsed)),
        }
        return parsed, usage

    def _request(self, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        url = f"{self.host}{path}"
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=body, method="GET" if payload is None else "POST")
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - fixed loopback host
                raw = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            raise OllamaUnavailableError(f"Local Ollama is unavailable: {exc}") from exc
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OllamaUnavailableError("Local Ollama returned an invalid JSON response.") from exc
        if not isinstance(parsed, dict):
            raise OllamaUnavailableError("Local Ollama returned an invalid response shape.")
        if parsed.get("error"):
            raise OllamaUnavailableError(str(parsed["error"]))
        return parsed


def summarize_marketnewsfeed_headlines(
    conn,
    config: Mapping[str, Any] | None,
    *,
    limit: int | None = None,
    client: LocalOllamaClient | None = None,
) -> dict[str, Any]:
    """Assess newly rendered public headlines and persist final JSON only."""
    settings = settings_from_config(config)
    now = _utcnow()
    run_id = uuid4().hex
    if not settings.enabled:
        return _record_run(
            conn,
            run_id=run_id,
            settings=settings,
            status="disabled",
            started_at=now,
            error_message="marketnewsfeed_llm.enabled=false",
        )
    if not table_exists(conn, "marketnewsfeed_headlines"):
        return _record_run(
            conn,
            run_id=run_id,
            settings=settings,
            status="not_ready",
            started_at=now,
            error_message="marketnewsfeed_headlines table is unavailable",
        )

    local_client = client or LocalOllamaClient(settings.ollama_host, settings.request_timeout_seconds)
    try:
        capability = local_client.probe(settings.model_name)
    except (OllamaUnavailableError, ValueError) as exc:
        status = "model_import_failed" if "model_import_failed" in str(exc) else "unavailable"
        return _record_run(
            conn,
            run_id=run_id,
            settings=settings,
            status=status,
            started_at=now,
            error_message=str(exc),
        )

    model_digest = str(capability["model_digest"])
    requested_limit = max(1, min(int(limit or settings.max_headlines_per_run), settings.max_headlines_per_run))
    headlines = _load_uncached_headlines(conn, settings, model_digest, requested_limit)
    cached_count = _cached_count(conn, settings, model_digest)
    if headlines.empty:
        return _record_run(
            conn,
            run_id=run_id,
            settings=settings,
            model_digest=model_digest,
            status="ready",
            started_at=now,
            requested_count=0,
            cached_count=cached_count,
        )

    known_symbols = _known_local_symbols(conn)
    records: list[dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0
    total_latency_ms = 0
    thinking_probe_count = 0
    failures: list[str] = []
    for uses_thinking, chunk in _thinking_chunks(headlines.to_dict(orient="records"), settings):
        started = time.monotonic()
        try:
            payload, usage = local_client.chat(
                model_name=settings.model_name,
                messages=_assessment_messages(chunk, settings),
                schema=_assessment_schema(),
                num_ctx=settings.num_ctx,
                max_output_tokens=settings.max_output_tokens,
                thinking=uses_thinking,
            )
            latency_ms = int((time.monotonic() - started) * 1000)
            total_input_tokens += int(usage["input_tokens"])
            total_output_tokens += int(usage["output_tokens"])
            total_latency_ms += latency_ms
            thinking_probe_count += int(uses_thinking)
            records.extend(
                _assessment_records(
                    payload,
                    headlines=chunk,
                    settings=settings,
                    run_id=run_id,
                    model_digest=model_digest,
                    known_symbols=known_symbols,
                    input_tokens=int(usage["input_tokens"]),
                    output_tokens=int(usage["output_tokens"]),
                    latency_ms=latency_ms,
                    thinking_used=uses_thinking,
                )
            )
        except OllamaUnavailableError as exc:
            failures.append(str(exc))

    if records:
        append_dedup_table(
            conn,
            "marketnewsfeed_llm_assessments",
            pd.DataFrame(records),
            ["headline_id", "model_digest", "prompt_version"],
        )
    status = "ready" if records and not failures else "partial_ready" if records else "failed"
    return _record_run(
        conn,
        run_id=run_id,
        settings=settings,
        model_digest=model_digest,
        status=status,
        started_at=now,
        requested_count=len(headlines),
        assessed_count=len(records),
        cached_count=cached_count,
        input_tokens=total_input_tokens,
        output_tokens=total_output_tokens,
        latency_ms=total_latency_ms,
        thinking_probe_count=thinking_probe_count,
        error_message="; ".join(failures[:3]) or None,
    )


def build_marketnewsfeed_llm_signal_daily(
    conn,
    config: Mapping[str, Any] | None,
    *,
    feature_version: str | None = None,
) -> pd.DataFrame:
    """Build cutoff-safe market and deterministic stock LLM features."""
    settings = settings_from_config(config)
    version = feature_version or f"{settings.prompt_version}-signals-v1"
    if not table_exists(conn, "marketnewsfeed_llm_assessments"):
        return pd.DataFrame(columns=_signal_columns())
    frame = conn.execute(
        """
        SELECT
          a.headline_id,
          a.direction,
          a.importance,
          a.confidence,
          a.themes_json,
          a.mapped_symbols_json,
          h.published_at,
          h.collected_at
        FROM marketnewsfeed_llm_assessments AS a
        INNER JOIN marketnewsfeed_headlines AS h ON h.headline_id = a.headline_id
        WHERE a.status = 'ready'
        ORDER BY h.published_at, h.collected_at, a.assessed_at
        """
    ).fetchdf()
    if frame.empty:
        return pd.DataFrame(columns=_signal_columns())
    trading_dates = _price_dates(conn)
    if not trading_dates:
        return pd.DataFrame(columns=_signal_columns())
    cutoff = _cutoff_time(settings.market_open_cutoff_kst)
    frame["signal_date"] = frame.apply(
        lambda row: _next_available_trade_date(
            _latest_available_timestamp(row.get("published_at"), row.get("collected_at")),
            cutoff=cutoff,
            trading_dates=trading_dates,
        ),
        axis=1,
    )
    frame = frame[frame["signal_date"].notna()].copy()
    if frame.empty:
        return pd.DataFrame(columns=_signal_columns())
    rows = _build_signal_rows(frame, version)
    signals = pd.DataFrame(rows, columns=_signal_columns())
    if not signals.empty:
        append_dedup_table(
            conn,
            "marketnewsfeed_llm_signal_daily",
            signals,
            ["signal_date", "scope", "symbol", "feature_version"],
        )
    return signals


def attach_marketnewsfeed_llm_features(
    features: pd.DataFrame,
    llm_signals: pd.DataFrame | None,
) -> pd.DataFrame:
    """Left-join safe LLM values without changing the production feature list."""
    if features.empty:
        return features
    output = features.copy()
    output["date"] = pd.to_datetime(output["date"], errors="coerce").dt.date
    output["symbol"] = output["symbol"].astype(str).str.zfill(6)
    for column, default in LLM_FEATURE_DEFAULTS.items():
        if column not in output.columns:
            output[column] = default
        else:
            output[column] = pd.to_numeric(output[column], errors="coerce").fillna(default)
    if llm_signals is None or llm_signals.empty:
        return output

    signals = llm_signals.copy()
    signals["date"] = pd.to_datetime(signals["signal_date"], errors="coerce").dt.date
    signals["symbol"] = signals["symbol"].fillna("").astype(str)
    value_map = {
        "llm_news_attention": "llm_news_attention_score",
        "llm_news_risk": "llm_news_risk_score",
        "llm_news_direction": "llm_news_direction_score",
        "llm_news_semiconductor": "llm_news_semiconductor_score",
        "llm_news_macro": "llm_news_macro_score",
        "llm_news_confidence": "llm_news_confidence_score",
        "llm_news_coverage": "llm_news_coverage_score",
    }
    market = signals[(signals["scope"].eq("market")) | (signals["symbol"].eq("ALL"))].copy()
    stock = signals[signals["scope"].eq("stock")].copy()
    if not market.empty:
        market = market.drop_duplicates("date", keep="last")
        for source, target in value_map.items():
            mapping = market.set_index("date")[source] if source in market.columns else pd.Series(dtype=float)
            output[target] = output["date"].map(mapping).fillna(output[target])
    if not stock.empty:
        stock["symbol"] = stock["symbol"].astype(str).str.zfill(6)
        stock = stock.drop_duplicates(["date", "symbol"], keep="last")
        indexed = stock.set_index(["date", "symbol"])
        keys = pd.MultiIndex.from_frame(output[["date", "symbol"]])
        for source, target in value_map.items():
            if source not in indexed.columns:
                continue
            values = pd.Series(indexed[source].reindex(keys).to_numpy(), index=output.index)
            output[target] = values.fillna(output[target])
    return output


def _load_uncached_headlines(
    conn,
    settings: MarketNewsFeedLlmSettings,
    model_digest: str,
    limit: int,
) -> pd.DataFrame:
    return conn.execute(
        """
        SELECT h.*
        FROM marketnewsfeed_headlines AS h
        WHERE h.source = ?
          AND NOT EXISTS (
            SELECT 1
            FROM marketnewsfeed_llm_assessments AS a
            WHERE a.headline_id = h.headline_id
              AND a.model_digest = ?
              AND a.prompt_version = ?
              AND a.status = 'ready'
          )
        ORDER BY h.published_at DESC NULLS LAST, h.collected_at DESC NULLS LAST
        LIMIT ?
        """,
        [settings.source, model_digest, settings.prompt_version, int(limit)],
    ).fetchdf()


def _cached_count(conn, settings: MarketNewsFeedLlmSettings, model_digest: str) -> int:
    return int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM marketnewsfeed_llm_assessments
            WHERE model_digest = ? AND prompt_version = ? AND status = 'ready'
            """,
            [model_digest, settings.prompt_version],
        ).fetchone()[0]
    )


def _assessment_messages(headlines: list[dict[str, Any]], settings: MarketNewsFeedLlmSettings) -> list[dict[str, str]]:
    items = []
    for headline in headlines:
        items.append(
            {
                "id": str(headline["headline_id"]),
                "headline": _truncate(str(headline.get("title") or ""), settings.max_headline_chars),
                "category": str(headline.get("site_category") or ""),
                "importance": _safe_int(headline.get("importance")) or 1,
                "asset_tags": _loads(headline.get("tickers_json"), []),
                "published_at": _iso_without_tz(headline.get("published_at")),
            }
        )
    system = (
        "You classify short public market headlines for a Korean research dashboard. "
        "Return only JSON matching the schema. Do not invent facts, prices, symbols, or advice. "
        "Use a concise Korean summary of at most 120 Korean characters. "
        "direction must be risk_on, risk_off, neutral, or uncertain. "
        "Themes may only be AI, SEMICONDUCTOR, RATE, ENERGY, BIO, DEFENSE, CRYPTO, MACRO, FX, AUTO, KOREA."
    )
    user = json.dumps({"headlines": items}, ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _assessment_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "summary_ko": {"type": "string"},
                        "event_type": {"type": "string"},
                        "direction": {"type": "string"},
                        "importance": {"type": "integer"},
                        "confidence": {"type": "number"},
                        "themes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "summary_ko", "event_type", "direction", "importance", "confidence", "themes"],
                },
            }
        },
        "required": ["items"],
    }


def _assessment_records(
    payload: Mapping[str, Any],
    *,
    headlines: list[dict[str, Any]],
    settings: MarketNewsFeedLlmSettings,
    run_id: str,
    model_digest: str,
    known_symbols: set[str],
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
    thinking_used: bool,
) -> list[dict[str, Any]]:
    by_id = {str(item["headline_id"]): item for item in headlines}
    raw_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    records: list[dict[str, Any]] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping):
            continue
        headline_id = str(raw.get("id") or "")
        headline = by_id.get(headline_id)
        if headline is None:
            continue
        normalized = _normalize_assessment(raw, headline, known_symbols)
        if normalized is None:
            continue
        records.append(
            {
                "assessment_id": _digest_text(f"{headline_id}|{model_digest}|{settings.prompt_version}"),
                "run_id": run_id,
                "headline_id": headline_id,
                "source": settings.source,
                "model_name": settings.model_name,
                "model_digest": model_digest,
                "prompt_version": settings.prompt_version,
                "headline_digest": _digest_text(_truncate(str(headline.get("title") or ""), settings.max_headline_chars)),
                "summary_ko": normalized["summary_ko"],
                "event_type": normalized["event_type"],
                "direction": normalized["direction"],
                "importance": normalized["importance"],
                "confidence": normalized["confidence"],
                "themes_json": _json(normalized["themes"]),
                "mapped_symbols_json": _json(normalized["mapped_symbols"]),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "latency_ms": latency_ms,
                "thinking_used": bool(thinking_used),
                "status": "ready",
                "error_message": None,
                "assessed_at": _utcnow(),
            }
        )
    return records


def _normalize_assessment(raw: Mapping[str, Any], headline: Mapping[str, Any], known_symbols: set[str]) -> dict[str, Any] | None:
    direction = str(raw.get("direction") or "uncertain").strip().lower()
    if direction not in ALLOWED_DIRECTIONS:
        direction = "uncertain"
    event_type = str(raw.get("event_type") or "other").strip().lower()
    if event_type not in ALLOWED_EVENT_TYPES:
        event_type = "other"
    summary = " ".join(str(raw.get("summary_ko") or "").split())
    if not summary:
        return None
    themes = {
        str(value).strip().upper()
        for value in (raw.get("themes") or [])
        if str(value).strip().upper() in ALLOWED_THEMES
    }
    title = str(headline.get("title") or "")
    themes.update(theme for theme in classify_themes(title) if theme in ALLOWED_THEMES)
    if event_type == "macro":
        themes.add("MACRO")
    mapped_symbols = _deterministic_local_symbols(headline, known_symbols)
    return {
        "summary_ko": _truncate(summary, 180),
        "event_type": event_type,
        "direction": direction,
        "importance": max(1, min(_safe_int(raw.get("importance")) or _safe_int(headline.get("importance")) or 1, 5)),
        "confidence": _clip(_safe_float(raw.get("confidence")) or 0.0),
        "themes": sorted(themes)[:5],
        "mapped_symbols": mapped_symbols,
    }


def _deterministic_local_symbols(headline: Mapping[str, Any], known_symbols: set[str]) -> list[str]:
    source_tags = _loads(headline.get("tickers_json"), [])
    title = str(headline.get("title") or "")
    candidates = {str(tag).strip().zfill(6) for tag in source_tags if str(tag).strip().isdigit()}
    candidates.update(
        str(symbol).zfill(6)
        for symbol in extract_tickers(title, ticker_aliases=DEFAULT_LOCAL_TICKER_ALIASES)
        if str(symbol).isdigit() and len(str(symbol)) <= 6
    )
    return sorted(symbol for symbol in candidates if symbol in known_symbols)


def _thinking_chunks(headlines: list[dict[str, Any]], settings: MarketNewsFeedLlmSettings) -> list[tuple[bool, list[dict[str, Any]]]]:
    if settings.thinking_mode.lower() not in {"auto", "true", "enabled"}:
        return [(False, chunk) for chunk in _chunks(headlines, settings.batch_size)]
    ordinary: list[dict[str, Any]] = []
    probes: list[dict[str, Any]] = []
    for item in headlines:
        text = str(item.get("title") or "").lower()
        importance = _safe_int(item.get("importance")) or 0
        if importance >= settings.thinking_importance_threshold or any(word in text for word in RISK_THINKING_WORDS):
            probes.append(item)
        else:
            ordinary.append(item)
    return [(False, chunk) for chunk in _chunks(ordinary, settings.batch_size)] + [
        (True, chunk) for chunk in _chunks(probes, settings.batch_size)
    ]


def _build_signal_rows(frame: pd.DataFrame, feature_version: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for signal_date, group in frame.groupby("signal_date"):
        rows.append(_aggregate_signal(group, signal_date, "market", "ALL", feature_version))
        for symbol, stock_group in _stock_groups(group).items():
            rows.append(_aggregate_signal(stock_group, signal_date, "stock", symbol, feature_version))
    return rows


def _aggregate_signal(
    group: pd.DataFrame,
    signal_date: date,
    scope: str,
    symbol: str,
    feature_version: str,
) -> dict[str, Any]:
    importance = pd.to_numeric(group["importance"], errors="coerce").fillna(1.0).clip(1.0, 5.0) / 5.0
    confidence = pd.to_numeric(group["confidence"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    weights = (importance * confidence).clip(lower=0.05)
    directions = group["direction"].map({"risk_on": 1.0, "risk_off": 0.0, "neutral": 0.5, "uncertain": 0.5}).fillna(0.5)
    risks = group["direction"].map({"risk_off": 1.0, "uncertain": 0.5, "neutral": 0.2, "risk_on": 0.0}).fillna(0.5)
    themes = group["themes_json"].map(lambda value: _loads(value, []))
    denominator = max(float(weights.sum()), 1e-9)
    def theme_score(theme: str) -> float:
        total = sum(weight for weight, values in zip(weights, themes, strict=False) if theme in values)
        return float(total / denominator)
    evidence = [str(value) for value in group["headline_id"].tolist()[:12]]
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
        "assessment_count": int(len(group)),
        "evidence_json": _json(evidence),
        "feature_version": feature_version,
        "created_at": _utcnow(),
    }


def _stock_groups(group: pd.DataFrame) -> dict[str, pd.DataFrame]:
    output: dict[str, list[int]] = {}
    for index, raw_symbols in group["mapped_symbols_json"].items():
        for symbol in _loads(raw_symbols, []):
            normalized = str(symbol).zfill(6)
            if normalized.isdigit() and len(normalized) == 6:
                output.setdefault(normalized, []).append(index)
    return {symbol: group.loc[indices] for symbol, indices in output.items()}


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
        "assessment_count",
        "evidence_json",
        "feature_version",
        "created_at",
    ]


def _record_run(
    conn,
    *,
    run_id: str,
    settings: MarketNewsFeedLlmSettings,
    status: str,
    started_at: datetime,
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
    row = pd.DataFrame(
        [
            {
                "run_id": run_id,
                "source": settings.source,
                "model_name": settings.model_name,
                "model_digest": model_digest,
                "prompt_version": settings.prompt_version,
                "requested_headline_count": int(requested_count),
                "assessed_headline_count": int(assessed_count),
                "cached_headline_count": int(cached_count),
                "input_tokens": int(input_tokens),
                "output_tokens": int(output_tokens),
                "latency_ms": int(latency_ms),
                "thinking_probe_count": int(thinking_probe_count),
                "status": status,
                "error_message": _truncate(str(error_message or ""), 500) or None,
                "started_at": started_at,
                "completed_at": _utcnow(),
            }
        ]
    )
    append_dedup_table(conn, "marketnewsfeed_llm_runs", row, ["run_id"])
    return {
        "run_id": run_id,
        "status": status,
        "model_name": settings.model_name,
        "model_digest": model_digest,
        "requested_headline_count": int(requested_count),
        "assessed_headline_count": int(assessed_count),
        "cached_headline_count": int(cached_count),
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "thinking_probe_count": int(thinking_probe_count),
        "error_message": _truncate(str(error_message or ""), 500) or None,
    }


def _known_local_symbols(conn) -> set[str]:
    if not table_exists(conn, "symbols"):
        return set()
    return {
        str(value).zfill(6)
        for value, in conn.execute("SELECT DISTINCT symbol FROM symbols").fetchall()
        if str(value).isdigit() and len(str(value)) <= 6
    }


def _price_dates(conn) -> list[date]:
    if not table_exists(conn, "prices_daily"):
        return []
    return [
        pd.Timestamp(value).date()
        for value, in conn.execute("SELECT DISTINCT date FROM prices_daily WHERE date IS NOT NULL ORDER BY date").fetchall()
    ]


def _latest_available_timestamp(published_at: Any, collected_at: Any) -> datetime | None:
    values = [_as_utc_timestamp(value) for value in (published_at, collected_at)]
    values = [value for value in values if value is not None]
    return max(values) if values else None


def _next_available_trade_date(
    available_at: datetime | None,
    *,
    cutoff: clock_time,
    trading_dates: Sequence[date],
) -> date | None:
    if available_at is None:
        return None
    kst = available_at.astimezone(KST)
    candidate = kst.date() if kst.timetz().replace(tzinfo=None) <= cutoff else kst.date() + timedelta(days=1)
    return next((trade_date for trade_date in trading_dates if trade_date >= candidate), None)


def _cutoff_time(value: str) -> clock_time:
    try:
        hour, minute = (int(part) for part in str(value).split(":", maxsplit=1))
        return clock_time(hour, minute)
    except (TypeError, ValueError):
        return clock_time(8, 0)


def _as_utc_timestamp(value: Any) -> datetime | None:
    parsed = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().astimezone(UTC)


def _chunks(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    return [items[index : index + size] for index in range(0, len(items), size)]


def _loads(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _parse_structured_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", maxsplit=1)[-1]
        raw = raw.rsplit("```", maxsplit=1)[0].strip()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OllamaUnavailableError("Ollama returned non-JSON structured output.") from exc
    if not isinstance(parsed, dict):
        raise OllamaUnavailableError("Ollama structured output must be a JSON object.")
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _digest_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _truncate(value: str, maximum: int) -> str:
    text = " ".join(str(value).split())
    return text[:maximum]


def _iso_without_tz(value: Any) -> str | None:
    parsed = _as_utc_timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _clip(value: float) -> float:
    return float(max(0.0, min(1.0, value)))


def _estimate_tokens(value: Any) -> int:
    return max(1, int(math.ceil(len(json.dumps(value, ensure_ascii=False, default=str)) / 4.0)))


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

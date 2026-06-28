from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import feedparser
import pandas as pd

from roboquant.signals.telegram_text import (
    classify_themes,
    extract_tickers,
    normalize_sentiment,
    simple_sentiment,
    text_excerpt,
)

VALID_CATEGORIES = {"macro", "flow", "sector"}


@dataclass(frozen=True)
class MarketNewsFeedSource:
    source: str
    name: str
    feed_url: str
    default_category: str = "macro"
    enabled: bool = True


class MarketNewsFeedProvider:
    provider_name = "market_news_rss"

    def __init__(
        self,
        *,
        opener: Callable[[Request], bytes | str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self._opener = opener
        self._timeout = timeout

    def fetch_entries(
        self,
        sources: Sequence[MarketNewsFeedSource],
        *,
        config: Mapping[str, Any] | None = None,
        max_entries_per_feed: int = 50,
    ) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        collected_at = _utcnow()
        for source in sources:
            if not source.enabled:
                continue
            feed_url = str(source.feed_url or "").strip()
            if not feed_url:
                continue
            for entry in self._fetch_feed_entries(feed_url, max_entries=max_entries_per_feed):
                normalized = normalize_rss_entry(
                    source=source,
                    entry=entry,
                    config=config,
                    collected_at=collected_at,
                )
                if normalized is None:
                    continue
                dedup_key = _entry_dedup_key(normalized["link"], entry.get("id"), entry.get("guid"))
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)
                rows.append(normalized)
        return pd.DataFrame(rows)

    def _fetch_feed_entries(self, feed_url: str, *, max_entries: int) -> list[Mapping[str, Any]]:
        payload = self._request(feed_url)
        parsed = feedparser.parse(payload)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            exc = getattr(parsed, "bozo_exception", None)
            raise RuntimeError(f"RSS parse failed for {feed_url}: {exc}")
        return list(parsed.entries[: max(1, int(max_entries))])

    def _request(self, feed_url: str) -> bytes | str:
        request = Request(
            feed_url,
            headers={"User-Agent": "roboquant-market-news/1.0"},
            method="GET",
        )
        try:
            if self._opener is not None:
                payload = self._opener(request)
            else:
                with urlopen(request, timeout=self._timeout) as response:
                    payload = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"RSS HTTP {exc.code} for {feed_url}") from exc
        except URLError as exc:
            raise RuntimeError(f"RSS request failed for {feed_url}: {exc.reason}") from exc
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return payload


def curated_articles_from_config(
    config: Mapping[str, Any],
    *,
    collected_at: datetime | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    stamp = collected_at or _utcnow()
    for item in list(config.get("latest_market_context", [])) + list(config.get("curated_articles", [])):
        normalized = normalize_curated_article(item, config=config, collected_at=stamp)
        if normalized is not None:
            rows.append(normalized)
    return pd.DataFrame(rows)


def normalize_curated_article(
    item: Mapping[str, Any],
    *,
    config: Mapping[str, Any] | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any] | None:
    title = _clean_text(item.get("title"))
    if not title:
        return None
    source = str(item.get("source", "curated")).strip() or "curated"
    summary = text_excerpt(_clean_text(item.get("summary")) or "", limit=500)
    link = _clean_text(item.get("link"))
    pub_date = _parse_iso_datetime(item.get("pub_date")) or collected_at or _utcnow()
    combined_text = " ".join(part for part in [title, summary] if part)
    text_config = dict((config or {}).get("text_features", {}))
    tickers = extract_tickers(
        combined_text,
        ignore_words=text_config.get("ignore_words"),
        ticker_aliases=text_config.get("ticker_aliases"),
    )
    themes = classify_themes(combined_text, theme_keywords=text_config.get("theme_keywords"))
    sentiment_raw = simple_sentiment(
        combined_text,
        positive_words=text_config.get("positive_words"),
        negative_words=text_config.get("negative_words"),
    )
    sentiment = normalize_sentiment(sentiment_raw)
    category = resolve_category(
        combined_text,
        default_category=str(item.get("category", "macro")),
        category_keywords=(config or {}).get("category_keywords"),
    )
    article_id = article_id_from_entry(
        source=source,
        link=link,
        guid=None,
        title=title,
        pub_date=pub_date,
    )
    raw_json = json.dumps(
        {"source": source, "title": title, "link": link, "curated": True},
        ensure_ascii=False,
        allow_nan=False,
    )
    return {
        "article_id": article_id,
        "source": source,
        "category": category,
        "title": title,
        "summary": summary or None,
        "link": link,
        "pub_date": pub_date,
        "tickers_json": _json(tickers),
        "themes_json": _json(themes),
        "sentiment_score": float(sentiment),
        "raw_json": raw_json,
        "collected_at": collected_at or _utcnow(),
    }


def sources_from_config(config: Mapping[str, Any]) -> list[MarketNewsFeedSource]:
    sources: list[MarketNewsFeedSource] = []
    for item in config.get("feeds", []):
        default_category = str(item.get("default_category", "macro")).strip().lower()
        if default_category not in VALID_CATEGORIES:
            default_category = "macro"
        sources.append(
            MarketNewsFeedSource(
                source=str(item.get("source", "")).strip(),
                name=str(item.get("name", "")).strip(),
                feed_url=str(item.get("feed_url", "")).strip(),
                default_category=default_category,
                enabled=bool(item.get("enabled", True)),
            )
        )
    return sources


def normalize_rss_entry(
    *,
    source: MarketNewsFeedSource,
    entry: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any] | None:
    title = _clean_text(entry.get("title"))
    if not title:
        return None

    summary = text_excerpt(
        _clean_text(entry.get("summary") or entry.get("description") or entry.get("subtitle")),
        limit=500,
    )
    link = _resolve_link(entry)
    pub_date = _parse_entry_date(entry)
    combined_text = " ".join(part for part in [title, summary] if part)
    text_config = dict((config or {}).get("text_features", {}))
    tickers = extract_tickers(
        combined_text,
        ignore_words=text_config.get("ignore_words"),
        ticker_aliases=text_config.get("ticker_aliases"),
    )
    themes = classify_themes(combined_text, theme_keywords=text_config.get("theme_keywords"))
    sentiment_raw = simple_sentiment(
        combined_text,
        positive_words=text_config.get("positive_words"),
        negative_words=text_config.get("negative_words"),
    )
    sentiment = normalize_sentiment(sentiment_raw)
    category = resolve_category(
        combined_text,
        default_category=source.default_category,
        category_keywords=(config or {}).get("category_keywords"),
    )
    article_id = article_id_from_entry(
        source=source.source,
        link=link,
        guid=_clean_text(entry.get("id") or entry.get("guid")),
        title=title,
        pub_date=pub_date,
    )
    raw_json = json.dumps(_entry_raw_payload(source, entry), ensure_ascii=False, allow_nan=False)
    return {
        "article_id": article_id,
        "source": source.source,
        "category": category,
        "title": title,
        "summary": summary or None,
        "link": link,
        "pub_date": pub_date,
        "tickers_json": _json(tickers),
        "themes_json": _json(themes),
        "sentiment_score": float(sentiment),
        "raw_json": raw_json,
        "collected_at": collected_at or _utcnow(),
    }


def resolve_category(
    text: str,
    *,
    default_category: str,
    category_keywords: Mapping[str, Sequence[str]] | None = None,
) -> str:
    if not category_keywords:
        return _normalize_category(default_category)

    priority = ("flow", "macro", "sector")
    for category in priority:
        keywords = category_keywords.get(category, [])
        if any(_contains_keyword(text, str(keyword)) for keyword in keywords):
            return category
    return _normalize_category(default_category)


def article_id_from_entry(
    *,
    source: str,
    link: str | None,
    guid: str | None,
    title: str,
    pub_date: datetime | None,
) -> str:
    raw = "|".join(
        [
            source,
            link or "",
            guid or "",
            title,
            "" if pub_date is None else pub_date.isoformat(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _entry_dedup_key(link: str | None, entry_id: object, guid: object) -> str:
    for value in (link, entry_id, guid):
        text = _clean_text(value)
        if text:
            return text.lower()
    return ""


def _entry_raw_payload(source: MarketNewsFeedSource, entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "source": source.source,
        "source_name": source.name,
        "title": _clean_text(entry.get("title")),
        "link": _resolve_link(entry),
        "id": _clean_text(entry.get("id")),
        "guid": _clean_text(entry.get("guid")),
        "published": _clean_text(entry.get("published")),
        "updated": _clean_text(entry.get("updated")),
    }


def _resolve_link(entry: Mapping[str, Any]) -> str | None:
    link = entry.get("link")
    if link:
        return _clean_text(link)
    links = entry.get("links") or []
    for item in links:
        href = item.get("href") if isinstance(item, Mapping) else getattr(item, "href", None)
        text = _clean_text(href)
        if text:
            return text
    return None


def _parse_iso_datetime(value: object) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    if hasattr(parsed, "to_pydatetime"):
        parsed = parsed.to_pydatetime()
    if getattr(parsed, "tzinfo", None) is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _parse_entry_date(entry: Mapping[str, Any]) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC).replace(tzinfo=None)
            except (TypeError, ValueError):
                continue
    for key in ("published", "updated"):
        text = _clean_text(entry.get(key))
        if not text:
            continue
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            continue
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC).replace(tzinfo=None)
        return parsed
    return None


def _normalize_category(value: str) -> str:
    category = str(value or "macro").strip().lower()
    return category if category in VALID_CATEGORIES else "macro"


def _contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii():
        return keyword.lower() in text.lower()
    return keyword in text


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

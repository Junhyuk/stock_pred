from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from os import environ
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import pandas as pd

from roboquant.data.providers.market_news_feed import resolve_category
from roboquant.signals.telegram_text import (
    classify_themes,
    extract_tickers,
    normalize_sentiment,
    simple_sentiment,
    text_excerpt,
)


class XMarketNewsConfigurationError(RuntimeError):
    """Raised when X API credentials are not configured."""


@dataclass(frozen=True)
class XMarketNewsSettings:
    username: str = "MarketNews_Feed"
    source: str = "x_marketnews_feed"
    default_category: str = "macro"
    max_results: int = 25
    exclude: tuple[str, ...] = ("retweets", "replies")


class XMarketNewsProvider:
    provider_name = "x_api_v2"
    api_base = "https://api.x.com/2"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        opener: Any | None = None,
        timeout: float = 15.0,
    ) -> None:
        values = environ if env is None else env
        token = str(values.get("X_BEARER_TOKEN", "")).strip()
        if not token or token == "change_me":
            raise XMarketNewsConfigurationError("X_BEARER_TOKEN is required for X API v2 collection.")
        self._bearer_token = token
        self._opener = opener
        self._timeout = float(timeout)

    def fetch_posts(
        self,
        *,
        settings: XMarketNewsSettings,
        config: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        user_id = self.resolve_user_id(settings.username)
        response = self._request_json(self._timeline_url(user_id, settings))
        posts = response.get("data") or []
        collected_at = _utcnow()
        rows = [
            normalize_x_post(
                post,
                settings=settings,
                config=config,
                collected_at=collected_at,
            )
            for post in posts
        ]
        rows = [row for row in rows if row is not None]
        return pd.DataFrame(rows)

    def resolve_user_id(self, username: str) -> str:
        encoded = quote(str(username).strip().lstrip("@"))
        response = self._request_json(f"{self.api_base}/users/by/username/{encoded}?user.fields=id,username,name")
        user_id = ((response.get("data") or {}).get("id") or "").strip()
        if not user_id:
            raise RuntimeError(f"X user id not found for username={username}")
        return user_id

    def _timeline_url(self, user_id: str, settings: XMarketNewsSettings) -> str:
        params = {
            "max_results": max(5, min(100, int(settings.max_results))),
            "tweet.fields": "created_at,lang,public_metrics",
        }
        exclude = [item for item in settings.exclude if item in {"retweets", "replies"}]
        if exclude:
            params["exclude"] = ",".join(exclude)
        return f"{self.api_base}/users/{quote(str(user_id))}/tweets?{urlencode(params)}"

    def _request_json(self, url: str) -> dict[str, Any]:
        request = Request(
            url,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "User-Agent": "roboquant-x-market-news/1.0",
            },
            method="GET",
        )
        try:
            if self._opener is not None:
                payload = self._opener(request)
            else:
                with urlopen(request, timeout=self._timeout) as response:
                    payload = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"X API HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"X API request failed: {exc.reason}") from exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return json.loads(payload.decode("utf-8"))


def settings_from_config(config: Mapping[str, Any]) -> XMarketNewsSettings:
    raw = config.get("x_market_news") or {}
    exclude = raw.get("exclude", ["retweets", "replies"])
    return XMarketNewsSettings(
        username=str(raw.get("username", "MarketNews_Feed")).strip().lstrip("@") or "MarketNews_Feed",
        source=str(raw.get("source", "x_marketnews_feed")).strip() or "x_marketnews_feed",
        default_category=str(raw.get("default_category", "macro")).strip().lower() or "macro",
        max_results=int(raw.get("max_results", 25)),
        exclude=tuple(str(item).strip().lower() for item in exclude if str(item).strip()),
    )


def normalize_x_post(
    post: Mapping[str, Any],
    *,
    settings: XMarketNewsSettings,
    config: Mapping[str, Any] | None = None,
    collected_at: datetime | None = None,
) -> dict[str, Any] | None:
    post_id = str(post.get("id") or "").strip()
    text = _clean_text(post.get("text"))
    if not post_id or not text:
        return None
    pub_date = _parse_datetime(post.get("created_at")) or collected_at or _utcnow()
    title = text_excerpt(text, limit=240)
    summary = text_excerpt(text, limit=500)
    link = f"https://x.com/{settings.username}/status/{post_id}"
    text_config = dict((config or {}).get("text_features", {}))
    tickers = extract_tickers(
        text,
        ignore_words=text_config.get("ignore_words"),
        ticker_aliases=text_config.get("ticker_aliases"),
    )
    themes = classify_themes(text, theme_keywords=text_config.get("theme_keywords"))
    sentiment_raw = simple_sentiment(
        text,
        positive_words=text_config.get("positive_words"),
        negative_words=text_config.get("negative_words"),
    )
    sentiment = normalize_sentiment(sentiment_raw)
    category = resolve_category(
        text,
        default_category=settings.default_category,
        category_keywords=(config or {}).get("category_keywords"),
    )
    raw_json = json.dumps(
        {
            "source": settings.source,
            "username": settings.username,
            "post_id": post_id,
            "created_at": post.get("created_at"),
            "lang": post.get("lang"),
            "public_metrics": post.get("public_metrics"),
        },
        ensure_ascii=False,
        allow_nan=False,
    )
    return {
        "article_id": article_id_from_x_post(settings.source, post_id),
        "source": settings.source,
        "category": category,
        "title": title,
        "summary": summary,
        "link": link,
        "pub_date": pub_date,
        "tickers_json": _json(tickers),
        "themes_json": _json(themes),
        "sentiment_score": float(sentiment),
        "raw_json": raw_json,
        "collected_at": collected_at or _utcnow(),
    }


def article_id_from_x_post(source: str, post_id: str) -> str:
    raw = f"{source}|{post_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if hasattr(parsed, "to_pydatetime"):
        parsed = parsed.to_pydatetime()
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value or [], ensure_ascii=False, allow_nan=False)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

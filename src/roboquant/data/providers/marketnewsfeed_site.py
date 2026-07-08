from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pandas as pd

from roboquant.data.providers.market_news_feed import resolve_category

SOURCE = "marketnewsfeed_site"


@dataclass(frozen=True)
class MarketNewsFeedSiteSettings:
    site_url: str = "https://marketnewsfeed.com/"
    telegram_channel: str = "marketfeed"
    x_username: str = "MarketNews_Feed"
    source: str = SOURCE
    default_category: str = "macro"
    enabled: bool = True
    crawl_site_metadata: bool = True


class MarketNewsFeedSiteProvider:
    provider_name = SOURCE

    def __init__(
        self,
        *,
        opener: Callable[[Request], bytes | str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._opener = opener
        self._timeout = float(timeout)

    def fetch_metadata(
        self,
        *,
        settings: MarketNewsFeedSiteSettings,
        config: Mapping[str, Any] | None = None,
    ) -> pd.DataFrame:
        if not settings.enabled or not settings.crawl_site_metadata:
            return pd.DataFrame()
        site_url = str(settings.site_url or "").strip()
        if not site_url:
            raise ValueError("marketnewsfeed.site_url is required.")

        html = self._request_html(site_url)
        parsed = parse_marketnewsfeed_html(html, base_url=site_url)
        collected_at = _utcnow()
        text = " ".join(
            part
            for part in [
                parsed.get("title"),
                parsed.get("meta_description"),
                " ".join(parsed.get("body_text", [])[:10]),
            ]
            if part
        )
        category = resolve_category(
            text,
            default_category=settings.default_category,
            category_keywords=(config or {}).get("category_keywords"),
        )
        raw_payload = {
            "source": settings.source,
            "site_url": site_url,
            "title": parsed.get("title"),
            "meta_description": parsed.get("meta_description"),
            "links": parsed.get("links", []),
            "telegram_links": parsed.get("telegram_links", []),
            "x_links": parsed.get("x_links", []),
            "configured_telegram_channel": settings.telegram_channel,
            "configured_x_username": settings.x_username,
            "contains_loading": parsed.get("contains_loading", False),
            "extraction_mode": "site_metadata_only",
            "private_or_js_endpoints_inferred": False,
        }
        title = parsed.get("title") or "Market News Feed"
        summary = _metadata_summary(settings, parsed)
        return pd.DataFrame(
            [
                {
                    "article_id": article_id_for_site(settings.source, site_url),
                    "source": settings.source,
                    "category": category,
                    "title": title,
                    "summary": summary,
                    "link": site_url,
                    "pub_date": collected_at,
                    "tickers_json": _json([]),
                    "themes_json": _json(["MACRO"]),
                    "sentiment_score": 0.5,
                    "raw_json": json.dumps(raw_payload, ensure_ascii=False, allow_nan=False),
                    "collected_at": collected_at,
                }
            ]
        )

    def _request_html(self, site_url: str) -> str:
        request = Request(
            site_url,
            headers={"User-Agent": "roboquant-marketnewsfeed-site/1.0"},
            method="GET",
        )
        try:
            if self._opener is not None:
                payload = self._opener(request)
            else:
                with urlopen(request, timeout=self._timeout) as response:
                    payload = response.read()
        except HTTPError as exc:
            raise RuntimeError(f"MarketNewsFeed site HTTP {exc.code} for {site_url}") from exc
        except URLError as exc:
            raise RuntimeError(f"MarketNewsFeed site request failed for {site_url}: {exc.reason}") from exc
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return str(payload)


class _MetadataParser(HTMLParser):
    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.in_title = False
        self.title_parts: list[str] = []
        self.body_text: list[str] = []
        self.meta_description: str | None = None
        self.links: list[dict[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        tag = tag.lower()
        if tag == "title":
            self.in_title = True
        elif tag == "meta" and attrs_map.get("name", "").lower() == "description":
            content = _clean_text(attrs_map.get("content"))
            if content:
                self.meta_description = content
        elif tag == "a":
            href = _clean_text(attrs_map.get("href"))
            if href:
                self._current_href = urljoin(self.base_url, href)
                self._current_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self.in_title = False
        elif tag == "a" and self._current_href:
            self.links.append(
                {
                    "href": self._current_href,
                    "text": _clean_text(" ".join(self._current_text)) or self._current_href,
                }
            )
            self._current_href = None
            self._current_text = []

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        if self.in_title:
            self.title_parts.append(text)
        elif self._current_href:
            self._current_text.append(text)
        else:
            self.body_text.append(text)

    def payload(self) -> dict[str, Any]:
        title = _clean_text(" ".join(self.title_parts))
        links = _dedupe_links(self.links)
        return {
            "title": title,
            "meta_description": self.meta_description,
            "links": links,
            "telegram_links": [item for item in links if "t.me/" in item["href"].lower()],
            "x_links": [
                item
                for item in links
                if "x.com/" in item["href"].lower() or "twitter.com/" in item["href"].lower()
            ],
            "body_text": self.body_text[:50],
            "contains_loading": any("loading" in item.lower() for item in self.body_text),
        }


def parse_marketnewsfeed_html(html: str, *, base_url: str) -> dict[str, Any]:
    parser = _MetadataParser(base_url=base_url)
    parser.feed(html)
    return parser.payload()


def settings_from_config(config: Mapping[str, Any]) -> MarketNewsFeedSiteSettings:
    raw = config.get("marketnewsfeed") or {}
    return MarketNewsFeedSiteSettings(
        site_url=str(raw.get("site_url", "https://marketnewsfeed.com/")).strip() or "https://marketnewsfeed.com/",
        telegram_channel=str(raw.get("telegram_channel", "marketfeed")).strip().lstrip("@") or "marketfeed",
        x_username=str(raw.get("x_username", "MarketNews_Feed")).strip().lstrip("@") or "MarketNews_Feed",
        source=str(raw.get("source", SOURCE)).strip() or SOURCE,
        default_category=str(raw.get("default_category", "macro")).strip().lower() or "macro",
        enabled=bool(raw.get("enabled", True)),
        crawl_site_metadata=bool(raw.get("crawl_site_metadata", True)),
    )


def article_id_for_site(source: str, site_url: str) -> str:
    return hashlib.sha256(f"{source}|{site_url}".encode()).hexdigest()


def _metadata_summary(settings: MarketNewsFeedSiteSettings, parsed: Mapping[str, Any]) -> str:
    telegram_count = len(parsed.get("telegram_links") or [])
    x_count = len(parsed.get("x_links") or [])
    return (
        "MarketNewsFeed site metadata. Headlines are collected through "
        f"Telegram channel {settings.telegram_channel} and X account {settings.x_username}; "
        f"site metadata exposed {telegram_count} Telegram links and {x_count} X links. "
        "No hidden JavaScript or private feed endpoints are inferred."
    )


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for item in links:
        href = item.get("href", "")
        if not href or href in seen:
            continue
        seen.add(href)
        output.append({"href": href, "text": item.get("text") or href})
    return output


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split()).strip()
    return text or None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

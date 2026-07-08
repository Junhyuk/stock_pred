from __future__ import annotations

import hashlib
import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

import pandas as pd


@dataclass(frozen=True)
class NewsUrlCandidate:
    source_table: str
    source_article_id: str
    source_url: str


class NewsUrlCrawler:
    user_agent = "roboquant-news-url-crawler/1.0"

    def __init__(
        self,
        *,
        opener: Callable[[Request], Any] | None = None,
        robots_checker: Callable[[str, str], bool] | None = None,
        respect_robots: bool = True,
        timeout: float = 10.0,
        max_bytes: int = 1_048_576,
        excerpt_chars: int = 700,
        sleep_seconds: float = 0.0,
    ) -> None:
        self._opener = opener
        self._robots_checker = robots_checker
        self._respect_robots = respect_robots
        self._timeout = timeout
        self._max_bytes = max(1024, int(max_bytes))
        self._excerpt_chars = max(120, int(excerpt_chars))
        self._sleep_seconds = max(0.0, float(sleep_seconds))
        self._last_domain: str | None = None

    def crawl(self, candidate: NewsUrlCandidate) -> dict[str, Any]:
        fetched_at = _utcnow()
        source_url = str(candidate.source_url or "").strip()
        domain = urlparse(source_url).netloc.lower()
        base = {
            "url_hash": url_hash(source_url),
            "source_table": candidate.source_table,
            "source_article_id": candidate.source_article_id,
            "source_url": source_url,
            "canonical_url": None,
            "domain": domain,
            "crawl_status": "failed",
            "http_status": None,
            "content_type": None,
            "title": None,
            "meta_description": None,
            "published_at": None,
            "excerpt": None,
            "text_hash": None,
            "robots_allowed": None,
            "error_message": None,
            "fetched_at": fetched_at,
        }
        if not source_url or not domain:
            base["crawl_status"] = "invalid_url"
            base["error_message"] = "missing or invalid URL"
            return base
        try:
            allowed = self._robots_allowed(source_url, domain)
            base["robots_allowed"] = allowed
            if not allowed:
                base["crawl_status"] = "skipped_robots"
                return base
            if self._sleep_seconds and self._last_domain == domain:
                time.sleep(self._sleep_seconds)
            self._last_domain = domain
            payload, status, content_type = self._fetch(source_url)
            base["http_status"] = status
            base["content_type"] = content_type
            if "html" not in str(content_type).lower():
                base["crawl_status"] = "non_html"
                return base
            parsed = parse_html_metadata(payload, source_url=source_url, excerpt_chars=self._excerpt_chars)
            base.update(parsed)
            base["crawl_status"] = "fetched"
            return base
        except Exception as exc:
            base["crawl_status"] = "failed"
            base["error_message"] = str(exc)[:500]
            return base

    def _fetch(self, url: str) -> tuple[bytes, int | None, str | None]:
        request = Request(url, headers={"User-Agent": self.user_agent}, method="GET")
        try:
            if self._opener is not None:
                response = self._opener(request)
            else:
                response = urlopen(request, timeout=self._timeout)
            if isinstance(response, bytes):
                return response[: self._max_bytes], None, "text/html"
            if isinstance(response, str):
                return response.encode("utf-8")[: self._max_bytes], None, "text/html"
            try:
                payload = response.read(self._max_bytes + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            headers = getattr(response, "headers", None) or getattr(response, "info", lambda: {})()
            content_type = headers.get("Content-Type") if hasattr(headers, "get") else None
            status = response.getcode() if hasattr(response, "getcode") else getattr(response, "status", None)
            return payload[: self._max_bytes], status, content_type
        except HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"request failed: {exc.reason}") from exc

    def _robots_allowed(self, url: str, domain: str) -> bool:
        if not self._respect_robots:
            return True
        if self._robots_checker is not None:
            return bool(self._robots_checker(url, self.user_agent))
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{domain}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except Exception:
            return True
        return bool(parser.can_fetch(self.user_agent, url))


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None
        self.meta_description: str | None = None
        self.published_at: str | None = None
        self.canonical_url: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_map = {key.lower(): value for key, value in attrs if key}
        tag = tag.lower()
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "meta":
            name = str(attrs_map.get("name") or attrs_map.get("property") or "").lower()
            content = attrs_map.get("content")
            if content and name in {"description", "og:description", "twitter:description"} and not self.meta_description:
                self.meta_description = _clean_text(content)
            if content and name in {"article:published_time", "og:published_time", "pubdate"} and not self.published_at:
                self.published_at = _clean_text(content)
        if tag == "link" and str(attrs_map.get("rel") or "").lower() == "canonical":
            self.canonical_url = _clean_text(attrs_map.get("href"))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title":
            self._in_title = False
            self.title = _clean_text(" ".join(self._title_parts)) or self.title
        if tag in {"script", "style", "noscript"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = _clean_text(data)
        if not text:
            return
        if self._in_title:
            self._title_parts.append(text)
            return
        if len(text) >= 25:
            self._text_parts.append(text)

    def excerpt(self, limit: int) -> str | None:
        return _clip_text(" ".join(self._text_parts), limit=limit)


def parse_html_metadata(payload: bytes, *, source_url: str, excerpt_chars: int = 700) -> dict[str, Any]:
    text = payload.decode("utf-8", errors="ignore")
    parser = _MetadataParser()
    parser.feed(text)
    excerpt = parser.excerpt(excerpt_chars)
    published_at = _parse_datetime(parser.published_at)
    return {
        "canonical_url": parser.canonical_url or source_url,
        "title": parser.title,
        "meta_description": parser.meta_description,
        "published_at": published_at,
        "excerpt": excerpt,
        "text_hash": hashlib.sha256((excerpt or "").encode("utf-8")).hexdigest() if excerpt else None,
    }


def candidates_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_empty_crawl_cache().columns)


def url_hash(url: str) -> str:
    return hashlib.sha256(str(url or "").strip().lower().encode("utf-8")).hexdigest()


def _parse_datetime(value: object) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce", utc=True)
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().astimezone(UTC).replace(tzinfo=None)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _clip_text(value: object, *, limit: int) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].strip()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _empty_crawl_cache() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "url_hash",
            "source_table",
            "source_article_id",
            "source_url",
            "canonical_url",
            "domain",
            "crawl_status",
            "http_status",
            "content_type",
            "title",
            "meta_description",
            "published_at",
            "excerpt",
            "text_hash",
            "robots_allowed",
            "error_message",
            "fetched_at",
        ]
    )

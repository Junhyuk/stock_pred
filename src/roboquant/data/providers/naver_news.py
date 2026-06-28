from __future__ import annotations

import hashlib
import html
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from os import environ
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pandas as pd


class NaverNewsConfigurationError(RuntimeError):
    """Raised when Naver Search API credentials are not configured."""


@dataclass(frozen=True)
class NaverNewsQuery:
    symbol: str
    name: str
    query: str


class NaverNewsProvider:
    provider_name = "naver_search_api"
    endpoint = "https://openapi.naver.com/v1/search/news.json"

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        opener: Any | None = None,
        timeout: float = 10.0,
    ) -> None:
        values = environ if env is None else env
        client_id = str(values.get("NAVER_CLIENT_ID", "")).strip()
        client_secret = str(values.get("NAVER_CLIENT_SECRET", "")).strip()
        if not client_id or not client_secret or "change_me" in {client_id, client_secret}:
            raise NaverNewsConfigurationError(
                "NAVER_CLIENT_ID and NAVER_CLIENT_SECRET are required for Naver News Search API."
            )
        self._client_id = client_id
        self._client_secret = client_secret
        self._opener = opener
        self._timeout = timeout

    def fetch_articles(
        self,
        queries: Sequence[NaverNewsQuery],
        *,
        query_date: date,
        display: int = 10,
        sort: str = "date",
    ) -> pd.DataFrame:
        rows = []
        collected_at = datetime.now(UTC).replace(tzinfo=None)
        for item in queries:
            response = self._request(query=item.query, display=display, sort=sort)
            for article in response.get("items", []):
                rows.append(normalize_article(item, article, query_date, collected_at))
        return pd.DataFrame(rows)

    def _request(self, *, query: str, display: int, sort: str) -> dict[str, Any]:
        params = urlencode(
            {
                "query": query,
                "display": max(1, min(100, int(display))),
                "start": 1,
                "sort": sort,
            }
        )
        request = Request(
            f"{self.endpoint}?{params}",
            headers={
                "X-Naver-Client-Id": self._client_id,
                "X-Naver-Client-Secret": self._client_secret,
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
            raise RuntimeError(f"Naver News API HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"Naver News API request failed: {exc.reason}") from exc
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        return json.loads(payload.decode("utf-8"))


def queries_from_config(items: Sequence[dict[str, Any]], template: str = "{name} 주가") -> list[NaverNewsQuery]:
    queries = []
    for item in items:
        symbol = str(item["symbol"]).zfill(6)
        name = str(item.get("name") or symbol)
        queries.append(NaverNewsQuery(symbol=symbol, name=name, query=template.format(symbol=symbol, name=name)))
    return queries


def normalize_article(
    query: NaverNewsQuery,
    article: Mapping[str, Any],
    query_date: date,
    collected_at: datetime,
) -> dict[str, Any]:
    title = _strip_html(article.get("title"))
    description = _strip_html(article.get("description"))
    originallink = _clean_string(article.get("originallink"))
    link = _clean_string(article.get("link"))
    pub_date = _parse_pub_date(article.get("pubDate"))
    raw_json = json.dumps(dict(article), ensure_ascii=False, allow_nan=False)
    article_id = _article_id(query, title, originallink, link, pub_date)
    return {
        "article_id": article_id,
        "collected_at": collected_at,
        "query_date": query_date,
        "symbol": query.symbol,
        "name": query.name,
        "query": query.query,
        "title": title,
        "description": description,
        "originallink": originallink,
        "link": link,
        "pub_date": pub_date,
        "source_name": NaverNewsProvider.provider_name,
        "sentiment_score": None,
        "raw_json": raw_json,
    }


def _article_id(
    query: NaverNewsQuery,
    title: str,
    originallink: str | None,
    link: str | None,
    pub_date: datetime | None,
) -> str:
    raw = "|".join(
        [
            query.symbol,
            originallink or "",
            link or "",
            title,
            "" if pub_date is None else pub_date.isoformat(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _strip_html(value: object) -> str:
    text = _clean_string(value) or ""
    return html.unescape(re.sub(r"<[^>]+>", "", text)).strip()


def _clean_string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_pub_date(value: object) -> datetime | None:
    text = _clean_string(value)
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed

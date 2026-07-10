from __future__ import annotations

import hashlib
import json
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import pandas as pd

from roboquant.data.providers.market_news_feed import resolve_category

SOURCE = "marketnewsfeed_site"
HEADLINE_SOURCE = "marketnewsfeed_site_shadow"
HEADLINE_COLUMNS = [
    "headline_id",
    "source",
    "site_category",
    "title",
    "source_url",
    "source_name",
    "published_at",
    "tickers_json",
    "importance",
    "channel",
    "collected_at",
    "raw_json",
]


@dataclass(frozen=True)
class MarketNewsFeedSiteSettings:
    site_url: str = "https://marketnewsfeed.com/"
    telegram_channel: str = "marketfeed"
    x_username: str = "MarketNews_Feed"
    source: str = SOURCE
    default_category: str = "macro"
    enabled: bool = True
    crawl_site_metadata: bool = True
    crawl_rendered_headlines: bool = False
    max_headlines: int = 50
    chrome_path: str | None = None


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


class _RenderedHeadlineParser(HTMLParser):
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, *, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.headlines: list[dict[str, Any]] = []
        self._depth = 0
        self._record: dict[str, Any] | None = None
        self._capture_field: str | None = None
        self._capture_tag: str | None = None
        self._capture_parts: list[str] = []
        self._source_depth: int | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attrs_map = {key.lower(): value or "" for key, value in attrs}
        classes = set(attrs_map.get("class", "").split())
        if self._record is None:
            if tag != "article" or "card" not in classes:
                return
            self._record = {
                "public_id": _clean_text(attrs_map.get("data-hid") or attrs_map.get("id")),
                "published_at": _parse_public_datetime(
                    attrs_map.get("data-ts"),
                    unix_timestamp=True,
                ),
                "title": None,
                "site_category": None,
                "source_url": None,
                "source_name": None,
                "tickers": [],
                "importance": None,
                "channel": None,
            }
            self._depth = 1
            return

        if tag not in self._VOID_TAGS:
            self._depth += 1
        if tag == "time":
            self._record["published_at"] = _parse_public_datetime(attrs_map.get("title")) or self._record["published_at"]
        elif tag == "h2" and "hl" in classes:
            self._start_capture("title", tag)
        elif tag == "span" and "cat" in classes:
            self._start_capture("site_category", tag)
        elif tag == "span" and "chan" in classes:
            self._start_capture("channel", tag)
        elif tag == "span" and "imp" in classes:
            self._record["importance"] = _parse_importance(attrs_map.get("title"))
        elif tag == "button" and "chip-sym" in classes:
            ticker = _clean_text(attrs_map.get("data-sym"))
            if ticker and ticker not in self._record["tickers"]:
                self._record["tickers"].append(ticker)
        elif tag == "div" and "srcs" in classes:
            self._source_depth = self._depth
        elif tag == "a" and self._source_depth is not None:
            href = _clean_text(attrs_map.get("href"))
            if href and not self._record["source_url"]:
                self._record["source_url"] = urljoin(self.base_url, href)
                self._start_capture("source_name", tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._record is None:
            return
        if self._capture_tag == tag:
            self._record[self._capture_field] = _clean_text(" ".join(self._capture_parts))
            self._capture_field = None
            self._capture_tag = None
            self._capture_parts = []
        if self._source_depth == self._depth and tag == "div":
            self._source_depth = None
        self._depth -= 1
        if self._depth == 0:
            if self._record.get("title"):
                self.headlines.append(self._record)
            self._record = None

    def handle_data(self, data: str) -> None:
        if self._record is None or self._capture_field is None:
            return
        text = _clean_text(data)
        if text:
            self._capture_parts.append(text)

    def _start_capture(self, field: str, tag: str) -> None:
        self._capture_field = field
        self._capture_tag = tag
        self._capture_parts = []


def parse_marketnewsfeed_rendered_html(html: str, *, base_url: str) -> list[dict[str, Any]]:
    parser = _RenderedHeadlineParser(base_url=base_url)
    parser.feed(html)
    return parser.headlines


def rendered_headlines_to_frame(
    html: str,
    *,
    base_url: str,
    collected_at: datetime | None = None,
) -> pd.DataFrame:
    stamp = collected_at or _utcnow()
    rows: list[dict[str, Any]] = []
    for item in parse_marketnewsfeed_rendered_html(html, base_url=base_url):
        public_id = str(item.get("public_id") or "").removeprefix("card-h")
        title = str(item.get("title") or "")
        published_at = item.get("published_at") or stamp
        headline_id = hashlib.sha256(
            f"{HEADLINE_SOURCE}|{public_id}|{title}|{published_at.isoformat()}".encode()
        ).hexdigest()
        raw_payload = {
            "public_id": public_id or None,
            "site_category": item.get("site_category"),
            "source_url": item.get("source_url"),
            "source_name": item.get("source_name"),
            "tickers": item.get("tickers") or [],
            "importance": item.get("importance"),
            "channel": item.get("channel"),
            "extraction_mode": "public_rendered_dom",
            "shadow_only": True,
            "impact_returns_collected": False,
            "article_body_collected": False,
            "private_or_js_endpoints_inferred": False,
        }
        rows.append(
            {
                "headline_id": headline_id,
                "source": HEADLINE_SOURCE,
                "site_category": str(item.get("site_category") or "OTHER").upper(),
                "title": title,
                "source_url": item.get("source_url"),
                "source_name": item.get("source_name"),
                "published_at": published_at,
                "tickers_json": _json(item.get("tickers") or []),
                "importance": item.get("importance"),
                "channel": item.get("channel"),
                "collected_at": stamp,
                "raw_json": json.dumps(raw_payload, ensure_ascii=False, allow_nan=False),
            }
        )
    return pd.DataFrame(rows, columns=HEADLINE_COLUMNS)


def render_marketnewsfeed_dom(
    site_url: str,
    *,
    chrome_path: str | None = None,
    timeout_seconds: float = 20.0,
) -> str:
    executable = chrome_path or _find_chrome()
    if not executable:
        raise RuntimeError("Chrome executable not found; rendered headline collection skipped.")
    timeout = max(5.0, float(timeout_seconds))
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="roboquant-marketnewsfeed-") as profile_dir:
        process = subprocess.Popen(
            [
                executable,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--remote-debugging-port=0",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile_dir}",
                site_url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        try:
            browser_ws_url = _read_devtools_url(process, deadline)
            page_ws_url = _find_page_websocket(browser_ws_url, site_url, deadline)
            return _read_rendered_dom(page_ws_url, deadline)
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _read_devtools_url(process: subprocess.Popen[str], deadline: float) -> str:
    if process.stderr is None:
        raise RuntimeError("Chrome stderr is unavailable.")
    selector = selectors.DefaultSelector()
    selector.register(process.stderr, selectors.EVENT_READ)
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"Chrome exited before DevTools was ready: {process.returncode}")
            events = selector.select(timeout=min(0.25, max(0.0, deadline - time.monotonic())))
            for key, _ in events:
                line = key.fileobj.readline()
                match = re.search(r"DevTools listening on (ws://\S+)", line)
                if match:
                    return match.group(1)
    finally:
        selector.close()
    raise RuntimeError("Timed out waiting for Chrome DevTools.")


def _find_page_websocket(browser_ws_url: str, site_url: str, deadline: float) -> str:
    parsed = urlparse(browser_ws_url)
    endpoint = f"http://{parsed.netloc}/json/list"
    while time.monotonic() < deadline:
        try:
            with urlopen(endpoint, timeout=1.0) as response:
                targets = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            time.sleep(0.1)
            continue
        pages = [item for item in targets if item.get("type") == "page"]
        matching = [item for item in pages if str(item.get("url") or "").startswith(site_url)]
        target = (matching or pages or [None])[0]
        if target and target.get("webSocketDebuggerUrl"):
            return str(target["webSocketDebuggerUrl"])
        time.sleep(0.1)
    raise RuntimeError("Timed out waiting for the MarketNewsFeed page target.")


def _read_rendered_dom(page_ws_url: str, deadline: float) -> str:
    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise RuntimeError("The websockets package is required for rendered headline collection.") from exc

    command_id = 0
    with connect(
        page_ws_url,
        open_timeout=max(1.0, deadline - time.monotonic()),
        close_timeout=1.0,
    ) as websocket:
        while time.monotonic() < deadline:
            command_id += 1
            websocket.send(
                json.dumps(
                    {
                        "id": command_id,
                        "method": "Runtime.evaluate",
                        "params": {
                            "expression": "(() => { const cards = document.querySelectorAll('main article.card'); return { count: cards.length, html: cards.length ? document.documentElement.outerHTML : '' }; })()",
                            "returnByValue": True,
                        },
                    }
                )
            )
            remaining = max(0.1, deadline - time.monotonic())
            while time.monotonic() < deadline:
                response = json.loads(websocket.recv(timeout=remaining))
                if response.get("id") != command_id:
                    remaining = max(0.1, deadline - time.monotonic())
                    continue
                value = (((response.get("result") or {}).get("result") or {}).get("value") or {})
                if int(value.get("count") or 0) > 0 and value.get("html"):
                    return str(value["html"])
                break
            time.sleep(0.2)
    raise RuntimeError("Timed out waiting for rendered MarketNewsFeed headline cards.")


def _find_chrome() -> str | None:
    for name in ("google-chrome", "chromium", "chromium-browser"):
        executable = shutil.which(name)
        if executable:
            return executable
    return None


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
        crawl_rendered_headlines=bool(raw.get("crawl_rendered_headlines", False)),
        max_headlines=max(1, int(raw.get("max_headlines", 50))),
        chrome_path=_clean_text(raw.get("chrome_path")),
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


def _parse_public_datetime(value: object, *, unix_timestamp: bool = False) -> datetime | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        parsed = pd.to_datetime(float(text), unit="s", utc=True) if unix_timestamp else pd.to_datetime(text, utc=True)
    except (TypeError, ValueError, OverflowError):
        return None
    if pd.isna(parsed):
        return None
    return parsed.to_pydatetime().astimezone(UTC).replace(tzinfo=None)


def _parse_importance(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    prefix = "importance "
    if not text.lower().startswith(prefix):
        return None
    try:
        return int(text[len(prefix) :].split("/", 1)[0])
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

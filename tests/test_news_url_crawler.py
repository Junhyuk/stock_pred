from __future__ import annotations

from datetime import date

import pandas as pd

from roboquant.data.providers.news_url_crawler import (
    NewsUrlCandidate,
    NewsUrlCrawler,
    parse_html_metadata,
)
from roboquant.db import append_dedup_table, connect_database


def test_news_url_crawler_respects_robots_without_fetching() -> None:
    fetched = False

    def opener(request):
        nonlocal fetched
        fetched = True
        return b"never"

    crawler = NewsUrlCrawler(
        opener=opener,
        robots_checker=lambda url, user_agent: False,
        respect_robots=True,
    )

    row = crawler.crawl(NewsUrlCandidate("market_news_feed", "a1", "https://example.com/news"))

    assert row["crawl_status"] == "skipped_robots"
    assert row["robots_allowed"] is False
    assert fetched is False


def test_news_url_crawler_extracts_short_html_metadata() -> None:
    html = b"""
    <html><head>
      <title>Market shock</title>
      <link rel="canonical" href="https://example.com/canonical">
      <meta name="description" content="A short market summary">
      <meta property="article:published_time" content="2026-06-10T09:00:00+09:00">
    </head><body>
      <p>Samsung Electronics and SK hynix moved after a semiconductor selloff.</p>
    </body></html>
    """

    parsed = parse_html_metadata(html, source_url="https://example.com/news", excerpt_chars=120)

    assert parsed["title"] == "Market shock"
    assert parsed["canonical_url"] == "https://example.com/canonical"
    assert parsed["meta_description"] == "A short market summary"
    assert "Samsung Electronics" in parsed["excerpt"]


def test_news_url_crawl_cache_deduplicates_by_url_hash(tmp_path) -> None:
    conn = connect_database(tmp_path / "crawl.duckdb")
    frame = pd.DataFrame(
        [
            {
                "url_hash": "same",
                "source_table": "market_news_feed",
                "source_article_id": "a1",
                "source_url": "https://example.com/a",
                "canonical_url": "https://example.com/a",
                "domain": "example.com",
                "crawl_status": "fetched",
                "http_status": 200,
                "content_type": "text/html",
                "title": "A",
                "meta_description": "B",
                "published_at": None,
                "excerpt": "short",
                "text_hash": "hash",
                "robots_allowed": True,
                "error_message": None,
                "fetched_at": date(2026, 6, 10),
            }
        ]
    )

    append_dedup_table(conn, "news_url_crawl_cache", frame, ["url_hash"])
    append_dedup_table(conn, "news_url_crawl_cache", frame, ["url_hash"])

    assert conn.execute("SELECT COUNT(*) FROM news_url_crawl_cache").fetchone()[0] == 1

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from roboquant.data.providers.market_news_feed import (
    MarketNewsFeedProvider,
    MarketNewsFeedSource,
    article_id_from_entry,
    curated_articles_from_config,
    normalize_rss_entry,
    resolve_category,
    sources_from_config,
)
from roboquant.db import append_dedup_table, connect_database

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "market_news_sample.xml"


@pytest.fixture
def sample_config() -> dict:
    return {
        "category_keywords": {
            "macro": ["금리", "Fed", "interest rate"],
            "flow": ["수급", "국민연금", "리밸런싱", "순매수", "외국인"],
            "sector": ["반도체"],
        },
        "text_features": {
            "theme_keywords": {
                "PENSION_FLOW": ["국민연금", "리밸런싱"],
                "RATE": ["금리", "Fed"],
                "FLOW": ["수급", "외국인", "순매수"],
            },
            "positive_words": ["상향", "강세"],
            "negative_words": ["하향", "약세"],
        },
    }


def test_sources_from_config_skips_disabled_and_empty_urls() -> None:
    config = {
        "feeds": [
            {
                "source": "fed_press",
                "name": "Fed",
                "feed_url": "https://example.com/fed.xml",
                "default_category": "macro",
                "enabled": True,
            },
            {
                "source": "nps_kr",
                "name": "NPS",
                "feed_url": "",
                "default_category": "flow",
                "enabled": False,
            },
        ]
    }
    sources = sources_from_config(config)
    assert len(sources) == 2
    assert sources[0].enabled is True
    assert sources[1].enabled is False


def test_resolve_category_prefers_flow_over_macro(sample_config) -> None:
    text = "국민연금 리밸런싱과 금리 전망"
    assert resolve_category(text, default_category="macro", category_keywords=sample_config["category_keywords"]) == "flow"


def test_normalize_rss_entry_extracts_themes_and_sentiment(sample_config) -> None:
    source = MarketNewsFeedSource(
        source="sample",
        name="Sample Feed",
        feed_url="https://example.com/rss",
        default_category="macro",
    )
    row = normalize_rss_entry(
        source=source,
        entry={
            "title": "국민연금, 2분기 리밸런싱 검토",
            "summary": "국내주식 비중 상향 가능성",
            "link": "https://example.com/news/1",
            "guid": "guid-1",
            "published": "Wed, 10 Jun 2026 09:00:00 +0900",
        },
        config=sample_config,
        collected_at=datetime(2026, 6, 10, 12, 0, 0),
    )
    assert row is not None
    assert row["category"] == "flow"
    assert row["title"].startswith("국민연금")
    themes = json.loads(row["themes_json"])
    assert "PENSION_FLOW" in themes
    assert 0.0 <= float(row["sentiment_score"]) <= 1.0


def test_provider_parses_fixture_and_deduplicates_by_link(sample_config) -> None:
    payload = FIXTURE_PATH.read_bytes()

    def opener(_request):
        return payload

    provider = MarketNewsFeedProvider(opener=opener)
    source = MarketNewsFeedSource(
        source="sample",
        name="Sample Feed",
        feed_url="https://example.com/rss",
        default_category="macro",
    )
    frame = provider.fetch_entries([source], config=sample_config, max_entries_per_feed=10)

    assert len(frame) == 3
    assert set(frame["category"]) == {"flow", "macro"}
    assert frame["link"].nunique() == 3


def test_curated_articles_reads_latest_market_context(sample_config) -> None:
    config = {
        **sample_config,
        "latest_market_context": [
            {
                "source": "curated",
                "category": "sector",
                "title": "반도체 급락과 외국인 차익실현",
                "summary": "AI 기술주 약세",
                "link": "https://example.com/latest",
                "pub_date": "2026-06-23",
            }
        ],
    }

    frame = curated_articles_from_config(config, collected_at=datetime(2026, 6, 23, 12))

    assert len(frame) == 1
    assert frame.iloc[0]["title"].startswith("반도체")
    assert frame.iloc[0]["category"] in {"flow", "sector"}


def test_market_news_feed_deduplicates_by_article_id(tmp_path, sample_config) -> None:
    conn = connect_database(tmp_path / "market_news.duckdb")
    source = MarketNewsFeedSource(
        source="sample",
        name="Sample Feed",
        feed_url="https://example.com/rss",
        default_category="macro",
    )
    entry = {
        "title": "Fed keeps rates unchanged",
        "summary": "Interest rate decision",
        "link": "https://example.com/news/fed-rate-1",
        "guid": "fed-1",
        "published": "Wed, 10 Jun 2026 10:00:00 +0000",
    }
    row = normalize_rss_entry(
        source=source,
        entry=entry,
        config=sample_config,
        collected_at=datetime(2026, 6, 10, 12, 0, 0),
    )
    assert row is not None
    expected_id = article_id_from_entry(
        source=source.source,
        link=row["link"],
        guid="fed-1",
        title=row["title"],
        pub_date=row["pub_date"],
    )
    assert row["article_id"] == expected_id

    frame = pd.DataFrame([row])
    append_dedup_table(conn, "market_news_feed", frame, ["article_id"])
    append_dedup_table(conn, "market_news_feed", frame, ["article_id"])

    assert conn.execute("SELECT COUNT(*) FROM market_news_feed").fetchone()[0] == 1

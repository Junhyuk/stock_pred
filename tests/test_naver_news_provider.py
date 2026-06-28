from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd
import pytest

from roboquant.data.providers.naver_news import (
    NaverNewsConfigurationError,
    NaverNewsProvider,
    NaverNewsQuery,
    normalize_article,
)
from roboquant.db import append_dedup_table, connect_database


def test_naver_news_provider_requires_credentials() -> None:
    with pytest.raises(NaverNewsConfigurationError, match="NAVER_CLIENT_ID"):
        NaverNewsProvider(env={})


def test_naver_news_provider_normalizes_mock_response() -> None:
    provider = NaverNewsProvider(
        env={"NAVER_CLIENT_ID": "client", "NAVER_CLIENT_SECRET": "secret"},
        opener=lambda request: json.dumps(
            {
                "items": [
                    {
                        "title": "<b>삼성전자</b> 주가 반등",
                        "description": "반도체 &amp; 환율 영향",
                        "originallink": "https://example.com/a",
                        "link": "https://news.naver.com/a",
                        "pubDate": "Wed, 10 Jun 2026 09:00:00 +0900",
                    }
                ]
            }
        ),
    )

    frame = provider.fetch_articles(
        [NaverNewsQuery(symbol="005930", name="삼성전자", query="삼성전자 주가")],
        query_date=date(2026, 6, 10),
    )

    assert len(frame) == 1
    assert frame.iloc[0]["title"] == "삼성전자 주가 반등"
    assert frame.iloc[0]["description"] == "반도체 & 환율 영향"
    assert frame.iloc[0]["symbol"] == "005930"
    assert frame.iloc[0]["source_name"] == "naver_search_api"


def test_news_articles_deduplicate_by_article_hash(tmp_path) -> None:
    conn = connect_database(tmp_path / "news.duckdb")
    query = NaverNewsQuery(symbol="005930", name="삼성전자", query="삼성전자 주가")
    row = normalize_article(
        query,
        {
            "title": "같은 뉴스",
            "description": "내용",
            "originallink": "https://example.com/a",
            "link": "https://news.naver.com/a",
            "pubDate": "Wed, 10 Jun 2026 09:00:00 +0900",
        },
        date(2026, 6, 10),
        datetime(2026, 6, 10),
    )

    frame = pd.DataFrame([row])
    append_dedup_table(conn, "news_articles", frame, ["article_id"])
    append_dedup_table(conn, "news_articles", frame, ["article_id"])

    assert conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 1

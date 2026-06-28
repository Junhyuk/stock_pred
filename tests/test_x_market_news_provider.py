from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from roboquant.data.providers.x_market_news import (
    XMarketNewsConfigurationError,
    XMarketNewsProvider,
    XMarketNewsSettings,
    normalize_x_post,
    settings_from_config,
)
from roboquant.db import connect_database

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "collect_x_market_news.py"
SPEC = importlib.util.spec_from_file_location("collect_x_market_news", SCRIPT_PATH)
assert SPEC is not None
collector = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def test_x_market_news_provider_fetches_and_normalizes_fixture_posts() -> None:
    def opener(request):
        url = request.full_url
        assert request.headers["Authorization"] == "Bearer test-token"
        if "/users/by/username/MarketNews_Feed" in url:
            return json.dumps({"data": {"id": "123", "username": "MarketNews_Feed"}})
        if "/users/123/tweets" in url:
            assert "exclude=retweets%2Creplies" in url
            return json.dumps(
                {
                    "data": [
                        {
                            "id": "1885000000000000001",
                            "text": "Samsung Electronics shares fall after downgrade and chip demand miss",
                            "created_at": "2026-06-26T01:02:03.000Z",
                            "lang": "en",
                            "public_metrics": {"like_count": 7},
                        }
                    ]
                }
            )
        raise AssertionError(url)

    provider = XMarketNewsProvider(env={"X_BEARER_TOKEN": "test-token"}, opener=opener)
    frame = provider.fetch_posts(
        settings=XMarketNewsSettings(username="MarketNews_Feed", max_results=5),
        config=_news_config(),
    )

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["source"] == "x_marketnews_feed"
    assert row["link"] == "https://x.com/MarketNews_Feed/status/1885000000000000001"
    assert row["pub_date"].isoformat() == "2026-06-26T01:02:03"
    assert "005930" in json.loads(row["tickers_json"])
    assert "SEMICONDUCTOR" in json.loads(row["themes_json"])
    assert row["sentiment_score"] < 0.5
    assert "test-token" not in row["raw_json"]


def test_x_market_news_provider_requires_bearer_token() -> None:
    with pytest.raises(XMarketNewsConfigurationError):
        XMarketNewsProvider(env={})


def test_collect_x_market_news_missing_token_skips_without_fake_rows(tmp_path) -> None:
    db_path = tmp_path / "x_missing.duckdb"
    config_path = tmp_path / "x.yaml"
    config_path.write_text(
        f"""
paths:
  database: {db_path}

x_market_news:
  enabled: true
  username: MarketNews_Feed
  source: x_marketnews_feed
  market_news_config: configs/market_news.yaml
""",
        encoding="utf-8",
    )

    result = collector.run_collection(config_path, allow_missing_key=True, env={})

    assert "skipped X market news" in result
    conn = connect_database(db_path)
    assert conn.execute("SELECT COUNT(*) FROM market_news_feed").fetchone()[0] == 0
    failure = conn.execute(
        "SELECT source, error_message FROM collection_failures WHERE step = 'collect_x_market_news'"
    ).fetchone()
    assert failure[0] == "x_marketnews_feed"
    assert "X_BEARER_TOKEN" in failure[1]


def test_settings_from_config_defaults_to_marketnews_feed() -> None:
    settings = settings_from_config({"x_market_news": {}})

    assert settings.username == "MarketNews_Feed"
    assert settings.source == "x_marketnews_feed"
    assert settings.exclude == ("retweets", "replies")


def test_normalize_x_post_ignores_empty_text() -> None:
    row = normalize_x_post(
        {"id": "1", "text": ""},
        settings=XMarketNewsSettings(),
        config=_news_config(),
    )

    assert row is None


def _news_config() -> dict:
    return {
        "category_keywords": {"sector": ["chip", "Samsung"], "macro": ["Fed"]},
        "text_features": {
            "ticker_aliases": {"Samsung Electronics": "005930"},
            "theme_keywords": {"SEMICONDUCTOR": ["chip", "Samsung"]},
            "positive_words": ["beat", "upgrade"],
            "negative_words": ["downgrade", "miss", "fall"],
        },
    }

from __future__ import annotations

import json
from datetime import datetime

from roboquant.dashboard.local_llm_news_service import get_local_llm_news_status
from roboquant.db import connect_database
from roboquant.signals.local_llm_news import (
    _thinking_chunks,
    _validated_themes,
    build_local_llm_news_signal_daily,
    refresh_local_llm_documents,
    settings_from_config,
    summarize_local_llm_news,
)


class FakeOllama:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def probe(self, _model_name: str) -> dict:
        return {"model_digest": "fixture-digest", "server_version": "test", "details": {}}

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        documents = json.loads(kwargs["messages"][1]["content"])["documents"]
        return (
            {
                "items": [
                    {
                        "id": document["id"],
                        "summary_ko": "반도체 수요 둔화",
                        "event_type": "sector",
                        "direction": "risk_off",
                        "importance": 4,
                        "confidence": 0.8,
                        "themes": ["SEMICONDUCTOR"],
                    }
                    for document in documents
                ]
            },
            {"input_tokens": 19, "output_tokens": 11},
        )


def _config() -> dict:
    return {
        "local_llm_news": {
            "model_name": "fixture-local",
            "prompt_version": "fixture-v1",
            "market_open_cutoff_kst": "08:00",
            "document_max_chars": 320,
            "batch_size": 6,
            "num_ctx": 4096,
            "max_output_tokens": 180,
            "thinking_mode": "off",
            "close_max_documents": 30,
            "premarket_max_documents": 12,
            "daily_brief": {"enabled": False},
        }
    }


def test_local_llm_theme_validation_rejects_short_ascii_substrings() -> None:
    assert "AI" not in _validated_themes(["AI"], "France's CAC 40 rises; Spain is mixed")
    assert "AI" in _validated_themes(["AI"], "AI GPU demand lifts data center spending")


def _seed_sources(conn) -> None:
    conn.execute(
        """
        INSERT INTO symbols (symbol, name, market, is_active, collected_at)
        VALUES ('005930', '삼성전자', 'KOSPI', TRUE, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO prices_daily (date, symbol, close, source, collected_at) VALUES
          ('2026-07-10', '005930', 100, 'fixture', CURRENT_TIMESTAMP),
          ('2026-07-13', '005930', 101, 'fixture', CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO market_news_feed (
          article_id, source, category, title, summary, link, pub_date,
          tickers_json, themes_json, sentiment_score, raw_json, collected_at
        ) VALUES (
          'fixture-news', 'fixture', 'market', ?, 'short summary', 'https://example.test/private-news',
          '2026-07-10 00:30:00', '["999999"]', '[]', 0.5, '{}', '2026-07-10 00:35:00'
        )
        """,
        ["삼성전자 반도체 수요 " + "x" * 400],
    )
    conn.execute(
        """
        INSERT INTO marketnewsfeed_headlines (
          headline_id, source, site_category, title, source_url, source_name,
          published_at, tickers_json, importance, channel, collected_at, raw_json
        ) VALUES (
          'fixture-headline', 'marketnewsfeed_site', 'STOCKS', 'KOSPI 급락 경계',
          'https://example.test/marketnewsfeed', 'fixture', '2026-07-09 22:00:00',
          '[]', 4, 'MF', '2026-07-09 22:01:00', '{}'
        )
        """
    )


def test_local_llm_queue_cache_cutoff_and_aggregate_privacy(tmp_path) -> None:
    conn = connect_database(tmp_path / "local_llm.duckdb")
    _seed_sources(conn)
    fake = FakeOllama()

    queued = refresh_local_llm_documents(conn, _config())
    first = summarize_local_llm_news(conn, _config(), stage="close", client=fake)
    second = summarize_local_llm_news(conn, _config(), stage="close", client=fake)
    signals = build_local_llm_news_signal_daily(conn, _config())

    assert queued["sources"] == {"market_news_feed": 1, "marketnewsfeed": 1}
    assert first["status"] == "ready"
    assert first["input_tokens"] == 19
    assert first["output_tokens"] == 11
    assert second["requested_document_count"] == 0
    assert len(fake.calls) == 1
    assert fake.calls[0]["num_ctx"] == 4096
    assert fake.calls[0]["max_output_tokens"] == 180
    queued_documents = json.loads(fake.calls[0]["messages"][1]["content"])["documents"]
    assert max(len(item["title"]) for item in queued_documents) <= 320
    assert set(signals["signal_date"]) == {datetime(2026, 7, 10).date(), datetime(2026, 7, 13).date()}
    assert signals.loc[signals["signal_date"] == datetime(2026, 7, 13).date(), "assessment_count"].max() == 1

    assessment = conn.execute("SELECT mapped_symbols_json FROM local_llm_news_assessments").fetchdf()
    assert "999999" not in assessment.to_json()
    status = get_local_llm_news_status(conn)
    aggregate = get_local_llm_news_status(conn, aggregate_only=True)
    assert status["items"]
    assert "https://example.test/private-news" in json.dumps(status, ensure_ascii=False)
    assert "https://example.test/private-news" not in json.dumps(aggregate, ensure_ascii=False)
    assert "summary_ko" not in json.dumps(aggregate, ensure_ascii=False)
    assert "items" not in aggregate
    assert "brief" not in aggregate
    conn.close()


def test_local_llm_batches_fit_the_structured_output_cap() -> None:
    settings = settings_from_config(_config())
    documents = [{"title": f"fixture {index}"} for index in range(6)]

    chunks = _thinking_chunks(documents, settings, remaining_probes=0)

    assert len(chunks) == 3
    assert all(len(chunk) == 2 for _, chunk in chunks)

from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from roboquant.dashboard.marketnewsfeed_llm_service import get_marketnewsfeed_llm_status
from roboquant.db import connect_database
from roboquant.marketnewsfeed_llm_shadow import build_market_range_shadow, evaluate_shadow_gate
from roboquant.signals.marketnewsfeed_llm import (
    LocalOllamaClient,
    attach_marketnewsfeed_llm_features,
    build_marketnewsfeed_llm_signal_daily,
    summarize_marketnewsfeed_headlines,
)


class FakeOllama:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def probe(self, model_name: str) -> dict:
        return {"model_digest": "fixture-digest", "server_version": "test", "details": {}}

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        items = []
        for item in kwargs["messages"][1]["content"].split('"headlines":', 1)[1].rsplit("}", 1)[0].split("}"):
            if '"id"' not in item:
                continue
            headline_id = item.split('"id":"', 1)[1].split('"', 1)[0]
            items.append(
                {
                    "id": headline_id,
                    "summary_ko": "반도체 관련 공개 headline을 요약했습니다.",
                    "event_type": "sector",
                    "direction": "risk_off",
                    "importance": 4,
                    "confidence": 0.8,
                    "themes": ["SEMICONDUCTOR", "NOT_ALLOWED"],
                    "invented_symbols": ["999999"],
                }
            )
        return {"items": items, "thinking": "this must never be stored"}, {"input_tokens": 17, "output_tokens": 9}


def _config() -> dict:
    return {
        "marketnewsfeed_llm": {
            "source": "marketnewsfeed_site_shadow",
            "model_name": "test-local",
            "prompt_version": "test-v1",
            "max_headlines_per_run": 30,
            "batch_size": 6,
            "max_headline_chars": 280,
            "num_ctx": 4096,
            "max_output_tokens": 360,
            "thinking_mode": "auto",
            "thinking_importance_threshold": 4,
            "market_open_cutoff_kst": "08:00",
            "min_signal_days_for_shadow": 60,
        }
    }


def _seed_headline(conn, *, headline_id: str = "h-1", published_at: str = "2026-07-09 22:00:00") -> None:
    conn.execute(
        """
        INSERT INTO symbols (symbol, name, market, is_active, collected_at)
        VALUES ('005930', 'Samsung Electronics', 'KOSPI', TRUE, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO marketnewsfeed_headlines (
          headline_id, source, site_category, title, source_url, source_name,
          published_at, tickers_json, importance, channel, collected_at, raw_json
        ) VALUES (?, 'marketnewsfeed_site_shadow', 'STOCKS',
          'Samsung Electronics semiconductor demand weakens', 'https://example.test/item', 'fixture',
          ?, '[]', 4, 'MF', '2026-07-09 22:01:00', '{}')
        """,
        [headline_id, published_at],
    )


def test_local_ollama_refuses_non_loopback_host() -> None:
    with pytest.raises(ValueError):
        LocalOllamaClient("https://example.com")


def test_assessment_cache_token_caps_and_no_reasoning_trace(tmp_path) -> None:
    conn = connect_database(tmp_path / "llm.duckdb")
    _seed_headline(conn)
    fake = FakeOllama()

    first = summarize_marketnewsfeed_headlines(conn, _config(), client=fake)
    second = summarize_marketnewsfeed_headlines(conn, _config(), client=fake)

    assessment = conn.execute("SELECT * FROM marketnewsfeed_llm_assessments").fetchdf().iloc[0].to_dict()
    assert first["status"] == "ready"
    assert first["input_tokens"] == 17
    assert first["output_tokens"] == 9
    assert second["requested_headline_count"] == 0
    assert len(fake.calls) == 1
    assert fake.calls[0]["num_ctx"] == 4096
    assert fake.calls[0]["max_output_tokens"] == 360
    assert assessment["mapped_symbols_json"] == '["005930"]'
    assert "thinking" not in assessment
    assert "this must never be stored" not in str(assessment)
    conn.close()


def test_cutoff_safe_signal_and_feature_attachment(tmp_path) -> None:
    conn = connect_database(tmp_path / "llm.duckdb")
    _seed_headline(conn, published_at="2026-07-10 00:30:00")  # 09:30 KST, after the 08:00 cutoff.
    conn.execute(
        """
        INSERT INTO prices_daily (date, symbol, close, source, collected_at) VALUES
          ('2026-07-10', '005930', 100, 'fixture', CURRENT_TIMESTAMP),
          ('2026-07-13', '005930', 101, 'fixture', CURRENT_TIMESTAMP)
        """
    )
    summarize_marketnewsfeed_headlines(conn, _config(), client=FakeOllama())
    signals = build_marketnewsfeed_llm_signal_daily(conn, _config())

    assert set(signals["signal_date"]) == {pd.Timestamp("2026-07-13").date()}
    features = pd.DataFrame(
        [{"date": "2026-07-13", "symbol": "005930", "horizon": "2M"}]
    )
    attached = attach_marketnewsfeed_llm_features(features, signals)
    assert attached.iloc[0]["llm_news_coverage_score"] > 0
    assert attached.iloc[0]["llm_news_risk_score"] > 0
    conn.close()


def test_market_range_shadow_is_chronological_and_never_production(tmp_path) -> None:
    conn = connect_database(tmp_path / "range.duckdb")
    dates = pd.bdate_range("2026-01-02", periods=75)
    for market in ("KOSPI", "KOSDAQ"):
        benchmark_rows = []
        price = 100.0
        for index, trade_date in enumerate(dates):
            price *= 1.0 + ((index % 7) - 3) / 1000.0
            benchmark_rows.append((trade_date.date(), market, price))
        conn.executemany("INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, ?, ?)", benchmark_rows)
    signal_rows = []
    for _index, trade_date in enumerate(dates):
        signal_rows.append(
            (
                trade_date.date(), "market", "ALL", 0.2, 0.3, 0.4, 0.5, 0.1, 0.6, 1.0, 2,
                "[]", "fixture", datetime.now(UTC).replace(tzinfo=None),
            )
        )
    conn.executemany(
        """
        INSERT INTO marketnewsfeed_llm_signal_daily (
          signal_date, scope, symbol, llm_news_attention, llm_news_risk, llm_news_direction,
          llm_news_semiconductor, llm_news_macro, llm_news_confidence, llm_news_coverage,
          assessment_count, evidence_json, feature_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        signal_rows,
    )
    frame, summaries = build_market_range_shadow(conn, model_version="test", min_signal_days=60)
    assert not frame.empty
    assert frame["production_weight"].eq(0.0).all()
    assert {item["market"] for item in summaries} == {"KOSPI", "KOSDAQ"}
    conn.close()


def test_shadow_gate_and_local_status_are_safe(tmp_path) -> None:
    baseline = {
        "precision_at_k": 0.2,
        "ic": 0.1,
        "avg_excess_return": 0.01,
        "mdd": -0.10,
        "avg_turnover": 0.5,
    }
    candidate = {**baseline, "precision_at_k": 0.21, "ic": 0.11, "mdd": -0.11, "avg_turnover": 0.6}
    assert evaluate_shadow_gate(candidate, baseline, {"max_mdd_worsening": 0.02, "max_turnover_ratio": 1.3})[0] == "accepted_shadow"
    rejected = {**candidate, "ic": 0.01}
    assert evaluate_shadow_gate(rejected, baseline, {"max_mdd_worsening": 0.02, "max_turnover_ratio": 1.3})[0] == "rejected"

    conn = connect_database(tmp_path / "status.duckdb")
    conn.execute(
        """
        INSERT INTO marketnewsfeed_llm_shadow_runs (
          run_id, model_name, feature_set_name, horizon, signal_days,
          mature_prediction_dates, gate_status, production_weight,
          baseline_metrics_json, candidate_metrics_json, fail_reason,
          report_path, created_at
        ) VALUES (
          'fixture', 'fixture-model', 'fixture-features', '2M', 60,
          60, 'accepted_shadow', 0.0, '{}', '{}', NULL,
          '/private/local/report.json', CURRENT_TIMESTAMP
        )
        """
    )
    payload = get_marketnewsfeed_llm_status(conn)
    assert payload["status"] == "not_ready"
    assert payload["production_weight"] == 0.0
    aggregate = get_marketnewsfeed_llm_status(conn, aggregate_only=True)
    assert "items" not in aggregate
    assert aggregate["signals"] == {"status": "not_ready"}
    assert "report_path" not in json.dumps(aggregate)
    assert aggregate["shadow"][0]["gate_status"] == "accepted_shadow"
    conn.close()

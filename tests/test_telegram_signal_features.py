from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd

from roboquant.db import append_dedup_table, connect_database
from roboquant.signals.telegram_signals import (
    attach_telegram_market_features,
    build_telegram_market_signal_daily,
    build_telegram_signal_daily,
    normalize_telegram_message,
    render_daily_report,
)


def test_telegram_posts_deduplicate_by_channel_and_message_id(tmp_path) -> None:
    conn = connect_database(tmp_path / "telegram.duckdb")
    post, mentions = normalize_telegram_message(
        channel="kwusa",
        message_id=101,
        message_date=datetime(2026, 6, 21, 1, 0),
        text="NVDA 실적 서프라이즈 AI 데이터센터 성장",
        source_weight=1.0,
    )

    frame = pd.DataFrame([post])
    append_dedup_table(conn, "telegram_posts", frame, ["channel", "message_id"])
    append_dedup_table(conn, "telegram_posts", frame, ["channel", "message_id"])
    append_dedup_table(conn, "telegram_ticker_mentions", pd.DataFrame(mentions), ["mention_id"])

    assert conn.execute("SELECT COUNT(*) FROM telegram_posts").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM telegram_ticker_mentions").fetchone()[0] == 1


def test_telegram_signal_daily_uses_only_messages_before_asof(tmp_path) -> None:
    conn = connect_database(tmp_path / "telegram_signal.duckdb")
    asof = datetime(2026, 6, 21, 12, 0)
    before = [
        _message("kwusa", 1, asof - timedelta(hours=2), "NVDA 실적 서프라이즈 AI 성장", 1.0),
        _message("mkglobalinvest", 2, asof - timedelta(minutes=30), "엔비디아 데이터센터 수혜", 0.8),
    ]
    future = _message(
        "FastStockNewsUSA",
        3,
        asof + timedelta(minutes=30),
        "NVDA 급락 소송 규제 쇼크",
        0.7,
    )

    _insert_messages(conn, before)
    first = build_telegram_signal_daily(conn, asof=asof)
    _insert_messages(conn, [future])
    second = build_telegram_signal_daily(conn, asof=asof)

    first_row = first[first["ticker"] == "NVDA"].iloc[0]
    second_row = second[second["ticker"] == "NVDA"].iloc[0]
    assert first_row["mention_count_24h"] == 2
    assert second_row["mention_count_24h"] == 2
    assert first_row["sentiment_avg_24h"] == second_row["sentiment_avg_24h"]


def test_render_daily_report_contains_evidence_and_disclaimer(tmp_path) -> None:
    conn = connect_database(tmp_path / "telegram_report.duckdb")
    asof = datetime(2026, 6, 21, 12, 0)
    _insert_messages(
        conn,
        [_message("kwusa", 10, asof - timedelta(hours=1), "AAPL 목표가 상향 리포트", 1.0)],
    )

    signals = build_telegram_signal_daily(conn, asof=asof)
    report = render_daily_report(signals, asof=asof)

    assert "AAPL" in report
    assert "kwusa" in report
    assert "투자 권유가 아니며" in report
    evidence = json.loads(signals.iloc[0]["evidence_json"])
    assert evidence[0]["telegram_url"] == "https://t.me/kwusa/10"


def test_telegram_market_signal_daily_builds_market_wide_features(tmp_path) -> None:
    conn = connect_database(tmp_path / "telegram_market.duckdb")
    asof = datetime(2026, 6, 21, 12, 0)
    _insert_messages(
        conn,
        [
            _message("sypark_strategy", 20, asof - timedelta(minutes=30), "반도체 급락 외국인 차익실현 환율 부담", 0.9),
            _message("marketfeed", 21, asof - timedelta(hours=2), "Market selloff risk semiconductor ETF", 0.8),
        ],
    )

    frame = build_telegram_market_signal_daily(conn, asof=asof)

    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["message_count_24h"] == 2
    assert row["message_count_1h"] == 1
    assert row["telegram_attention_score"] > 0
    assert row["telegram_semiconductor_score"] > 0
    evidence = json.loads(row["evidence_json"])
    assert evidence[0]["channel"] in {"sypark_strategy", "marketfeed"}


def test_attach_telegram_market_features_uses_semantic_defaults() -> None:
    features = pd.DataFrame([{"date": datetime(2026, 6, 21).date(), "symbol": "005930", "horizon": "2M"}])

    output = attach_telegram_market_features(features, pd.DataFrame())

    assert output.iloc[0]["telegram_attention_score"] == 0.0
    assert output.iloc[0]["telegram_sentiment_score"] == 0.5
    assert output.iloc[0]["telegram_risk_score"] == 0.0


def _message(channel: str, message_id: int, dt: datetime, text: str, weight: float):
    return normalize_telegram_message(
        channel=channel,
        message_id=message_id,
        message_date=dt,
        text=text,
        source_weight=weight,
    )


def _insert_messages(conn, messages) -> None:
    posts = []
    mentions = []
    for post, mention_rows in messages:
        posts.append(post)
        mentions.extend(mention_rows)
    append_dedup_table(conn, "telegram_posts", pd.DataFrame(posts), ["channel", "message_id"])
    append_dedup_table(
        conn,
        "telegram_ticker_mentions",
        pd.DataFrame(mentions),
        ["mention_id"],
    )

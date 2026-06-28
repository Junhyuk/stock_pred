from __future__ import annotations

from roboquant.signals.telegram_text import (
    classify_themes,
    extract_risk_keywords,
    extract_tickers,
    normalize_sentiment,
    simple_sentiment,
    urgency_score,
)


def test_extract_tickers_excludes_market_abbreviations() -> None:
    text = "NVDA AAPL MSFT AI CEO CPI ETF SEC FED 급등, 엔비디아 데이터센터 수혜"

    tickers = extract_tickers(text)

    assert tickers == ["AAPL", "MSFT", "NVDA"]


def test_sentiment_theme_urgency_and_risk_keywords() -> None:
    positive = "속보 NVDA 실적 서프라이즈, AI GPU 데이터센터 성장"
    negative = "TSLA 소송과 규제, 성장 둔화로 약세"
    risk = "급등확정 수익보장 리딩방 입장"

    assert simple_sentiment(positive) > 0
    assert normalize_sentiment(simple_sentiment(positive)) > 0.5
    assert simple_sentiment(negative) < 0
    assert classify_themes(positive) == ["AI"]
    assert urgency_score(positive) == 1.0
    assert {"급등확정", "수익보장", "리딩방"}.issubset(set(extract_risk_keywords(risk)))

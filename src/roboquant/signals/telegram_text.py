from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence

DEFAULT_IGNORE_WORDS = {
    "AI",
    "CEO",
    "CPI",
    "PCE",
    "FOMC",
    "ETF",
    "EPS",
    "GDP",
    "USA",
    "USD",
    "SEC",
    "FED",
    "PMI",
    "ISM",
    "IPO",
    "ADR",
    "THE",
    "AND",
    "FOR",
}

DEFAULT_TICKER_ALIASES = {
    "엔비디아": "NVDA",
    "NVIDIA": "NVDA",
    "테슬라": "TSLA",
    "애플": "AAPL",
    "마이크로소프트": "MSFT",
    "아마존": "AMZN",
    "메타": "META",
    "구글": "GOOGL",
    "알파벳": "GOOGL",
    "브로드컴": "AVGO",
    "마이크론": "MU",
}

DEFAULT_THEME_KEYWORDS = {
    "AI": ["AI", "인공지능", "데이터센터", "GPU", "LLM", "NVIDIA", "엔비디아"],
    "SEMICONDUCTOR": ["반도체", "HBM", "DRAM", "파운드리", "TSMC", "마이크론", "Micron"],
    "RATE": ["금리", "FOMC", "10년물", "국채", "연준", "Fed", "yield"],
    "ENERGY": ["유가", "원유", "WTI", "천연가스", "전력", "전력망"],
    "BIO": ["FDA", "임상", "신약", "바이오", "제약"],
    "DEFENSE": ["방산", "국방", "미사일", "드론"],
    "CRYPTO": ["비트코인", "BTC", "이더리움", "ETH", "코인"],
}

DEFAULT_URGENCY_KEYWORDS = {
    "high": ["속보", "급등", "급락", "서프라이즈", "가이던스", "인수", "합병", "소송"],
    "medium": ["실적", "매출", "EPS", "목표가", "상향", "하향", "리포트"],
    "low": ["전망", "분석", "코멘트", "브리핑"],
}

DEFAULT_POSITIVE_WORDS = [
    "상향",
    "호조",
    "강세",
    "서프라이즈",
    "수혜",
    "성장",
    "돌파",
    "최고",
    "beat",
    "upgrade",
    "bullish",
]

DEFAULT_NEGATIVE_WORDS = [
    "하향",
    "부진",
    "약세",
    "쇼크",
    "규제",
    "소송",
    "감소",
    "둔화",
    "miss",
    "downgrade",
    "bearish",
]

DEFAULT_RISK_KEYWORDS = [
    "수익보장",
    "급등확정",
    "리딩방",
    "무료방",
    "유료방",
    "송금",
    "계좌",
    "몰빵",
    "무조건",
    "확정",
    "guaranteed",
    "pump",
]

TICKER_PATTERN = re.compile(r"(?<![A-Za-z0-9])[$#]?[A-Z]{1,5}(?![A-Za-z0-9])")
URL_PATTERN = re.compile(r"https?://[^\s)>\]]+")
WHITESPACE_PATTERN = re.compile(r"\s+")


def extract_tickers(
    text: str | None,
    *,
    ignore_words: Sequence[str] | None = None,
    ticker_aliases: Mapping[str, str] | None = None,
) -> list[str]:
    if not text:
        return []

    ignored = {word.upper() for word in (ignore_words or DEFAULT_IGNORE_WORDS)}
    aliases = {**DEFAULT_TICKER_ALIASES, **(dict(ticker_aliases or {}))}
    found = {
        match.group(0).replace("$", "").replace("#", "").upper()
        for match in TICKER_PATTERN.finditer(text)
    }
    found = {ticker for ticker in found if ticker not in ignored}

    for alias, ticker in aliases.items():
        if alias and _contains_keyword(text, alias):
            normalized = str(ticker).upper().strip()
            if normalized and normalized not in ignored:
                found.add(normalized)

    return sorted(found)


def classify_themes(
    text: str | None,
    *,
    theme_keywords: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    if not text:
        return []
    keywords = theme_keywords or DEFAULT_THEME_KEYWORDS
    themes = [
        str(theme).upper()
        for theme, values in keywords.items()
        if any(_contains_keyword(text, keyword) for keyword in values)
    ]
    return sorted(set(themes))


def simple_sentiment(
    text: str | None,
    *,
    positive_words: Sequence[str] | None = None,
    negative_words: Sequence[str] | None = None,
) -> float:
    if not text:
        return 0.0
    positives = positive_words or DEFAULT_POSITIVE_WORDS
    negatives = negative_words or DEFAULT_NEGATIVE_WORDS
    pos_count = sum(1 for word in positives if _contains_keyword(text, word))
    neg_count = sum(1 for word in negatives if _contains_keyword(text, word))
    total = pos_count + neg_count
    if total == 0:
        return 0.0
    return (pos_count - neg_count) / total


def normalize_sentiment(raw_score: float) -> float:
    return max(0.0, min(1.0, (float(raw_score) + 1.0) / 2.0))


def urgency_score(
    text: str | None,
    *,
    urgency_keywords: Mapping[str, Sequence[str]] | None = None,
) -> float:
    if not text:
        return 0.0
    keywords = urgency_keywords or DEFAULT_URGENCY_KEYWORDS
    if any(_contains_keyword(text, keyword) for keyword in keywords.get("high", [])):
        return 1.0
    if any(_contains_keyword(text, keyword) for keyword in keywords.get("medium", [])):
        return 0.6
    if any(_contains_keyword(text, keyword) for keyword in keywords.get("low", [])):
        return 0.3
    return 0.0


def extract_risk_keywords(
    text: str | None,
    *,
    risk_keywords: Sequence[str] | None = None,
) -> list[str]:
    if not text:
        return []
    keywords = risk_keywords or DEFAULT_RISK_KEYWORDS
    return sorted({str(keyword) for keyword in keywords if _contains_keyword(text, str(keyword))})


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return [match.group(0).rstrip(".,") for match in URL_PATTERN.finditer(text)]


def text_excerpt(text: str | None, limit: int = 220) -> str:
    normalized = WHITESPACE_PATTERN.sub(" ", str(text or "")).strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "..."


def duplicate_key(text: str | None, urls: Sequence[str] | None = None) -> str:
    if urls:
        return f"url:{str(urls[0]).strip().lower()}"
    normalized = URL_PATTERN.sub("", str(text or ""))
    normalized = re.sub(r"[^0-9A-Za-z가-힣]+", " ", normalized).lower()
    normalized = WHITESPACE_PATTERN.sub(" ", normalized).strip()
    return "text:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def _contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii():
        return keyword.lower() in text.lower()
    return keyword in text

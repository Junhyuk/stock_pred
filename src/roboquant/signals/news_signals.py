from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.db import table_exists
from roboquant.signals.telegram_text import (
    classify_themes,
    extract_risk_keywords,
    extract_tickers,
    normalize_sentiment,
    simple_sentiment,
    text_excerpt,
    urgency_score,
)

KST = ZoneInfo("Asia/Seoul")

X_NEWS_TRAINING_FEATURE_COLUMNS = [
    "x_news_count_24h",
    "x_news_count_3d",
    "x_news_negative_count_3d",
    "x_news_negative_attention_score",
    "x_news_bias_adjusted_sentiment_score",
]

NEWS_TRAINING_FEATURE_COLUMNS = [
    "news_headline_count_1d",
    "news_headline_count_3d",
    "news_attention_score",
    "news_sentiment_score",
    "news_urgency_score",
    "news_risk_score",
    "news_semiconductor_score",
    "news_macro_score",
    "news_flow_score",
    "news_negative_count_3d",
    "news_negative_ratio_3d",
    "news_negative_attention_score",
    "news_bias_adjusted_sentiment_score",
    "news_source_diversity_score",
    *X_NEWS_TRAINING_FEATURE_COLUMNS,
]

NEWS_SIGNAL_DEFAULTS = {
    "news_headline_count_1d": 0.0,
    "news_headline_count_3d": 0.0,
    "news_attention_score": 0.0,
    "news_sentiment_score": 0.5,
    "news_urgency_score": 0.0,
    "news_risk_score": 0.0,
    "news_semiconductor_score": 0.0,
    "news_macro_score": 0.0,
    "news_flow_score": 0.0,
    "news_negative_count_3d": 0.0,
    "news_negative_ratio_3d": 0.0,
    "news_negative_attention_score": 0.0,
    "news_bias_adjusted_sentiment_score": 0.5,
    "news_source_diversity_score": 0.0,
    "x_news_count_24h": 0.0,
    "x_news_count_3d": 0.0,
    "x_news_negative_count_3d": 0.0,
    "x_news_negative_attention_score": 0.0,
    "x_news_bias_adjusted_sentiment_score": 0.5,
}

X_MARKET_NEWS_SOURCE = "x_marketnews_feed"

DEFAULT_LOCAL_TICKER_ALIASES = {
    "Samsung Electronics": "005930",
    "Samsung Electronics Co": "005930",
    "Samsung Electronics Co Ltd": "005930",
    "Samsung Electronics Co.": "005930",
    "Samsung Electronics Co., Ltd.": "005930",
    "005930.KS": "005930",
    "005930 KRX": "005930",
    "SSNLF": "005930",
    "SK hynix": "000660",
    "SK Hynix": "000660",
    "SK hynix Inc": "000660",
    "000660.KS": "000660",
    "LG Electronics": "066570",
    "LG Electronics Inc": "066570",
    "066570.KS": "066570",
    "SL Corp": "005850",
    "SL Corporation": "005850",
    "005850.KS": "005850",
}

DEFAULT_NEGATIVE_BUSINESS_KEYWORDS = [
    "하향",
    "부진",
    "약세",
    "급락",
    "차익실현",
    "규제",
    "감소",
    "둔화",
    "실망",
    "쇼크",
    "sell-off",
    "selloff",
    "downgrade",
    "miss",
    "weak",
    "slump",
    "tumble",
    "falls",
    "loss",
    "probe",
]

DEFAULT_NEWS_SIGNAL_SETTINGS = {
    "negative_weight": 2.0,
    "positive_weight": 0.75,
    "negative_sentiment_threshold": 0.45,
    "positive_sentiment_threshold": 0.60,
}


def build_news_signal_daily(
    conn,
    config: Mapping[str, Any] | None = None,
    *,
    signal_date: str | date = "latest",
    symbols: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Build daily, headline-only news signals from approved stored news tables."""
    target_date = _resolve_signal_date(conn, signal_date)
    text_config = _text_config(config)
    settings = _news_signal_settings(config)
    universe_symbols = _resolve_symbols(conn, symbols)
    rows = _news_rows(conn, text_config, settings)
    if rows.empty:
        return pd.DataFrame(columns=_signal_columns())

    rows["news_date"] = pd.to_datetime(rows["news_date"], errors="coerce").dt.date
    rows = rows[rows["news_date"].notna()]
    window_start = target_date - timedelta(days=2)
    rows = rows[(rows["news_date"] >= window_start) & (rows["news_date"] <= target_date)]
    if rows.empty:
        return pd.DataFrame(columns=_signal_columns())

    market_rows = rows[rows["kind"].eq("market")]
    output = []
    if not market_rows.empty:
        output.append(_aggregate_signal(market_rows, signal_date=target_date, scope="market", symbol="ALL", settings=settings))
    for symbol in universe_symbols:
        stock_rows = _rows_for_symbol(rows, symbol)
        if not stock_rows.empty:
            output.append(_aggregate_signal(stock_rows, signal_date=target_date, scope="stock", symbol=symbol, settings=settings))
    return pd.DataFrame(output, columns=_signal_columns())


def attach_news_signal_features(
    features: pd.DataFrame,
    news_signals: pd.DataFrame | None,
) -> pd.DataFrame:
    if features.empty:
        return features
    output = features.copy()
    output["date"] = pd.to_datetime(output["date"]).dt.date
    output["symbol"] = output["symbol"].astype(str).str.zfill(6)

    if news_signals is not None and not news_signals.empty:
        signals = news_signals.copy()
        signals["date"] = pd.to_datetime(signals["signal_date"], errors="coerce").dt.date
        signals["symbol"] = signals["symbol"].fillna("").astype(str)
        market = signals[(signals["scope"].astype(str) == "market") | (signals["symbol"] == "ALL")]
        stock = signals[signals["scope"].astype(str) == "stock"].copy()
        stock["symbol"] = stock["symbol"].astype(str).str.zfill(6)

        keep = ["date", *NEWS_TRAINING_FEATURE_COLUMNS]
        if not market.empty:
            market = market[[column for column in keep if column in market.columns]].drop_duplicates("date")
            output = output.merge(market, on="date", how="left", suffixes=("", "_market"))
        if not stock.empty:
            stock_keep = ["date", "symbol", *NEWS_TRAINING_FEATURE_COLUMNS]
            stock = stock[[column for column in stock_keep if column in stock.columns]].drop_duplicates(["date", "symbol"])
            output = output.merge(stock, on=["date", "symbol"], how="left", suffixes=("_market", "_stock"))
            for column in NEWS_TRAINING_FEATURE_COLUMNS:
                stock_column = f"{column}_stock"
                market_column = f"{column}_market"
                if stock_column in output.columns and market_column in output.columns:
                    output[column] = output[stock_column].combine_first(output[market_column])
                elif stock_column in output.columns:
                    output[column] = output[stock_column]
                elif market_column in output.columns:
                    output[column] = output[market_column]
        else:
            for column in NEWS_TRAINING_FEATURE_COLUMNS:
                market_column = f"{column}_market"
                if market_column in output.columns:
                    output[column] = output[market_column]

    for column, default in NEWS_SIGNAL_DEFAULTS.items():
        if column not in output.columns:
            output[column] = default
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(default)
    drop_columns = [
        column
        for column in output.columns
        if column.endswith("_market") or column.endswith("_stock")
    ]
    return output.drop(columns=drop_columns)


def _news_rows(conn, text_config: Mapping[str, Any], settings: Mapping[str, float]) -> pd.DataFrame:
    frames = [_stock_news_rows(conn, text_config, settings), _market_news_rows(conn, text_config, settings)]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _stock_news_rows(conn, text_config: Mapping[str, Any], settings: Mapping[str, float]) -> pd.DataFrame:
    if not table_exists(conn, "news_articles"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT article_id, query_date, symbol, title, description, pub_date,
               source_name, link, sentiment_score
        FROM news_articles
        ORDER BY query_date, symbol, pub_date
        """
    ).fetchdf()
    if frame.empty:
        return frame
    rows: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        symbol = str(row.get("symbol") or "").zfill(6)
        text = " ".join(part for part in [row.get("title"), row.get("description")] if part)
        news_date = _as_date(row.get("query_date")) or _as_date(row.get("pub_date"))
        if not symbol or not news_date:
            continue
        rows.append(
            _normalized_row(
                row,
                text=text,
                news_date=news_date,
                source=str(row.get("source_name") or "naver_search_api"),
                symbols=[symbol],
                category="stock",
                kind="stock",
                text_config=text_config,
                settings=settings,
            )
        )
    return pd.DataFrame(rows)


def _market_news_rows(conn, text_config: Mapping[str, Any], settings: Mapping[str, float]) -> pd.DataFrame:
    if not table_exists(conn, "market_news_feed"):
        return pd.DataFrame()
    frame = conn.execute(
        """
        SELECT article_id, source, category, title, summary, link, pub_date,
               tickers_json, themes_json, sentiment_score
        FROM market_news_feed
        ORDER BY pub_date, source
        """
    ).fetchdf()
    if frame.empty:
        return frame
    rows: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        text = " ".join(part for part in [row.get("title"), row.get("summary")] if part)
        news_date = _as_date(row.get("pub_date"))
        if not news_date:
            continue
        symbols = sorted(
            set(
                [
                    str(item).zfill(6)
                    for item in _loads(row.get("tickers_json"), [])
                    if _is_local_symbol(item)
                ]
                + _extract_local_symbols(text, text_config)
            )
        )
        rows.append(
            _normalized_row(
                row,
                text=text,
                news_date=news_date,
                source=str(row.get("source") or "market_news_rss"),
                symbols=symbols,
                category=str(row.get("category") or "macro"),
                kind="market",
                text_config=text_config,
                settings=settings,
            )
        )
    return pd.DataFrame(rows)


def _normalized_row(
    row: Mapping[str, Any],
    *,
    text: str,
    news_date: date,
    source: str,
    symbols: list[str],
    category: str,
    kind: str,
    text_config: Mapping[str, Any],
    settings: Mapping[str, float],
) -> dict[str, Any]:
    raw_sentiment = row.get("sentiment_score")
    sentiment = _safe_float(raw_sentiment)
    if sentiment is None:
        sentiment = normalize_sentiment(
            simple_sentiment(
                text,
                positive_words=text_config.get("positive_words"),
                negative_words=text_config.get("negative_words"),
            )
        )
    themes = set(_loads(row.get("themes_json"), []))
    themes.update(classify_themes(text, theme_keywords=text_config.get("theme_keywords")))
    risks = extract_risk_keywords(text, risk_keywords=text_config.get("risk_keywords"))
    negative_keywords = _negative_keyword_hits(text, text_config)
    is_negative = bool(negative_keywords) or sentiment <= float(settings["negative_sentiment_threshold"])
    is_positive = (not is_negative) and sentiment >= float(settings["positive_sentiment_threshold"])
    return {
        "article_id": row.get("article_id"),
        "kind": kind,
        "news_date": news_date,
        "symbols": sorted(set(symbols)),
        "category": category,
        "title": text_excerpt(row.get("title"), limit=160),
        "source": source,
        "link": row.get("link"),
        "sentiment": float(sentiment),
        "urgency": urgency_score(text, urgency_keywords=text_config.get("urgency_keywords")),
        "risk": 1.0 if risks or negative_keywords else 0.0,
        "negative": bool(is_negative),
        "positive": bool(is_positive),
        "negative_keywords": negative_keywords,
        "themes": sorted(str(theme).upper() for theme in themes),
    }


def _aggregate_signal(
    rows: pd.DataFrame,
    *,
    signal_date: date,
    scope: str,
    symbol: str,
    settings: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    weights = settings or DEFAULT_NEWS_SIGNAL_SETTINGS
    count_1d = int((rows["news_date"] == signal_date).sum())
    count_3d = int(len(rows))
    negative_count = int(rows["negative"].fillna(False).sum()) if "negative" in rows.columns else 0
    negative_ratio = float(negative_count / count_3d) if count_3d else 0.0
    x_rows = rows[rows["source"].astype(str).str.lower().eq(X_MARKET_NEWS_SOURCE)].copy()
    x_count_24h = int((x_rows["news_date"] == signal_date).sum()) if not x_rows.empty else 0
    x_count_3d = int(len(x_rows))
    x_negative_count = int(x_rows["negative"].fillna(False).sum()) if "negative" in x_rows.columns and not x_rows.empty else 0
    themes = _top_values(theme for values in rows["themes"] for theme in values)
    sources = sorted({str(source) for source in rows["source"].dropna().unique() if str(source).strip()})
    evidence = [
        {
            "date": str(row["news_date"]),
            "source": row.get("source"),
            "title": row.get("title"),
            "link": row.get("link"),
            "negative": bool(row.get("negative")),
            "negative_keywords": row.get("negative_keywords") or [],
        }
        for row in rows.sort_values("news_date", ascending=False).head(5).to_dict(orient="records")
    ]
    semiconductor_hits = _theme_ratio(rows, {"SEMICONDUCTOR", "AI"})
    macro_hits = _theme_ratio(rows, {"RATE", "ENERGY", "MACRO"}) + _category_ratio(rows, {"macro"})
    flow_hits = _theme_ratio(rows, {"FLOW", "PENSION_FLOW"}) + _category_ratio(rows, {"flow"})
    return {
        "signal_date": signal_date,
        "scope": scope,
        "symbol": symbol,
        "headline_count_1d": count_1d,
        "headline_count_3d": count_3d,
        "sentiment_avg_3d": _mean(rows["sentiment"], default=0.5),
        "urgency_avg_3d": _mean(rows["urgency"], default=0.0),
        "risk_avg_3d": _mean(rows["risk"], default=0.0),
        "news_headline_count_1d": float(count_1d),
        "news_headline_count_3d": float(count_3d),
        "news_attention_score": min(1.0, count_3d / 10.0),
        "news_sentiment_score": _mean(rows["sentiment"], default=0.5),
        "news_urgency_score": _mean(rows["urgency"], default=0.0),
        "news_risk_score": _mean(rows["risk"], default=0.0),
        "news_semiconductor_score": min(1.0, semiconductor_hits),
        "news_macro_score": min(1.0, macro_hits),
        "news_flow_score": min(1.0, flow_hits),
        "news_negative_count_3d": float(negative_count),
        "news_negative_ratio_3d": negative_ratio,
        "news_negative_attention_score": min(1.0, negative_count * float(weights["negative_weight"]) / 3.0),
        "news_bias_adjusted_sentiment_score": _weighted_sentiment(rows, weights),
        "news_source_diversity_score": min(1.0, len(sources) / 5.0),
        "x_news_count_24h": float(x_count_24h),
        "x_news_count_3d": float(x_count_3d),
        "x_news_negative_count_3d": float(x_negative_count),
        "x_news_negative_attention_score": min(1.0, x_negative_count * float(weights["negative_weight"]) / 3.0),
        "x_news_bias_adjusted_sentiment_score": _weighted_sentiment(x_rows, weights) if not x_rows.empty else 0.5,
        "themes_json": _json(themes),
        "evidence_json": _json(evidence),
        "created_at": _utcnow(),
    }


def _rows_for_symbol(rows: pd.DataFrame, symbol: str) -> pd.DataFrame:
    normalized = str(symbol).zfill(6)
    mask = rows["symbols"].map(lambda values: normalized in set(values or []))
    return rows[mask].copy()


def _resolve_signal_date(conn, signal_date: str | date) -> date:
    if isinstance(signal_date, date):
        return signal_date
    value = str(signal_date).strip().lower()
    if value != "latest":
        return datetime.fromisoformat(value).date()
    candidates: list[date] = []
    if table_exists(conn, "news_articles"):
        row = conn.execute("SELECT MAX(query_date) FROM news_articles").fetchone()
        parsed = _as_date(row[0] if row else None)
        if parsed:
            candidates.append(parsed)
    if table_exists(conn, "market_news_feed"):
        row = conn.execute("SELECT MAX(pub_date) FROM market_news_feed").fetchone()
        parsed = _as_date(row[0] if row else None)
        if parsed:
            candidates.append(parsed)
    return max(candidates) if candidates else datetime.now(KST).date()


def _resolve_symbols(conn, symbols: Sequence[str] | None) -> list[str]:
    if symbols:
        return sorted({str(symbol).zfill(6) for symbol in symbols})
    try:
        frame = conn.execute(
            """
            SELECT symbol
            FROM current_prediction_universe
            WHERE universe_rule = 'prediction_top_market_cap'
              AND is_enabled = TRUE
            ORDER BY market, prediction_rank, symbol
            """
        ).fetchdf()
        if not frame.empty:
            return sorted({str(symbol).zfill(6) for symbol in frame["symbol"]})
    except Exception:
        pass
    found: set[str] = set()
    if table_exists(conn, "news_articles"):
        found.update(str(row[0]).zfill(6) for row in conn.execute("SELECT DISTINCT symbol FROM news_articles WHERE symbol IS NOT NULL").fetchall())
    return sorted(found)


def _text_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    combined: dict[str, Any] = {}
    for section in ("market_news", "news", "telegram"):
        values = (config or {}).get(section, {})
        text_features = values.get("text_features") if isinstance(values, Mapping) else None
        if isinstance(text_features, Mapping):
            combined.update(text_features)
    direct = (config or {}).get("text_features")
    if isinstance(direct, Mapping):
        combined.update(direct)
    return combined


def _news_signal_settings(config: Mapping[str, Any] | None) -> dict[str, float]:
    values = dict(DEFAULT_NEWS_SIGNAL_SETTINGS)
    raw = (config or {}).get("news_signals", {})
    if isinstance(raw, Mapping):
        for key in values:
            if key in raw:
                try:
                    values[key] = float(raw[key])
                except (TypeError, ValueError):
                    pass
    values["negative_weight"] = max(0.1, values["negative_weight"])
    values["positive_weight"] = max(0.1, values["positive_weight"])
    return values


def _signal_columns() -> list[str]:
    return [
        "signal_date",
        "scope",
        "symbol",
        "headline_count_1d",
        "headline_count_3d",
        "sentiment_avg_3d",
        "urgency_avg_3d",
        "risk_avg_3d",
        *NEWS_TRAINING_FEATURE_COLUMNS,
        "themes_json",
        "evidence_json",
        "created_at",
    ]


def _theme_ratio(rows: pd.DataFrame, names: set[str]) -> float:
    if rows.empty:
        return 0.0
    hits = rows["themes"].map(lambda values: bool(set(values or []).intersection(names))).sum()
    return float(hits) / float(len(rows))


def _category_ratio(rows: pd.DataFrame, names: set[str]) -> float:
    if rows.empty:
        return 0.0
    hits = rows["category"].astype(str).str.lower().isin(names).sum()
    return float(hits) / float(len(rows))


def _mean(values: pd.Series, *, default: float) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if numeric.empty:
        return float(default)
    return float(numeric.mean())


def _weighted_sentiment(rows: pd.DataFrame, settings: Mapping[str, float]) -> float:
    if rows.empty or "sentiment" not in rows.columns:
        return 0.5
    sentiments = pd.to_numeric(rows["sentiment"], errors="coerce").fillna(0.5)
    negative = rows.get("negative", pd.Series(False, index=rows.index)).fillna(False).astype(bool)
    positive = rows.get("positive", pd.Series(False, index=rows.index)).fillna(False).astype(bool)
    weights = pd.Series(1.0, index=rows.index, dtype=float)
    weights.loc[positive] = float(settings["positive_weight"])
    weights.loc[negative] = float(settings["negative_weight"])
    denominator = float(weights.sum())
    if denominator <= 0:
        return 0.5
    return float((sentiments * weights).sum() / denominator)


def _negative_keyword_hits(text: str, text_config: Mapping[str, Any]) -> list[str]:
    values = []
    values.extend(DEFAULT_NEGATIVE_BUSINESS_KEYWORDS)
    values.extend(str(item) for item in text_config.get("negative_words") or [])
    values.extend(str(item) for item in text_config.get("negative_business_keywords") or [])
    seen: set[str] = set()
    hits: list[str] = []
    for keyword in values:
        normalized = str(keyword).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        if _contains_keyword(text, normalized):
            hits.append(normalized)
    return hits


def _extract_local_symbols(text: str, text_config: Mapping[str, Any]) -> list[str]:
    aliases = {**DEFAULT_LOCAL_TICKER_ALIASES, **dict(text_config.get("ticker_aliases") or {})}
    tickers = extract_tickers(text, ignore_words=text_config.get("ignore_words"), ticker_aliases=aliases)
    return sorted({str(ticker).zfill(6) for ticker in tickers if _is_local_symbol(ticker)})


def _top_values(values: Any, limit: int = 8) -> list[str]:
    counts: dict[str, int] = {}
    for value in values:
        text = str(value).strip()
        if text:
            counts[text] = counts.get(text, 0) + 1
    return [key for key, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str)


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if hasattr(parsed, "to_pydatetime"):
        return parsed.to_pydatetime().date()
    return None


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(parsed):
        return None
    return max(0.0, min(1.0, parsed))


def _is_local_symbol(value: Any) -> bool:
    text = str(value).strip()
    return text.isdigit() and len(text.zfill(6)) == 6


def _contains_keyword(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    if keyword.isascii():
        return keyword.lower() in str(text or "").lower()
    return keyword in str(text or "")


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

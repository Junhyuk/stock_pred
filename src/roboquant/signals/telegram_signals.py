from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.db import append_dedup_table, table_exists
from roboquant.signals.telegram_text import (
    classify_themes,
    duplicate_key,
    extract_risk_keywords,
    extract_tickers,
    extract_urls,
    normalize_sentiment,
    simple_sentiment,
    text_excerpt,
    urgency_score,
)

DISCLAIMER = (
    "본 리포트는 Telegram 공개 채널과 보유 데이터 기반 자동 분석 결과입니다. "
    "투자 권유가 아니며, Telegram 기반 정보에는 루머, 중복, 지연, 재가공 정보가 "
    "포함될 수 있으므로 공식 공시와 가격 데이터를 함께 확인해야 합니다."
)
TELEGRAM_MARKET_FEATURE_COLUMNS = [
    "telegram_attention_score",
    "telegram_sentiment_score",
    "telegram_urgency_score",
    "telegram_risk_score",
    "telegram_semiconductor_score",
    "telegram_macro_score",
]
TELEGRAM_TRAINING_FEATURE_COLUMNS = TELEGRAM_MARKET_FEATURE_COLUMNS.copy()
MACRO_THEME_KEYS = {"RATE", "ENERGY", "CRYPTO", "DEFENSE"}
KST = ZoneInfo("Asia/Seoul")


def normalize_telegram_message(
    *,
    channel: str,
    message_id: int,
    message_date: datetime | None,
    text: str | None,
    source_weight: float,
    config: Mapping[str, Any] | None = None,
    collected_at: datetime | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    text_config = dict((config or {}).get("text_features", {}))
    clean_text = str(text or "")
    date_utc = _to_utc_naive(message_date)
    collected = collected_at or _utcnow()
    tickers = extract_tickers(
        clean_text,
        ignore_words=text_config.get("ignore_words"),
        ticker_aliases=text_config.get("ticker_aliases"),
    )
    urls = extract_urls(clean_text)
    themes = classify_themes(clean_text, theme_keywords=text_config.get("theme_keywords"))
    risks = extract_risk_keywords(clean_text, risk_keywords=text_config.get("risk_keywords"))
    sentiment_raw = simple_sentiment(
        clean_text,
        positive_words=text_config.get("positive_words"),
        negative_words=text_config.get("negative_words"),
    )
    sentiment = normalize_sentiment(sentiment_raw)
    urgency = urgency_score(clean_text, urgency_keywords=text_config.get("urgency_keywords"))
    key = duplicate_key(clean_text, urls)
    telegram_url = f"https://t.me/{channel}/{int(message_id)}"

    post = {
        "channel": channel,
        "message_id": int(message_id),
        "date_utc": date_utc,
        "text": clean_text,
        "text_excerpt": text_excerpt(clean_text),
        "tickers_json": _json(tickers),
        "urls_json": _json(urls),
        "themes_json": _json(themes),
        "risk_keywords_json": _json(risks),
        "sentiment_raw": float(sentiment_raw),
        "sentiment_score": float(sentiment),
        "urgency_score": float(urgency),
        "source_weight": float(source_weight),
        "duplicate_key": key,
        "duplicate_score": 0.0,
        "telegram_url": telegram_url,
        "collected_at": collected,
        "raw_json": _json(
            {
                "channel": channel,
                "message_id": int(message_id),
                "date_utc": None if date_utc is None else date_utc.isoformat(),
                "urls": urls,
            }
        ),
    }
    mentions = [
        {
            "mention_id": _mention_id(channel, int(message_id), ticker),
            "ticker": ticker,
            "channel": channel,
            "message_id": int(message_id),
            "date_utc": date_utc,
            "themes_json": _json(themes),
            "risk_keywords_json": _json(risks),
            "sentiment_raw": float(sentiment_raw),
            "sentiment_score": float(sentiment),
            "urgency_score": float(urgency),
            "source_weight": float(source_weight),
            "duplicate_key": key,
            "duplicate_score": 0.0,
            "telegram_url": telegram_url,
            "created_at": collected,
        }
        for ticker in tickers
    ]
    return post, mentions


def upsert_telegram_frames(conn, posts: pd.DataFrame, mentions: pd.DataFrame) -> None:
    if not posts.empty:
        append_dedup_table(conn, "telegram_posts", posts, ["channel", "message_id"])
    if not mentions.empty:
        append_dedup_table(conn, "telegram_ticker_mentions", mentions, ["mention_id"])


def build_telegram_signal_daily(
    conn,
    *,
    asof: datetime | date | str | None = None,
    config: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    if not table_exists(conn, "telegram_ticker_mentions"):
        return _empty_signal_frame()

    asof_dt = _resolve_asof(asof)
    current_start = asof_dt - timedelta(hours=24)
    previous_start = asof_dt - timedelta(hours=48)
    mentions = conn.execute(
        """
        SELECT
          m.*,
          p.text_excerpt
        FROM telegram_ticker_mentions AS m
        LEFT JOIN telegram_posts AS p
          ON m.channel = p.channel
         AND m.message_id = p.message_id
        WHERE m.date_utc > ?
          AND m.date_utc <= ?
        ORDER BY m.date_utc DESC, m.channel, m.message_id
        """,
        [previous_start, asof_dt],
    ).fetchdf()
    if mentions.empty:
        return _empty_signal_frame()

    mentions["date_utc"] = pd.to_datetime(mentions["date_utc"])
    current = mentions[mentions["date_utc"] > pd.Timestamp(current_start)].copy()
    previous = mentions[mentions["date_utc"] <= pd.Timestamp(current_start)].copy()
    if current.empty:
        return _empty_signal_frame()

    one_hour_start = asof_dt - timedelta(hours=1)
    previous_counts = previous.groupby("ticker").size().to_dict() if not previous.empty else {}
    rows: list[dict[str, Any]] = []
    price_scores = _latest_price_momentum_scores(conn)
    ranking = dict((config or {}).get("ranking", {}))
    weights = {
        "mention_score": 0.25,
        "sentiment_score": 0.20,
        "source_weighted_score": 0.20,
        "price_momentum_score": 0.20,
        "urgency_score": 0.10,
        "risk_penalty": -0.15,
        "duplicate_score": -0.10,
        **dict(ranking.get("weights", {})),
    }
    mention_cap = max(1.0, float(ranking.get("mention_cap_24h", 10)))

    for ticker, group in current.groupby("ticker", sort=False):
        group = group.sort_values("date_utc", ascending=False).copy()
        count_24h = int(len(group))
        count_1h = int((group["date_utc"] > pd.Timestamp(one_hour_start)).sum())
        previous_count = int(previous_counts.get(ticker, 0))
        mention_delta = (
            float(count_24h - previous_count) / float(previous_count)
            if previous_count > 0
            else float(count_24h)
        )
        sentiment_avg = _mean(group["sentiment_score"], default=0.5)
        urgency_avg = _mean(group["urgency_score"], default=0.0)
        source_weighted_score = _source_weighted_score(group)
        duplicate = _duplicate_score(group)
        risks = _flatten_json_values(group["risk_keywords_json"])
        risk_penalty = min(1.0, len([item for item in risks if item]) / max(1, count_24h))
        price_momentum = float(price_scores.get(str(ticker), 0.5))
        mention_score = min(1.0, count_24h / mention_cap)
        attention_score = (
            0.45 * mention_score
            + 0.30 * source_weighted_score
            + 0.15 * urgency_avg
            + 0.10 * (1.0 - duplicate)
        )
        final_score = (
            weights["mention_score"] * mention_score
            + weights["sentiment_score"] * sentiment_avg
            + weights["source_weighted_score"] * source_weighted_score
            + weights["price_momentum_score"] * price_momentum
            + weights["urgency_score"] * urgency_avg
            + weights["risk_penalty"] * risk_penalty
            + weights["duplicate_score"] * duplicate
        )
        rows.append(
            {
                "signal_date": asof_dt.date(),
                "ticker": str(ticker).upper(),
                "mention_count_1h": count_1h,
                "mention_count_24h": count_24h,
                "mention_delta_24h": max(-1.0, min(10.0, mention_delta)),
                "sentiment_avg_24h": sentiment_avg,
                "urgency_avg_24h": urgency_avg,
                "source_weighted_score": source_weighted_score,
                "duplicate_score": duplicate,
                "risk_penalty": risk_penalty,
                "price_momentum_score": price_momentum,
                "telegram_attention_score": max(0.0, min(1.0, attention_score)),
                "final_signal_score": max(0.0, min(1.0, final_score)),
                "themes_json": _json(_top_values(_flatten_json_values(group["themes_json"]))),
                "risk_keywords_json": _json(_top_values(risks)),
                "evidence_json": _json(_evidence(group)),
                "created_at": _utcnow(),
            }
        )

    frame = pd.DataFrame(rows).sort_values(
        ["final_signal_score", "mention_count_24h", "ticker"],
        ascending=[False, False, True],
    )
    if not frame.empty:
        append_dedup_table(conn, "telegram_signal_daily", frame, ["signal_date", "ticker"])
    return frame.reset_index(drop=True)


def build_telegram_market_signal_daily(
    conn,
    *,
    asof: datetime | date | str | None = None,
    config: Mapping[str, Any] | None = None,
) -> pd.DataFrame:
    if not table_exists(conn, "telegram_posts"):
        return _empty_market_signal_frame()

    asof_dt = _resolve_asof(asof)
    current_start = asof_dt - timedelta(hours=24)
    one_hour_start = asof_dt - timedelta(hours=1)
    posts = conn.execute(
        """
        SELECT *
        FROM telegram_posts
        WHERE date_utc > ?
          AND date_utc <= ?
        ORDER BY date_utc DESC, channel, message_id
        """,
        [current_start, asof_dt],
    ).fetchdf()
    if posts.empty:
        return _empty_market_signal_frame()

    posts["date_utc"] = pd.to_datetime(posts["date_utc"])
    message_count_24h = int(len(posts))
    message_count_1h = int((posts["date_utc"] > pd.Timestamp(one_hour_start)).sum())
    ranking = dict((config or {}).get("ranking", {}))
    mention_cap = max(1.0, float(ranking.get("mention_cap_24h", 10)))
    sentiment = _mean(posts["sentiment_score"], default=0.5)
    urgency = _mean(posts["urgency_score"], default=0.0)
    source_weighted = _source_weighted_score(posts)
    duplicate = _duplicate_score(posts)
    risks = _flatten_json_values(posts["risk_keywords_json"])
    themes = _flatten_json_values(posts["themes_json"])
    risk_penalty = min(1.0, len([item for item in risks if item]) / max(1, message_count_24h))
    semiconductor_score = _theme_score(themes, {"SEMICONDUCTOR", "AI"}, message_count_24h)
    macro_score = _theme_score(themes, MACRO_THEME_KEYS, message_count_24h)
    mention_score = min(1.0, message_count_24h / mention_cap)
    attention = max(
        0.0,
        min(
            1.0,
            0.45 * mention_score
            + 0.25 * source_weighted
            + 0.20 * urgency
            + 0.10 * (1.0 - duplicate),
        ),
    )
    row = {
        "signal_date": _kst_date(asof_dt),
        "message_count_1h": message_count_1h,
        "message_count_24h": message_count_24h,
        "sentiment_avg_24h": sentiment,
        "urgency_avg_24h": urgency,
        "source_weighted_score": source_weighted,
        "duplicate_score": duplicate,
        "risk_penalty": risk_penalty,
        "telegram_attention_score": attention,
        "telegram_sentiment_score": sentiment,
        "telegram_urgency_score": urgency,
        "telegram_risk_score": risk_penalty,
        "telegram_semiconductor_score": semiconductor_score,
        "telegram_macro_score": macro_score,
        "themes_json": _json(_top_values(themes)),
        "risk_keywords_json": _json(_top_values(risks)),
        "evidence_json": _json(_market_evidence(posts)),
        "created_at": _utcnow(),
    }
    frame = pd.DataFrame([row])
    append_dedup_table(conn, "telegram_market_signal_daily", frame, ["signal_date"])
    return frame


def attach_telegram_market_features(
    features: pd.DataFrame,
    telegram_market: pd.DataFrame | None,
) -> pd.DataFrame:
    if features.empty:
        return features
    output = features.copy()
    if telegram_market is not None and not telegram_market.empty:
        items = telegram_market.copy()
        items["date"] = pd.to_datetime(items["signal_date"]).dt.date
        keep = ["date", *TELEGRAM_MARKET_FEATURE_COLUMNS]
        items = items[[column for column in keep if column in items.columns]]
        output["date"] = pd.to_datetime(output["date"]).dt.date
        output = output.merge(items, on="date", how="left")
    defaults = {
        "telegram_attention_score": 0.0,
        "telegram_sentiment_score": 0.5,
        "telegram_urgency_score": 0.0,
        "telegram_risk_score": 0.0,
        "telegram_semiconductor_score": 0.0,
        "telegram_macro_score": 0.0,
    }
    for column in TELEGRAM_MARKET_FEATURE_COLUMNS:
        if column not in output.columns:
            output[column] = defaults[column]
        output[column] = pd.to_numeric(output[column], errors="coerce").fillna(defaults[column])
    return output


def render_daily_report(signals: pd.DataFrame, *, asof: datetime | date | str | None = None, top_n: int = 10) -> str:
    asof_dt = _resolve_asof(asof)
    lines = [
        "# Telegram 관심 티커 리포트",
        "",
        f"- 기준시각(UTC): `{asof_dt.isoformat(sep=' ', timespec='minutes')}`",
        f"- 표시 개수: `{int(top_n)}`",
        "",
    ]
    if signals.empty:
        lines.extend(["수집된 Telegram 티커 신호가 없습니다.", "", "## 유의사항", DISCLAIMER, ""])
        return "\n".join(lines)

    frame = signals.sort_values("final_signal_score", ascending=False).head(int(top_n)).copy()
    for rank, (_, row) in enumerate(frame.iterrows(), start=1):
        evidence = _loads(row.get("evidence_json"), [])
        themes = ", ".join(_loads(row.get("themes_json"), [])) or "-"
        risks = ", ".join(_loads(row.get("risk_keywords_json"), [])) or "특이 키워드 없음"
        lines.extend(
            [
                f"## {rank}. {row['ticker']}",
                "",
                f"- 관심점수: `{_pct(row.get('final_signal_score'))}`",
                f"- 언급량 24h/1h: `{int(row.get('mention_count_24h') or 0)}` / `{int(row.get('mention_count_1h') or 0)}`",
                f"- 감성/긴급도: `{_pct(row.get('sentiment_avg_24h'))}` / `{_pct(row.get('urgency_avg_24h'))}`",
                f"- 테마: {themes}",
                f"- 리스크 키워드: {risks}",
                "- 근거 Telegram 글:",
            ]
        )
        if evidence:
            for item in evidence[:3]:
                lines.append(
                    f"  - {item.get('channel', '-')}: {item.get('text_excerpt', '')} ({item.get('telegram_url', '-')})"
                )
        else:
            lines.append("  - 근거 메시지 없음")
        lines.append("")

    lines.extend(["## 유의사항", DISCLAIMER, ""])
    return "\n".join(lines)


def _latest_price_momentum_scores(conn) -> dict[str, float]:
    if not table_exists(conn, "yahoo_prices_daily"):
        return {}
    prices = conn.execute(
        """
        SELECT symbol, date, close
        FROM yahoo_prices_daily
        WHERE asset_type IN ('stock', 'etf')
        ORDER BY symbol, date
        """
    ).fetchdf()
    if prices.empty:
        return {}
    prices["date"] = pd.to_datetime(prices["date"])
    prices["close"] = pd.to_numeric(prices["close"], errors="coerce")
    rows = []
    for symbol, group in prices.dropna(subset=["close"]).groupby("symbol"):
        group = group.sort_values("date")
        if len(group) < 2:
            continue
        latest = group.iloc[-1]["close"]
        base = group.iloc[max(0, len(group) - 6)]["close"]
        if not base:
            continue
        rows.append({"symbol": str(symbol).upper(), "return_5d": float(latest / base - 1.0)})
    if not rows:
        return {}
    frame = pd.DataFrame(rows)
    frame["score"] = frame["return_5d"].rank(pct=True).fillna(0.5)
    return dict(zip(frame["symbol"], frame["score"], strict=False))


def _source_weighted_score(group: pd.DataFrame) -> float:
    weights = pd.to_numeric(group["source_weight"], errors="coerce").fillna(0.5).clip(0.0, 2.0)
    sentiment = pd.to_numeric(group["sentiment_score"], errors="coerce").fillna(0.5).clip(0.0, 1.0)
    urgency = pd.to_numeric(group["urgency_score"], errors="coerce").fillna(0.0).clip(0.0, 1.0)
    base = (0.65 * sentiment) + (0.35 * urgency)
    denominator = float(weights.sum())
    if denominator <= 0:
        return 0.5
    return float((base * weights).sum() / denominator)


def _duplicate_score(group: pd.DataFrame) -> float:
    keys = [str(value) for value in group["duplicate_key"].dropna().tolist() if str(value)]
    if not keys:
        return 0.0
    return max(0.0, min(1.0, 1.0 - (len(set(keys)) / len(keys))))


def _evidence(group: pd.DataFrame) -> list[dict[str, Any]]:
    ranked = group.sort_values(
        ["source_weight", "urgency_score", "sentiment_score", "date_utc"],
        ascending=[False, False, False, False],
    )
    seen: set[tuple[str, int]] = set()
    items = []
    for _, row in ranked.iterrows():
        key = (str(row.get("channel")), int(row.get("message_id")))
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "channel": row.get("channel"),
                "message_id": int(row.get("message_id")),
                "date_utc": None
                if pd.isna(row.get("date_utc"))
                else pd.to_datetime(row.get("date_utc")).isoformat(),
                "telegram_url": row.get("telegram_url"),
                "text_excerpt": row.get("text_excerpt") or "",
            }
        )
        if len(items) >= 3:
            break
    return items


def _market_evidence(group: pd.DataFrame) -> list[dict[str, Any]]:
    ranked = group.sort_values(
        ["source_weight", "urgency_score", "sentiment_score", "date_utc"],
        ascending=[False, False, False, False],
    )
    items = []
    for _, row in ranked.iterrows():
        items.append(
            {
                "channel": row.get("channel"),
                "message_id": int(row.get("message_id")),
                "date_utc": None
                if pd.isna(row.get("date_utc"))
                else pd.to_datetime(row.get("date_utc")).isoformat(),
                "telegram_url": row.get("telegram_url"),
                "text_excerpt": row.get("text_excerpt") or "",
                "sentiment_score": None
                if pd.isna(row.get("sentiment_score"))
                else float(row.get("sentiment_score")),
                "urgency_score": None
                if pd.isna(row.get("urgency_score"))
                else float(row.get("urgency_score")),
            }
        )
        if len(items) >= 5:
            break
    return items


def _flatten_json_values(series: Iterable[object]) -> list[str]:
    values: list[str] = []
    for raw in series:
        loaded = _loads(raw, [])
        if isinstance(loaded, list):
            values.extend(str(item) for item in loaded if item)
    return values


def _top_values(values: list[str], limit: int = 8) -> list[str]:
    counts = Counter(values)
    return [item for item, _ in counts.most_common(limit)]


def _mean(series: pd.Series, default: float) -> float:
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return float(default)
    return float(numeric.mean())


def _theme_score(themes: list[str], target: set[str], denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    hits = sum(1 for theme in themes if str(theme).upper() in target)
    return max(0.0, min(1.0, hits / float(denominator)))


def _mention_id(channel: str, message_id: int, ticker: str) -> str:
    raw = f"{channel}|{int(message_id)}|{ticker.upper()}"
    return sha256(raw.encode("utf-8")).hexdigest()


def _resolve_asof(value: datetime | date | str | None) -> datetime:
    if value is None or str(value).strip().lower() == "latest":
        return _utcnow()
    if isinstance(value, datetime):
        return _to_utc_naive(value) or _utcnow()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, 23, 59, 59)
    return _to_utc_naive(datetime.fromisoformat(str(value))) or _utcnow()


def _to_utc_naive(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _kst_date(value: datetime) -> date:
    stamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return stamp.astimezone(KST).date()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _loads(value: object, default):
    if value is None:
        return default
    if isinstance(value, float) and pd.isna(value):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _pct(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) * 100:.0f}%"


def _empty_signal_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "signal_date",
            "ticker",
            "mention_count_1h",
            "mention_count_24h",
            "mention_delta_24h",
            "sentiment_avg_24h",
            "urgency_avg_24h",
            "source_weighted_score",
            "duplicate_score",
            "risk_penalty",
            "price_momentum_score",
            "telegram_attention_score",
            "final_signal_score",
            "themes_json",
            "risk_keywords_json",
            "evidence_json",
            "created_at",
        ]
    )


def _empty_market_signal_frame() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "signal_date",
            "message_count_1h",
            "message_count_24h",
            "sentiment_avg_24h",
            "urgency_avg_24h",
            "source_weighted_score",
            "duplicate_score",
            "risk_penalty",
            "telegram_attention_score",
            "telegram_sentiment_score",
            "telegram_urgency_score",
            "telegram_risk_score",
            "telegram_semiconductor_score",
            "telegram_macro_score",
            "themes_json",
            "risk_keywords_json",
            "evidence_json",
            "created_at",
        ]
    )

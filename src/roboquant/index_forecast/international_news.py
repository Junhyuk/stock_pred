"""Metadata-only news sensor and a development-selected, independently gated expert.

Source attention is an editorial prior, never a return weight. Unknown coverage
is missing data. All fitting and calibration use earlier, actually mature labels.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import feedparser
import numpy as np
import pandas as pd

from roboquant.index_forecast.reliable import (
    ReliableSettings,
    _bootstrap,
    _calibrate,
    _fit_base,
    _gate,
    _predict,
    _radius,
    _score,
    _simple_metrics,
)

SCHEMA_VERSION = "international_news_candidate_v1"
ALPHA_GRID = (0.0, .05, .10, .20)
TOPIC_RULES = {
    "escalation": ("attack", "strike", "missile", "invasion", "bomb", "공격", "공습", "미사일"),
    "deescalation": ("ceasefire", "peace talk", "negotiation", "de-escalat", "휴전", "협상", "종전"),
    "shipping_energy": ("hormuz", "tanker", "shipping", "oil", "energy", "lng", "호르무즈", "유조선", "원유"),
    "trade_sanctions": ("tariff", "sanction", "export control", "trade restriction", "관세", "제재", "수출통제"),
    "monetary_policy": ("interest rate", "monetary policy", "policy rate", "rate decision", "rate cut", "rate hike",
                        "discount rate", "federal funds", "federal open market committee", "fomc", "policy minutes",
                        "inflation target", "통화정책", "기준금리", "금리결정", "금리인하", "금리인상", "금통위"),
    "uncertainty": ("uncertain", "unconfirmed", "unverified", "alleged", "reportedly", "may ", "might ", "claim", "미확인", "불확실", "주장"),
}
NEWS_FEATURES = ["unique_clusters_24h", "source_diversity_24h", "confirmed_escalation_24h",
                 "reported_escalation_24h", "reported_deescalation_24h", "shipping_energy_24h",
                 "trade_sanctions_24h", "monetary_policy_24h", "uncertainty_24h",
                 "unconfirmed_24h", "novelty_ratio_7d"]
SOURCE_PRIORS = {"official": 1.0, "wire": .8, "aggregator": .5, "unknown": .5}


@dataclass(frozen=True)
class NewsSettings:
    initial_train_days: int = 60
    min_calibration_rows: int = 60
    calibration_window: int = 252
    retrain_step_days: int = 21
    bootstrap_samples: int = 2000
    bootstrap_block_days: int = 20
    random_state: int = 42

    def __post_init__(self):
        if any(value < 1 for key, value in asdict(self).items() if key != "random_state"):
            raise ValueError("news sample/window settings must be positive")


def _stamp(value: Any) -> pd.Timestamp:
    """Accept only explicit intraday, timezone-aware clocks; never invent a zone."""
    if value is None or (isinstance(value, str) and len(value.strip()) <= 10):
        return pd.NaT
    try:
        stamp = pd.Timestamp(value)
        return pd.NaT if pd.isna(stamp) or stamp.tzinfo is None else stamp.tz_convert("UTC")
    except (ValueError, TypeError):
        return pd.NaT


def _iso(value: Any) -> str | None:
    stamp = _stamp(value)
    return None if pd.isna(stamp) else stamp.isoformat()


def _url(value: Any) -> str:
    parts = urlsplit(str(value or ""))
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), urlencode(query), ""))


def normalize_news_event(item: dict, *, first_seen_at=None, source=None, source_kind=None) -> dict:
    """Store immutable metadata versions, never a feed description/article body."""
    title = re.sub(r"<[^>]+>", "", str(item.get("title", ""))).strip()[:500]
    link = _url(item.get("link") or item.get("source_url") or item.get("url"))
    seen = _stamp(item.get("first_seen_at") or first_seen_at)
    version_seen = _stamp(item.get("version_first_seen_at") or item.get("version_received_at"))
    if not pd.isna(version_seen):
        seen = max(t for t in (seen, version_seen) if not pd.isna(t))
    original_pub = item.get("published_at") or item.get("published") or item.get("pub_date")
    published, updated = _stamp(original_pub), _stamp(item.get("updated_at") or item.get("updated"))
    available = max((t for t in (seen, published, updated) if not pd.isna(t)), default=pd.NaT)
    publication_date = item.get("published_date") or (str(original_pub)[:10] if original_pub else None)
    # A separate receipt is mandatory even if a publication/update clock exists.
    if pd.isna(seen):
        available = pd.NaT
    kind = source_kind or item.get("source_kind") or "unknown"
    source_name = str(source or item.get("source") or "unknown")
    if kind == "unknown" and (source_name.lower() == "ap" or any(x in source_name.lower() for x in ("reuters", "ap news", "associated press"))):
        kind = "wire"
    status = str(item.get("claim_status") or "not_independently_checked").lower()
    # The legacy default described missing assessment, not an allegation. A
    # routine RSS headline must not become an unconfirmed incident by default.
    if status == "reported_unverified":
        status = "not_independently_checked"
    claims = item.get("claims", [])
    claim_text = " ".join(str(c.get("verification_status", "")) for c in claims if isinstance(c, dict)).lower()
    # Negative verification tokens take precedence over words such as 'verified'.
    if any(word in (claim_text + " " + str(item.get("verification_status", "")).lower()) for word in ("unverified", "unconfirmed", "anonymous", "attributed")):
        status = "unconfirmed"
    if status not in {"confirmed_event", "official_statement", "unconfirmed", "not_independently_checked"}:
        status = "not_independently_checked"
    topics = [topic for topic, words in TOPIC_RULES.items() if any(word in title.lower() for word in words)]
    cluster_title = re.sub(r"\s*[-|–]\s*(reuters|associated press|ap news)\s*$", "", title.lower())
    cluster_title = re.sub(r"[^\w]+", " ", cluster_title).strip()
    story_id = hashlib.sha256(link.encode()).hexdigest()
    version_id = hashlib.sha256(json.dumps([story_id, title, _iso(published), _iso(updated), _iso(seen)], ensure_ascii=False).encode()).hexdigest()
    return {"event_id": version_id, "story_id": story_id, "title": title, "source_url": link,
            "source": source_name, "source_kind": kind, "published_at": _iso(published),
            "published_date": publication_date, "updated_at": _iso(updated),
            "first_seen_at": _iso(seen), "available_at": _iso(available),
            "version_first_seen_at": _iso(version_seen),
            "publication_time_basis": "publisher_clock" if not pd.isna(published) else "unknown_actual_receipt_only",
            "provider_seen_at": item.get("provider_seen_at"), "publisher_source": item.get("publisher_source") or item.get("source"),
            "publisher_timestamp_raw": item.get("publisher_timestamp_raw"), "claim_status": status,
            "claim_status_rule": "explicit_unconfirmed_only; unspecified metadata is neutral",
            "verification_status": item.get("verification_status"), "claims": claims,
            "topics": topics, "cluster_key": cluster_title}


def _coverage(coverage: dict, cutoff: pd.Timestamp) -> dict:
    required = list(coverage.get("required_sources", []))
    start = cutoff - timedelta(days=7)
    covered, missing = [], []
    for source in required:
        segments = []
        for window in coverage.get("windows", []):
            recorded, low, high = (_stamp(window.get(key)) for key in ("recorded_at", "start_at", "end_at"))
            if (window.get("source") == source and window.get("complete") is True and window.get("status") == "ok"
                    and not any(pd.isna(t) for t in (recorded, low, high)) and high <= recorded <= cutoff and high >= low):
                segments.append((low, min(high, cutoff)))
        cursor = start
        for low, high in sorted(segments):
            if low > cursor:
                break
            cursor = max(cursor, high)
        (covered if cursor >= cutoff else missing).append(source)
    return {"ready": bool(required) and not missing, "required_sources": required,
            "covered_sources": covered, "missing_sources": missing, "lookback_days": 7,
            "status": "complete" if required and not missing else "unknown_or_incomplete",
            "missing_is_not_zero": True}


def _content_period(item: dict, cutoff: pd.Timestamp) -> tuple[str | None, float, str, str]:
    """A publication date is an age bucket, never a synthesized publication clock."""
    published = _stamp(item.get("published_at"))
    seen = _stamp(item.get("first_seen_at"))
    if not pd.isna(published):
        age = (cutoff - published).total_seconds() / 3600
        period = None if age < 0 or age > 168 else "current" if age <= 24 else "prior"
        return period, max(0., age), "publisher_clock", str(published.date())
    try:
        pub_day = date.fromisoformat(str(item.get("published_date")))
    except ValueError:
        pub_day = None
    age = max(0., (cutoff - seen).total_seconds() / 3600)
    if pub_day is not None:
        days = (cutoff.date() - pub_day).days
        # A newly received old statement remains old. The bound is deliberately
        # conservative because the publication's time of day is unknown.
        age = max(age, float(max(0, days + 1) * 24))
        period = None if days < 0 or days >= 7 else "date_only_recent" if days <= 1 else "prior"
        return period, age, "publication_date_bucket_time_unknown", str(pub_day)
    # Receipt proves availability, not that an undated statement is fresh.
    period = None if age > 168 else "unknown_age"
    return period, age, "publication_age_unknown_attention_zero", str(seen.date())


def build_news_features(events: list[dict] | dict, coverage: dict | None, prediction_cutoff) -> dict:
    cutoff = _stamp(prediction_cutoff)
    payload = events.get("events", events.get("observed", [])) if isinstance(events, dict) else events
    normalized = [normalize_news_event(e) for e in (payload or [])]
    return _normalized_features(normalized, coverage or {}, cutoff)


def _normalized_features(normalized: list[dict], coverage: dict, cutoff: pd.Timestamp) -> dict:
    eligible = [e for e in normalized if e["title"] and e["source_url"] and
                not pd.isna(_stamp(e["available_at"])) and _stamp(e["available_at"]) <= cutoff]
    versions = {}
    for item in sorted(eligible, key=lambda e: e["available_at"]):
        versions[item["story_id"]] = item
    unique = {}
    for item in sorted(versions.values(), key=lambda e: e["available_at"]):
        period, age, basis, content_day = _content_period(item, cutoff)
        if period:
            unique.setdefault((item["cluster_key"], content_day), {**item, "age_hours": age,
                "age_basis": basis, "_period": period})
    prior, current, date_only_recent, unknown_age = [], [], [], []
    for item in unique.values():
        period = item.pop("_period")
        (current if period == "current" else date_only_recent if period == "date_only_recent"
         else unknown_age if period == "unknown_age" else prior).append(item)
    def count(topic):
        return sum(topic in e["topics"] for e in current)
    observed = dict(zip(NEWS_FEATURES, [len(current), len({e["source"] for e in current}),
        sum("escalation" in e["topics"] and e["claim_status"] == "confirmed_event" for e in current),
        count("escalation"), count("deescalation"), count("shipping_energy"), count("trade_sanctions"),
        count("monetary_policy"), count("uncertainty"),
        sum(e["claim_status"] == "unconfirmed" for e in current),
        len(current) / (1 + len(prior) / 6)], strict=True))
    quality = _coverage(coverage, cutoff)
    if unknown_age:
        quality.update(ready=False, status="unknown_publication_age", unknown_age_count=len(unknown_age))
    elif date_only_recent:
        quality.update(ready=False, status="date_only_publication_clock", date_only_count=len(date_only_recent))
    attention = []
    for item in [*current, *date_only_recent]:
        age = item["age_hours"]
        prior_weight = SOURCE_PRIORS.get(item["source_kind"], .5)
        claim_weight = .5 if item["claim_status"] == "unconfirmed" else 1.
        attention.append({"event_id": item["event_id"], "source_prior": prior_weight,
                          "age_hours": age, "age_basis": item["age_basis"], "claim_multiplier": claim_weight,
                          "weight": prior_weight * .5 ** (age / 24) * claim_weight,
                          "meaning": "risk_attention_not_return_weight"})
    attention.extend({"event_id": e["event_id"], "source_prior": SOURCE_PRIORS.get(e["source_kind"], .5),
                      "age_hours": None, "age_basis": e["age_basis"], "claim_multiplier": .5 if e["claim_status"] == "unconfirmed" else 1.,
                      "weight": 0., "meaning": "risk_attention_not_return_weight"} for e in unknown_age)
    return {"feature_vector": observed if quality["ready"] else dict.fromkeys(NEWS_FEATURES),
            "observed_feature_vector": observed, "coverage": quality, "events": [*current, *date_only_recent],
            "date_only_context": date_only_recent, "unknown_age_context": unknown_age,
            "attention_weights": attention, "feature_rule_version": SCHEMA_VERSION}


def collect_news_metadata(config: dict, *, retrieved_at=None, opener=None) -> dict:
    """Bounded public RSS/GDELT ArtList reads; caller persists immutable receipts.

    An RSS head or bounded provider search does not certify a complete interval.
    Failures and successful-but-incomplete captures are recorded independently.
    """
    receipt = _stamp(retrieved_at or datetime.now(UTC))
    if pd.isna(receipt):
        raise ValueError("retrieved_at requires an explicit timezone")
    limit = min(100, max(1, int(config.get("max_entries_per_feed", 50))))
    byte_limit = min(2097152, int(config.get("max_response_bytes", 2097152)))
    def fetch(url):
        if opener:
            return opener(url)
        request = Request(url, headers={"User-Agent": "roboquant-news-metadata/1.0"})
        with urlopen(request, timeout=float(config.get("request_timeout_seconds", 15))) as response:
            raw = response.read(byte_limit + 1)
        if len(raw) > byte_limit:
            raise ValueError("metadata response exceeds configured byte limit")
        return raw
    result = {"schema_version": "international_news_collection_v1", "retrieved_at": receipt.isoformat(),
              "events": [], "coverage": {"required_sources": config.get("required_sources", []), "windows": []}}
    requests = [(dict(feed), "rss") for feed in config.get("feeds", []) if feed.get("enabled", True) and feed.get("url")]
    gdelt = config.get("gdelt", {})
    if gdelt.get("enabled"):
        for query in gdelt.get("queries", [])[:4]:
            params = {"query": query["query"], "mode": "ArtList", "format": "json",
                      "maxrecords": min(75, int(gdelt.get("max_records", 25))),
                      "timespan": gdelt.get("timespan", "1d"), "sort": "DateDesc"}
            requests.append(({**query, "url": gdelt["endpoint"] + "?" + urlencode(params), "kind": "aggregator"}, "gdelt"))
    for position, (source, kind) in enumerate(requests):
        if position and opener is None:
            time.sleep(1)
        window = {"source": source["source"], "recorded_at": receipt.isoformat(), "start_at": None,
                  "end_at": receipt.isoformat(), "complete": False, "status": "failed",
                  "reason": "bounded_snapshot_not_continuous_coverage"}
        try:
            raw = fetch(source["url"])
            # A run-start clock cannot stand in for a response's actual receipt.
            response_receipt = receipt if retrieved_at is not None else pd.Timestamp(datetime.now(UTC))
            window.update(recorded_at=response_receipt.isoformat(), end_at=response_receipt.isoformat())
            if kind == "rss":
                parsed = feedparser.parse(raw)
                if parsed.bozo and not parsed.entries:
                    raise ValueError("invalid RSS metadata")
                entries = []
                for entry in parsed.entries[:limit]:
                    published, published_date = None, None
                    raw_stamp = str(entry.get("published") or "")
                    has_clock = bool(re.search(r"[T\s]\d{1,2}:\d{2}", raw_stamp))
                    has_zone = bool(re.search(r"(?:Z|[+-]\d{2}:?\d{2}|GMT|UTC|UT|[ECMP][SD]T)\s*$", raw_stamp, re.IGNORECASE))
                    if entry.get("published_parsed") and has_clock and has_zone:
                        published = datetime(*entry.published_parsed[:6], tzinfo=UTC).isoformat()
                    elif entry.get("published_parsed"):
                        published_date = date(*entry.published_parsed[:3]).isoformat()
                    entries.append({"title": entry.get("title"), "link": entry.get("link"), "published_at": published,
                                    "published_date": published_date, "publisher_timestamp_raw": raw_stamp[:120]})
            else:
                articles = json.loads(raw).get("articles", [])
                entries = [{"title": a.get("title"), "link": a.get("url"), "published_at": None,
                            "provider_seen_at": a.get("seendate"), "source": a.get("domain")} for a in articles]
            for entry in entries:
                item = normalize_news_event(entry, first_seen_at=response_receipt, source=source["source"], source_kind=source.get("kind", "unknown"))
                if item["title"] and item["source_url"]:
                    result["events"].append(item)
            window.update(status="ok", retrieved_count=len(entries))
        except Exception as exc:
            # Exception class alone avoids echoing URLs, query data or credentials.
            window["error_type"] = type(exc).__name__
            if retrieved_at is None:
                window["recorded_at"] = datetime.now(UTC).isoformat()
        result["coverage"]["windows"].append(window)
    if retrieved_at is None:
        result["retrieved_at"] = datetime.now(UTC).isoformat()
    return result


def _prior(records: list[dict], cutoff, window: int) -> list[dict]:
    return [r for r in records if _stamp(r.get("prediction_cutoff")) < cutoff and
            _stamp(r.get("label_available_at")) <= cutoff and _stamp(r.get("target_close_at")) <= cutoff][-window:]


def _incremental(records: list[dict], config: ReliableSettings) -> dict:
    usable = [r for r in records if r.get("calibration_ready")]
    if not usable:
        return {"n": 0, "brier_difference_ci95": {}, "base": {}}
    y = np.asarray([r["actual_return"] for r in usable])
    p = np.asarray([r["p_up"] for r in usable])
    bp = np.asarray([r["base_p_up"] for r in usable])
    base = _simple_metrics(y, np.asarray([r["base_expected_return"] for r in usable]), bp)
    mixed = _simple_metrics(y, np.asarray([r["expected_return"] for r in usable]), p)
    return {**mixed, "base": base, "brier_difference_ci95": _bootstrap((p - (y > 0)) ** 2 - (bp - (y > 0)) ** 2, config)}


def _incremental_gate(metrics: dict, minimum: int) -> list[str]:
    reasons = []
    if metrics["n"] < minimum:
        reasons.append("insufficient_common_date_oos")
    if metrics.get("brier_difference_ci95", {}).get("high", math.inf) >= 0:
        reasons.append("no_incremental_brier_confidence_gain")
    if metrics.get("mae", math.inf) > metrics.get("base", {}).get("mae", -math.inf):
        reasons.append("mae_worse_than_frozen_base")
    if metrics.get("log_loss", math.inf) > metrics.get("base", {}).get("log_loss", -math.inf):
        reasons.append("log_loss_worse_than_frozen_base")
    return reasons


def _baseline_contract(row: dict) -> bool:
    baselines, references = row.get("baselines", {}), row.get("references", {})
    for name in ("legacy", "mean", "up_rate"):
        if name not in baselines or baselines[name].get("p_up") is None:
            return False
        if name != "up_rate" and any(baselines[name].get(key) is None for key in ("expected_return", "range_low", "range_high")):
            return False
    return all(name in references and all(references[name].get(k) is not None for k in ("expected_return", "p_up"))
               for name in ("previous_direction", "simple_trend"))


def build_international_news_report(events, coverage, base_oof_records, base_forecast, market,
                                    prediction_cutoff, target_close_at, *, minimum_oos=252,
                                    settings: NewsSettings | None = None,
                                    evaluation_protocol: dict | None = None) -> dict:
    """Return an exact alpha-zero base, or a separately calibrated gated mixture.

    Explicit development/terminal split tags come from the base evaluation. No
    boundary, feature, alpha grid or family is chosen by terminal outcomes.
    """
    cfg = settings or NewsSettings()
    config = ReliableSettings(initial_train_days=cfg.initial_train_days,
        min_calibration_rows=cfg.min_calibration_rows, calibration_window=cfg.calibration_window,
        retrain_step_days=cfg.retrain_step_days, bootstrap_samples=cfg.bootstrap_samples,
        bootstrap_block_days=cfg.bootstrap_block_days, random_state=cfg.random_state,
        min_oos_rows=minimum_oos, model_families=("linear",))
    cutoff, target = _stamp(prediction_cutoff), _stamp(target_close_at)
    payload = events.get("events", events.get("observed", [])) if isinstance(events, dict) else events
    normalized = [normalize_news_event(e) for e in (payload or [])]
    current = _normalized_features(normalized, coverage or {}, cutoff)
    report = {"schema_version": SCHEMA_VERSION, "market": market, "prediction_cutoff": _iso(cutoff),
              "target_close_at": _iso(target), "current_features": current, "news_model_ready": False,
              "selected_alpha": 0., "alpha": 0., "production_weight": 0., "gate_reasons": [],
              "blended_forecast": deepcopy(base_forecast), "validation_report": {
                  "alpha_grid": list(ALPHA_GRID), "alpha_cap": .20, "feature_names": NEWS_FEATURES,
                  "settings": asdict(cfg), "own_label_residual_used": False,
                  "selection": "earlier_development_only_terminal_can_veto", "minimum_terminal_oos": minimum_oos}}
    reasons = report["gate_reasons"]
    if market not in {"KOSPI", "KOSDAQ", "US_SPX", "US_NASDAQ"}:
        reasons.append("unsupported_market")
    if pd.isna(cutoff) or pd.isna(target) or target <= cutoff:
        reasons.append("invalid_market_prediction_or_target_clock")
    if not current["coverage"]["ready"]:
        reasons.append("current_feed_coverage_unknown_or_incomplete")
    if reasons:
        reasons.append("news_history_not_ready_alpha_zero")
        return report
    rows = []
    protocol = evaluation_protocol or {}
    seen = set()
    for original in sorted(base_oof_records, key=lambda r: str(r.get("prediction_cutoff", ""))):
        row = deepcopy(original)
        row_cutoff = _stamp(row.get("prediction_cutoff"))
        maturity, close = _stamp(row.get("label_available_at")), _stamp(row.get("target_close_at"))
        if (any(pd.isna(t) for t in (row_cutoff, maturity, close)) or row_cutoff > cutoff or close <= row_cutoff
                or row.get("market", market) != market or row_cutoff.isoformat() in seen):
            continue
        if not all(isinstance(row.get(k), (float, int)) and math.isfinite(row[k]) for k in ("actual_return", "expected_return", "p_up")):
            continue
        if not 0 <= row["p_up"] <= 1:
            continue
        features = _normalized_features(normalized, coverage or {}, row_cutoff)
        if not features["coverage"]["ready"]:
            continue
        split = row.get("split")
        if split is None and protocol.get("selection_frozen_before") and protocol.get("development_start"):
            split = ("terminal" if row["asof_date"] >= protocol["selection_frozen_before"] else
                     "development" if row["asof_date"] >= protocol["development_start"] else "calibration_history")
        row.update(features["feature_vector"], split=split, _cutoff=row_cutoff)
        rows.append(row)
        seen.add(row_cutoff.isoformat())
    report["validation_report"]["coverage_complete_base_rows"] = len(rows)
    if len(rows) < cfg.initial_train_days + 2 * cfg.min_calibration_rows + minimum_oos:
        reasons.append("insufficient_point_in_time_news_history")
        return report
    # Refitting is periodic; training preprocessing sees only prior mature rows.
    news_records, mixtures = [], {alpha: [] for alpha in ALPHA_GRID if alpha}
    fitted, last_fit = None, -cfg.retrain_step_days
    for i, row in enumerate(rows):
        mature_training = _prior(rows[:i], row["_cutoff"], len(rows))
        if len(mature_training) < cfg.initial_train_days:
            continue
        if fitted is None or i - last_fit >= cfg.retrain_step_days:
            train = pd.DataFrame(mature_training).rename(columns={"actual_return": "label_return"})
            fitted = _fit_base(train, NEWS_FEATURES, "linear", config)
            last_fit = i
        expected, raw = _predict(fitted, np.asarray([[row[f] for f in NEWS_FEATURES]], dtype=float))
        previous = _prior(news_records, row["_cutoff"], cfg.calibration_window)
        p_up, calibrated = _calibrate(previous, raw, config)
        radius = _radius(previous, config)
        news = {**row, "expected_return": expected, "raw_p_up": raw, "p_up": p_up,
                "range_low": None if radius is None else expected - radius,
                "range_high": None if radius is None else expected + radius,
                "calibration_ready": calibrated and radius is not None}
        news_records.append(news)
        for alpha, history in mixtures.items():
            mixed_return = (1 - alpha) * row["expected_return"] + alpha * expected
            mixed_raw = (1 - alpha) * row["p_up"] + alpha * p_up
            prior_mix = _prior(history, row["_cutoff"], cfg.calibration_window)
            mixed_p, mixed_calibrated = _calibrate(prior_mix, mixed_raw, config)
            mixed_radius = _radius(prior_mix, config)
            history.append({**row, "base_p_up": row["p_up"], "base_expected_return": row["expected_return"],
                "expected_return": mixed_return, "raw_p_up": mixed_raw, "p_up": mixed_p,
                "range_low": None if mixed_radius is None else mixed_return - mixed_radius,
                "range_high": None if mixed_radius is None else mixed_return + mixed_radius,
                "calibration_ready": bool(row.get("calibration_ready")) and news["calibration_ready"] and mixed_calibrated and mixed_radius is not None,
                "probability_method": "sigmoid_own_mixed_mature_oof"})
    mature_news = _prior(news_records, cutoff, cfg.calibration_window)
    report["news_model_ready"] = len(mature_news) >= cfg.min_calibration_rows
    development = {}
    for alpha, history in mixtures.items():
        dev = [r for r in history if r["split"] == "development"]
        terminal = [r for r in history if r["split"] == "terminal"]
        # Enforce chronological split and maturity before the first terminal cutoff.
        boundary = min((r["_cutoff"] for r in terminal), default=cutoff)
        dev = _prior(dev, boundary, len(dev))
        development[alpha] = _incremental(dev, config)
    report["validation_report"]["development_mixtures"] = {str(a): m for a, m in development.items()}
    minimum_dev = max(cfg.min_calibration_rows, minimum_oos)
    eligible = [(alpha, metrics) for alpha, metrics in development.items() if not _incremental_gate(metrics, minimum_dev)]
    if not eligible:
        reasons.append("development_no_incremental_news_gain_or_insufficient_samples")
        return report
    selected, _ = min(eligible, key=lambda pair: (pair[1]["brier"], pair[1]["log_loss"], pair[1]["mae"], pair[0]))
    report["selected_alpha"] = selected
    history = mixtures[selected]
    terminal = _prior([r for r in history if r["split"] == "terminal"], cutoff, len(history))
    if terminal:
        report["validation_report"]["last_terminal_prediction"] = {
            key: terminal[-1].get(key) for key in ("prediction_cutoff", "expected_return", "raw_p_up", "p_up", "range_low", "range_high")}
    # Core quality statistics need complete aligned baseline contracts.
    required_baselines = bool(terminal) and all(_baseline_contract(r) for r in terminal if r.get("calibration_ready"))
    # The incremental comparison, global gates and news-only ablation share the
    # same usable terminal dates. Missing baseline intervals cannot silently
    # change one comparator's evaluation population.
    terminal = [r for r in terminal if r.get("calibration_ready") and _baseline_contract(r)]
    incremental = _incremental(terminal, config)
    reasons.extend(_incremental_gate(incremental, minimum_oos))
    report["validation_report"]["terminal_incremental"] = incremental
    if protocol.get("status") != "accepted_shadow":
        reasons.append("frozen_base_global_gate_not_accepted")
    if not required_baselines or not terminal:
        reasons.append("missing_aligned_global_baseline_evidence")
    else:
        quality = _score(terminal, config)
        report["validation_report"]["terminal_quality"] = quality
        reasons.extend(_gate(quality, config))
    slices = {}
    for dimension in ("year", "volatility_regime", "trend_regime"):
        groups = {}
        for row in terminal:
            value = row.get("asof_date", "")[:4] if dimension == "year" else row.get(dimension, "unknown")
            groups.setdefault(value, []).append(row)
        slices[dimension] = {key: _incremental(value, config) for key, value in groups.items()}
    report["validation_report"]["regime_slices"] = slices
    common = {r["prediction_cutoff"] for r in terminal}
    news_common = [r for r in news_records if r["prediction_cutoff"] in common]
    news_metrics = (_simple_metrics(np.asarray([r["actual_return"] for r in news_common]),
                    np.asarray([r["expected_return"] for r in news_common]),
                    np.asarray([r["p_up"] for r in news_common]),
                    np.asarray([r["range_low"] for r in news_common]),
                    np.asarray([r["range_high"] for r in news_common])) if news_common else {"n": 0})
    report["validation_report"]["ablation"] = {
        "base_alpha_zero": incremental.get("base", {}),
        "news_only": news_metrics,
        "selected_mixture": incremental, "common_date_comparison": True}
    if reasons:
        report["gate_reasons"] = list(dict.fromkeys(reasons))
        return report
    live_training = _prior(rows, cutoff, len(rows))
    live_model = _fit_base(pd.DataFrame(live_training).rename(columns={"actual_return": "label_return"}), NEWS_FEATURES, "linear", config)
    live_expected, live_raw = _predict(live_model, np.asarray([[current["feature_vector"][f] for f in NEWS_FEATURES]], dtype=float))
    live_p, news_calibrated = _calibrate(mature_news, live_raw, config)
    if not news_calibrated or base_forecast.get("expected_return") is None or base_forecast.get("p_up") is None:
        reasons.append("live_news_or_base_probability_unavailable")
        return report
    expected = (1 - selected) * base_forecast["expected_return"] + selected * live_expected
    raw = (1 - selected) * base_forecast["p_up"] + selected * live_p
    previous = _prior(history, cutoff, cfg.calibration_window)
    probability, calibrated = _calibrate(previous, raw, config)
    radius = _radius(previous, config)
    if not calibrated or radius is None:
        reasons.append("live_mixture_calibration_not_ready")
        return report
    forecast = deepcopy(base_forecast)
    forecast.update(expected_return=expected, raw_p_up=raw, p_up=probability, p_down=1 - probability,
        p_flat=0., range_low=expected-radius, range_high=expected+radius, calibration_n=len(previous),
        probability_method="sigmoid_own_mixed_mature_oof", production_weight=0.)
    if forecast.get("latest_close") is not None:
        for key, value in (("expected_index", expected), ("index_range_low", expected-radius), ("index_range_high", expected+radius)):
            forecast[key] = forecast["latest_close"] * (1 + value)
    report.update(alpha=selected, blended_forecast=forecast)
    return report

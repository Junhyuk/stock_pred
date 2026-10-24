from copy import deepcopy
from datetime import timedelta

import pytest

from roboquant.index_forecast.international_news import (
    NEWS_FEATURES,
    NewsSettings,
    build_international_news_report,
    build_news_features,
    collect_news_metadata,
    normalize_news_event,
)


def event(**changes):
    item = {"title": "Iran says tanker attacked near Hormuz", "source": "wire",
            "link": "https://example.org/story?utm_source=rss", "published_at": "2026-10-23T11:00:00Z",
            "first_seen_at": "2026-10-23T12:00:00Z", "claim_status": "unconfirmed"}
    return {**item, **changes}


def test_receipt_publication_and_revision_clocks_are_all_required():
    events = [event(), event(link="https://example.org/future", published_at="2026-10-24T11:00:00Z"),
              event(link="https://example.org/unseen", first_seen_at="2026-10-24T11:00:00Z"),
              event(link="https://example.org/revised", updated_at="2026-10-24T11:00:00Z")]
    result = build_news_features(events, {}, "2026-10-23T13:00:00Z")
    assert len(result["events"]) == 1
    assert result["observed_feature_vector"]["unconfirmed_24h"] == 1
    assert result["observed_feature_vector"]["confirmed_escalation_24h"] == 0


def test_date_only_publication_uses_actual_receipt_and_never_midnight():
    item = normalize_news_event(event(published_at="2026-10-23"))
    assert item["published_at"] is None
    assert item["published_date"] == "2026-10-23"
    assert item["available_at"] == "2026-10-23T12:00:00+00:00"
    assert not build_news_features([item], {}, "2026-10-23T11:59:00Z")["events"]


def test_old_date_only_statement_is_not_fresh_because_received_today():
    items = [event(published_at=None, published_date="2026-07-13", link="https://example.org/old"),
             event(published_at=None, published_date="2026-10-12", link="https://example.org/old2"),
             event(published_at=None, published_date="2026-10-23", link="https://example.org/current")]
    result = build_news_features(items, {}, "2026-10-24T00:30:00Z")
    assert len(result["events"]) == 1
    assert result["events"][0]["published_at"] is None
    assert result["events"][0]["published_date"] == "2026-10-23"
    assert result["events"][0]["claim_status"] == "unconfirmed"
    assert result["attention_weights"][0]["age_basis"] == "publication_date_bucket_time_unknown"
    assert result["observed_feature_vector"]["unique_clusters_24h"] == 0
    assert result["coverage"]["ready"] is False


def test_undated_receipt_never_receives_fresh_publication_attention():
    result = build_news_features([event(published_at=None)], {}, "2026-10-23T13:00:00Z")
    assert not result["events"]
    assert len(result["unknown_age_context"]) == 1
    assert result["observed_feature_vector"]["unique_clusters_24h"] == 0
    assert result["attention_weights"][0]["weight"] == 0


def test_us_monday_close_event_is_unavailable_at_korean_monday_cutoff():
    item = event(published_at="2026-10-26T20:01:00Z", first_seen_at="2026-10-26T20:02:00Z")
    assert not build_news_features([item], {}, "2026-10-25T23:00:00Z")["events"]
    assert len(build_news_features([item], {}, "2026-10-26T21:00:00Z")["events"]) == 1


def test_syndication_dedup_and_unknown_feed_is_not_quiet_zero():
    duplicate = event(source="syndicator", link="https://elsewhere.org/reuters-copy")
    result = build_news_features([event(), duplicate], {}, "2026-10-23T13:00:00Z")
    assert len(result["events"]) == 1
    assert result["coverage"]["ready"] is False
    assert all(value is None for value in result["feature_vector"].values())
    assert result["attention_weights"][0]["weight"] > 0
    assert result["attention_weights"][0]["meaning"] == "risk_attention_not_return_weight"


def test_official_statement_does_not_confirm_underlying_attack():
    result = build_news_features([event(source="fed_press", source_kind="official")], {}, "2026-10-23T13:00:00Z")
    assert result["events"][0]["claim_status"] == "unconfirmed"
    assert result["observed_feature_vector"]["confirmed_escalation_24h"] == 0


@pytest.mark.parametrize("legacy_status", [None, "reported_unverified"])
def test_unspecified_rss_claim_status_is_neutral_with_explicit_claim_control(legacy_status):
    ordinary = event(title="Federal Reserve publishes monetary policy statement", source="fed_press", source_kind="official",
                     link="https://example.org/fed-statement", claim_status=legacy_status)
    alleged = event(source="Associated Press", source_kind="wire", claims=[
        {"text": "Tanker incident alleged", "attribution": "party statement", "verification_status": "unconfirmed"}])
    result = build_news_features([ordinary, alleged], {}, "2026-10-23T13:00:00Z")
    items = {row["source"]: row for row in result["events"]}
    weights = {row["event_id"]: row for row in result["attention_weights"]}
    assert items["fed_press"]["claim_status"] == "not_independently_checked"
    assert weights[items["fed_press"]["event_id"]]["claim_multiplier"] == 1
    assert items["Associated Press"]["claim_status"] == "unconfirmed"
    assert weights[items["Associated Press"]["event_id"]]["claim_multiplier"] == .5
    assert result["observed_feature_vector"]["unconfirmed_24h"] == 1
    assert result["observed_feature_vector"]["confirmed_escalation_24h"] == 0
    undated = build_news_features([{**ordinary, "published_at": None}], {}, "2026-10-23T13:00:00Z")
    assert undated["attention_weights"][0]["weight"] == 0
    assert undated["attention_weights"][0]["claim_multiplier"] == 1


@pytest.mark.parametrize("title,expected", [
    ("Federal Reserve Board announces enforcement action against a bank", 0),
    ("Federal Reserve Board approves bank merger application", 0),
    ("FOMC releases monetary policy statement and interest rate decision", 1),
    ("Federal Reserve Board approves discount rate increase", 1),
])
def test_monetary_policy_requires_policy_content_not_institution_name(title, expected):
    item = event(title=title, source="fed_press", source_kind="official", claim_status=None)
    result = build_news_features([item], {}, "2026-10-23T13:00:00Z")
    assert result["observed_feature_vector"]["monetary_policy_24h"] == expected


def test_missing_history_keeps_exact_base_and_is_json_serializable():
    import json

    base = {"expected_return": .01, "p_up": .6, "p_down": .4, "range_low": -.02,
            "range_high": .04, "expected_index": 101, "nested": {"unchanged": True}}
    report = build_international_news_report([event()], {}, [], base, "KOSPI",
                                             "2026-10-23T13:00:00Z", "2026-10-26T06:30:00Z")
    assert report["alpha"] == 0
    assert report["blended_forecast"] == base
    assert report["blended_forecast"] is not base
    assert report["news_model_ready"] is False
    json.dumps(report, allow_nan=False)


def test_collector_metadata_only_failure_and_unknown_coverage():
    xml = b'<rss><channel><item><title>Fed policy announcement</title><link>https://example.org/fed</link><pubDate>Fri, 23 Oct 2026 11:00:00 GMT</pubDate><description>FULL BODY MUST NOT BE STORED</description></item></channel></rss>'
    calls = []

    def opener(url):
        calls.append(url)
        if "fail" in url:
            raise OSError("offline")
        return xml

    result = collect_news_metadata({"feeds": [
        {"source": "fed_press", "url": "https://example.org/rss", "enabled": True, "kind": "official"},
        {"source": "failed", "url": "https://example.org/fail", "enabled": True},
        {"source": "disabled", "url": "https://example.org/disabled", "enabled": False}]},
        retrieved_at="2026-10-23T12:00:00Z", opener=opener)
    assert len(calls) == 2
    assert len(result["events"]) == 1
    assert "FULL BODY" not in str(result)
    assert result["coverage"]["windows"][0]["complete"] is False
    assert result["coverage"]["windows"][1]["status"] == "failed"


def synthetic_history():
    import numpy as np
    import pandas as pd

    rng = np.random.default_rng(14)
    rows, events, windows = [], [], []
    days = pd.bdate_range("2021-01-04", periods=540, tz="UTC")
    for i, day in enumerate(days):
        cutoff = day.replace(hour=21)
        target = cutoff + timedelta(days=1)
        positive = bool(rng.integers(0, 2))
        y = (.02 if positive else -.02) + rng.normal(0, .006)
        headline = "Central bank monetary policy decision" if positive else "Trade sanctions tariff restrictions"
        events.append(event(title=headline, link=f"https://example.org/{i}",
                            published_at=(cutoff-timedelta(hours=1)).isoformat(),
                            first_seen_at=(cutoff-timedelta(minutes=30)).isoformat()))
        windows.append({"source": "wire", "start_at": (cutoff-timedelta(days=7)).isoformat(),
                        "end_at": cutoff.isoformat(), "recorded_at": cutoff.isoformat(),
                        "complete": True, "status": "ok"})
        baseline = {"expected_return": 0., "p_up": .5, "range_low": -.025, "range_high": .025}
        rows.append({"asof_date": str(day.date()), "target_date": str(target.date()),
                     "prediction_cutoff": cutoff.isoformat(), "target_close_at": target.isoformat(),
                     "label_available_at": target.isoformat(), "actual_return": float(y),
                     "expected_return": 0., "raw_p_up": .5, "p_up": .5,
                     "range_low": -.04, "range_high": .04, "calibration_ready": True,
                     "baselines": {"legacy": baseline, "mean": baseline, "up_rate": {"p_up": .5}},
                     "references": {"previous_direction": baseline, "simple_trend": baseline},
                     "volatility_regime": "normal", "trend_regime": "positive",
                     "split": "development" if 300 <= i < 420 else "terminal" if i >= 420 else "calibration_history"})
    live = days[-1].replace(hour=21) + timedelta(days=3)
    events.append(event(title="Central bank monetary policy decision", link="https://example.org/live",
                        published_at=(live-timedelta(hours=1)).isoformat(), first_seen_at=live.isoformat()))
    windows.append({"source": "wire", "start_at": (live-timedelta(days=7)).isoformat(),
                    "end_at": live.isoformat(), "recorded_at": live.isoformat(), "complete": True, "status": "ok"})
    return events, {"required_sources": ["wire"], "windows": windows}, rows, live


def test_predictive_news_selects_bounded_weight_without_terminal_selection():
    events, coverage, rows, live = synthetic_history()
    base = {"expected_return": 0., "p_up": .5, "p_down": .5, "range_low": -.04,
            "range_high": .04, "latest_close": 100., "expected_index": 100.}
    kwargs = {"minimum_oos": 100, "settings": NewsSettings(initial_train_days=40, min_calibration_rows=20,
                calibration_window=100, bootstrap_samples=100), "evaluation_protocol": {"status": "accepted_shadow"}}
    report = build_international_news_report(events, coverage, rows, base, "US_SPX", live,
                                             live + timedelta(days=1), **kwargs)
    assert report["selected_alpha"] in (.05, .10, .20)
    assert report["news_model_ready"]
    assert report["alpha"] > 0, report["gate_reasons"]
    forecast = report["blended_forecast"]
    assert 0 < forecast["p_up"] < 1
    assert forecast["p_up"] + forecast["p_down"] == pytest.approx(1)
    assert forecast["range_low"] < forecast["expected_return"] < forecast["range_high"]
    assert forecast["expected_index"] == pytest.approx(100 * (1 + forecast["expected_return"]))
    changed = deepcopy(rows)
    changed[-1]["actual_return"] *= -1
    mutated = build_international_news_report(events, coverage, changed, base, "US_SPX", live,
                                              live + timedelta(days=1), **kwargs)
    assert mutated["selected_alpha"] == report["selected_alpha"]
    assert mutated["validation_report"]["last_terminal_prediction"] == report["validation_report"]["last_terminal_prediction"]
    # A terminal failure may veto the frozen choice; it cannot select another alpha.
    assert mutated["alpha"] in (0, report["selected_alpha"])
    assert report["validation_report"]["own_label_residual_used"] is False
    assert set(report["current_features"]["feature_vector"]) == set(NEWS_FEATURES)


def test_bad_coverage_and_unmature_labels_cannot_activate_news():
    events, coverage, rows, live = synthetic_history()
    for row in rows:
        row["label_available_at"] = "2099-01-01T00:00:00Z"
    report = build_international_news_report(events, coverage, rows, {}, "KOSDAQ", live,
                                             live + timedelta(days=1))
    assert report["alpha"] == 0
    assert report["news_model_ready"] is False
    coverage["windows"] = []
    missing = build_international_news_report(events, coverage, rows, {}, "KOSDAQ", live, live + timedelta(days=1))
    assert missing["alpha"] == 0
    assert missing["news_model_ready"] is False


def test_noise_expert_cannot_displace_a_stronger_frozen_base():
    import numpy as np

    events, coverage, rows, live = synthetic_history()
    rng = np.random.default_rng(44)
    for row in rows:
        # Independent of the fixed headline topic; base OOF is deliberately
        # stronger, so even a confidence fluctuation cannot pass the MAE gate.
        y = float(rng.normal(0, .02))
        row.update(actual_return=y, expected_return=y, p_up=.99 if y > 0 else .01)
    report = build_international_news_report(events, coverage, rows, {"expected_return": 0., "p_up": .5},
        "US_NASDAQ", live, live + timedelta(days=1), minimum_oos=100,
        settings=NewsSettings(initial_train_days=40, min_calibration_rows=20, bootstrap_samples=50),
        evaluation_protocol={"status": "accepted_shadow"})
    assert report["selected_alpha"] == 0
    assert report["alpha"] == 0

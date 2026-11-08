"""Four-market session clocks, paired US inputs and non-numeric event context."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.reliable import ReliableSettings, evaluate_reliable_forecasts
from roboquant.index_forecast.reliable_pipeline import (
    resolve_reliable_publication,
    run_reliable_pipeline,
)
from roboquant.index_forecast.us_market_inputs import normalize_us_observations, us_close_schedule


def settings():
    return ReliableSettings(initial_train_days=25, purge_days=1, retrain_step_days=15,
        calibration_window=25, min_calibration_rows=8, development_days=20, test_days=20,
        bootstrap_samples=30, bootstrap_block_days=5, min_oos_rows=15, model_families=("linear",))


def fixture(tmp_path):
    dates = pd.bdate_range(end="2026-10-23", periods=190)
    rng = np.random.default_rng(72)
    prices = pd.DataFrame({"session_date": dates.strftime("%Y-%m-%d"), "close": 2000 * np.exp(np.cumsum(rng.normal(0, .01, len(dates)))),
                           "source": "secondary_fixture", "published_at": None, "ingested_at": "2026-10-23T23:00:00Z"})
    path = tmp_path / "kr.sqlite"
    with sqlite3.connect(path) as conn:
        pd.concat([prices.assign(index_code=market) for market in ("KOSPI", "KOSDAQ")]).to_sql("index_eod", conn, index=False)
    schedule = us_close_schedule("2026-01-02", "2026-10-23")
    rows = []
    for symbol, group in schedule.groupby("symbol"):
        closes = 100 * np.exp(np.cumsum(rng.normal(0, .012, len(group))))
        rows.extend({"symbol": symbol, "session_date": day, "close": close, "source": "fixture", "source_url": "https://example.test/",
                     "fetched_at": "2026-10-23T23:00:00Z"} for day, close in zip(group.session_date, closes, strict=True))
    us = normalize_us_observations(pd.DataFrame(rows))["observations"]
    return path, dates.append(pd.DatetimeIndex(["2026-10-26"])), us


def test_explicit_us_closes_preserve_us_session_dates_and_maturity():
    rng = np.random.default_rng(12)
    dates = pd.bdate_range("2020-01-02", periods=121)
    frame = pd.DataFrame(rng.normal(size=(120, 7)), columns=PRICE_FEATURES)
    frame["label_return"] = rng.normal(0, .01, 120)
    frame["asof_date"], frame["target_date"] = dates[:-1].strftime("%Y-%m-%d"), dates[1:].strftime("%Y-%m-%d")
    frame["asof_close_at"] = frame.asof_date + "T21:00:00+00:00"
    frame["target_close_at"] = frame.target_date + "T21:00:00+00:00"
    frame["prediction_cutoff"] = frame.asof_date + "T21:15:00+00:00"
    frame["label_available_at"] = frame.target_date + "T21:15:00+00:00"
    result = evaluate_reliable_forecasts(frame, settings=settings())
    for row in result["candidate_observations"]["linear:price"]:
        assert row["target_close_at"] == row["target_date"] + "T21:00:00+00:00"
        assert row["asof_date"] in set(frame.asof_date)
        assert row["asof_date"] < row["target_date"]


def test_korean_price_us_trial_uses_common_oos_dates_and_explicit_challenger(tmp_path):
    path, sessions, observations = fixture(tmp_path)
    from dataclasses import asdict
    result = run_reliable_pipeline(path, tmp_path / "kr", config={"settings": asdict(settings())}, sessions=sessions,
                now=datetime(2026, 10, 23, 23, 30, tzinfo=UTC), us_observations=observations)
    for market in result["markets"]:
        histories = market["evaluation"]["candidate_observations"]
        assert [r["asof_date"] for r in histories["linear:price"]] == [r["asof_date"] for r in histories["linear:price_us"]]
        assert market["us_market"]["trial_status"] == "retrospective_terminal_previously_viewed"
        challenger = market["us_research_challenger"]
        assert challenger["group"] == "price_us" and challenger["calibration_n"] >= 8
        assert challenger["production_weight"] == 0
        assert challenger["p_up"] is not None


def test_missing_requested_us_group_remains_explicit_price_fallback(tmp_path):
    path, sessions, observations = fixture(tmp_path)
    from dataclasses import asdict
    result = run_reliable_pipeline(path, tmp_path / "missing-us", config={"settings": asdict(settings())}, sessions=sessions,
                now=datetime(2026, 10, 23, 23, 30, tzinfo=UTC), us_observations=observations.iloc[0:0])
    for market in result["markets"]:
        assert market["us_market"]["requested"] and not market["us_market"]["group_used"]
        assert "us_market" in market["missing_feature_groups"]
        assert market["forecast"]["group"] == "price"
        assert market["us_research_challenger"]["status"] == "not_ready"


def test_us_targets_have_own_next_session_open_and_close(tmp_path):
    assert importlib.util.find_spec("roboquant.index_forecast.us_market_pipeline"), "US target pipeline missing"
    module = importlib.import_module("roboquant.index_forecast.us_market_pipeline")
    _, _, observations = fixture(tmp_path)
    from dataclasses import asdict
    result = module.run_us_reliable_pipeline(observations, tmp_path / "us", config={"settings": asdict(settings())},
                now=datetime(2026, 10, 23, 23, 30, tzinfo=UTC))
    assert {m["market"] for m in result["markets"]} == {"US_SPX", "US_NASDAQ"}
    for market in result["markets"]:
        assert market["context"]["asof_date"] == "2026-10-23"
        assert market["context"]["target_date"] == "2026-10-26"
        assert market["context"]["issuance_deadline"] == "2026-10-26T13:30:00+00:00"
        assert market["forecast"]["target_close_at"] == "2026-10-26T20:00:00+00:00"
        assert market["forecast"]["production_weight"] == 0


def test_us_publication_blocks_at_its_own_session_open(tmp_path):
    module = importlib.import_module("roboquant.index_forecast.us_market_pipeline")
    _, _, observations = fixture(tmp_path)
    from dataclasses import asdict
    output = tmp_path / "opened-us"
    result = module.run_us_reliable_pipeline(observations, output, config={"settings": asdict(settings())},
                now=datetime(2026, 10, 26, 13, 30, tzinfo=UTC))
    assert result["status"] == "blocked_us_input_or_deadline"
    assert not list((output / "archive").glob("*.json"))


def test_us_own_price_history_retains_version_known_at_each_forecast_cutoff(tmp_path):
    module = importlib.import_module("roboquant.index_forecast.us_market_pipeline")
    _, _, observations = fixture(tmp_path)
    reporting = "2026-10-24T00:10:00Z"
    before, _ = module.build_us_target_frame(observations, symbol="^GSPC", reporting_cutoff=reporting)
    original = observations.loc[(observations.symbol == "^GSPC") & (observations.session_date == "2026-09-28")].copy()
    revision = original.copy()
    revision["close"] *= 1.25
    revision["revision"] = 1
    revision["first_seen_at"] = "2026-10-23T23:59:00Z"
    after, _ = module.build_us_target_frame(pd.concat([observations, revision]), symbol="^GSPC", reporting_cutoff=reporting)
    columns = ["asof_date", "close", *PRICE_FEATURES, "label_return", "label_available_at"]
    past_a = before.loc[before.asof_date < "2026-10-23", columns].reset_index(drop=True)
    past_b = after.loc[after.asof_date < "2026-10-23", columns].reset_index(drop=True)
    pd.testing.assert_frame_equal(past_a, past_b)
    only_late = observations.drop(index=original.index)
    late_frame, _ = module.build_us_target_frame(pd.concat([only_late, revision]), symbol="^GSPC", reporting_cutoff=reporting)
    target_row = late_frame.loc[late_frame.target_date == "2026-09-28"].iloc[0]
    assert pd.Timestamp(target_row.label_available_at) >= pd.Timestamp("2026-10-23T23:59:00Z")


def test_event_updated_content_is_filtered_before_dedup_without_losing_old_version():
    helper = importlib.import_module("roboquant.index_forecast.event_context")
    old = {"event_id": "versioned", "source": "fixture", "source_url": "https://example.test/versioned", "title": "Original known report",
           "published_at": "2026-10-24T00:00:00Z", "first_seen_at": "2026-10-24T00:01:00Z",
           "claims": [{"text": "Old statement", "verification_status": "reported"}], "scenario_conditions": ["Old condition"]}
    future = {**old, "updated_at": "2026-10-24T00:10:00Z", "claims": [{"text": "Future revised claim", "verification_status": "unconfirmed"}],
              "scenario_conditions": ["Future condition"]}
    payload = {"schema_version": "verified_event_context_v1", "events": [future, old]}
    earlier = helper.build_event_risk_context(payload, cutoff="2026-10-24T00:05:00Z")
    assert earlier["event_count"] == 1
    assert earlier["observed"][0]["claims"] == old["claims"]
    assert earlier["conditional_scenarios"] == old["scenario_conditions"]
    later = helper.build_event_risk_context(payload, cutoff="2026-10-24T00:15:00Z")
    assert later["observed"][0]["updated_at"] == future["updated_at"]
    assert later["observed"][0]["claims"] == future["claims"]


def test_missing_historical_news_forces_zero_fusion_for_kr_and_us(tmp_path):
    from dataclasses import asdict
    path, sessions, observations = fixture(tmp_path)
    module = importlib.import_module("roboquant.index_forecast.us_market_pipeline")
    kwargs = {"config": {"settings": asdict(settings())}, "now": datetime(2026, 10, 23, 23, 30, tzinfo=UTC)}
    kr = run_reliable_pipeline(path, tmp_path / "news-kr", sessions=sessions, us_observations=observations, **kwargs)
    us = module.run_us_reliable_pipeline(observations, tmp_path / "news-us", **kwargs)
    for market in kr["markets"] + us["markets"]:
        weighting = market["international_news"]
        assert weighting["alpha"] == 0 and weighting["production_weight"] == 0
        assert weighting["news_model_ready"] is False
        for key in ("expected_return", "p_up", "p_down", "range_low", "range_high"):
            assert weighting["blended_forecast"][key] == market["forecast"][key]


def test_verified_report_is_context_not_verified_incident_or_numeric_news_feature():
    assert importlib.util.find_spec("roboquant.index_forecast.event_context"), "event context helper missing"
    helper = importlib.import_module("roboquant.index_forecast.event_context")
    event = {"event_id": "claim", "source": "fixture verified publication", "source_url": "https://example.test/article",
             "title": "Attributed incident claim", "published_at": "2026-10-23T20:00:00Z", "first_seen_at": "2026-10-23T23:00:00Z",
             "verification_status": "publication_verified", "claims": [{"text": "Incident alleged", "attribution": "party statement", "verification_status": "unconfirmed"}],
             "channels": ["energy_risk", "shipping_risk"], "scenario_conditions": ["If independently verified, reassess energy and shipping risks."]}
    payload = {"schema_version": "verified_event_context_v1", "events": [event, event]}
    result = helper.build_event_risk_context(payload, cutoff="2026-10-23T23:30:00Z")
    assert result["news_ml_status"] == "not_validated_not_used"
    assert result["probability_adjustment"] == 0 and result["event_count"] == 1
    assert result["observed"][0]["claims"][0]["verification_status"] == "unconfirmed"
    assert result["event_time_inferred_from_publication"] is False
    assert helper.build_event_risk_context(payload, cutoff="2026-10-23T22:00:00Z")["event_count"] == 0
    date_only = {**event, "published_at": None, "published_date": "2026-10-23"}
    dated = helper.build_event_risk_context({**payload, "events": [date_only]}, cutoff="2026-10-23T23:30:00Z")
    assert dated["event_count"] == 1
    assert dated["observed"][0]["published_at"] is None
    assert dated["observed"][0]["availability_basis"] == "actual_receipt_publication_clock_unknown"


def test_us_pages_file_omits_oof_rows_while_archive_keeps_them(tmp_path):
    module = importlib.import_module("roboquant.index_forecast.us_market_pipeline")
    _, _, observations = fixture(tmp_path)
    from dataclasses import asdict
    output = tmp_path / "us-pages"
    result = module.run_us_reliable_pipeline(observations, output, config={"settings": asdict(settings())},
                now=datetime(2026, 10, 23, 23, 30, tzinfo=UTC))
    assert result["status"] == "research_only"
    assert result["markets"][0]["evaluation"]["candidate_observations"]
    written = json.loads((output / "reliable_index_forecast.json").read_text())
    archive = json.loads((output / "archive" / f"{result['run_id']}.json").read_text())

    def absent(value):
        if isinstance(value, dict):
            assert {"candidate_observations", "observations", "calibration_observations"}.isdisjoint(value)
            for item in value.values():
                absent(item)
        elif isinstance(value, list):
            for item in value:
                absent(item)

    absent(written)
    absent(json.loads((output / "reliable_validation_report.json").read_text()))
    assert archive["markets"][0]["evaluation"]["candidate_observations"]
    assert resolve_reliable_publication(output)["status"] == "research_only"
    manifest = json.loads((output / "reliable_publication.json").read_text())
    digest = hashlib.sha256((output / "reliable_index_forecast.json").read_bytes()).hexdigest()
    assert manifest["payload_checksums"]["reliable_index_forecast.json"] == digest

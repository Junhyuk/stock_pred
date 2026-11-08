"""US end-of-session reconstruction and actual-cutoff adversarial contracts."""
from __future__ import annotations

import importlib
import importlib.util

import pandas as pd
import pytest


def module():
    assert importlib.util.find_spec("roboquant.index_forecast.us_market_inputs") is not None, "US cutoff-aware input module is missing"
    return importlib.import_module("roboquant.index_forecast.us_market_inputs")


def raw(dates=("2026-10-21", "2026-10-22", "2026-10-23"), fetched="2026-10-23T23:00:00Z"):
    return pd.DataFrame([{"symbol": symbol, "session_date": day, "close": 100 + 2 * i,
                          "source": "fixture_secondary", "source_url": "https://example.test/daily",
                          "fetched_at": fetched}
                         for symbol in ("^GSPC", "^IXIC", "^SOX", "^VIX")
                         for i, day in enumerate(dates)])


def canonical(frame=None):
    return module().normalize_us_observations(raw() if frame is None else frame)["observations"]


def test_exchange_closes_obey_dst_and_vix_instrument_offset():
    schedule = module().us_close_schedule("2026-03-06", "2026-03-09")
    def close(symbol, day):
        return schedule.loc[(schedule.symbol == symbol) & (schedule.session_date == day), "session_close_at"].iloc[0]
    assert close("^GSPC", "2026-03-06") == "2026-03-06T21:00:00+00:00"
    assert close("^IXIC", "2026-03-09") == "2026-03-09T20:00:00+00:00"
    assert close("^VIX", "2026-03-09") == "2026-03-09T20:15:00+00:00"


def test_us_equity_early_close_is_calendar_driven():
    schedule = module().us_close_schedule("2026-11-27", "2026-11-27")
    assert schedule.loc[schedule.symbol == "^GSPC", "session_close_at"].iloc[0] == "2026-11-27T18:00:00+00:00"
    # VIX uses its own documented closing policy, never UTC midnight.
    assert pd.Timestamp(schedule.loc[schedule.symbol == "^VIX", "session_close_at"].iloc[0]) >= pd.Timestamp("2026-11-27T18:00:00Z")


def test_intraday_us_bar_is_not_a_completed_daily_observation():
    mod = module()
    normalized = mod.normalize_us_observations(raw(fetched="2026-10-23T15:00:00Z"))
    assert normalized["report"]["excluded_incomplete_bars"] == 4
    assert set(normalized["observations"].session_date) == {"2026-10-21", "2026-10-22"}
    result = mod.build_us_features(normalized["observations"], asof_cutoffs={"2026-10-23": "2026-10-23T15:40:00Z"}, live_asof_date="2026-10-23")
    assert result["report"]["status"] == "ready"
    assert all(row["selected_session"] == "2026-10-22" for row in result["report"]["latest_snapshot"].values())
    assert result["features"].iloc[0].us_spx_return_1d == pytest.approx(.02)


def test_post_close_friday_and_monday_kr_final_select_friday_us():
    mod = module()
    observations = canonical()
    before = mod.build_us_features(observations, asof_cutoffs={"2026-10-23": "2026-10-23T15:40:00Z"}, reporting_cutoff="2026-10-23T23:30:00Z")
    monday = mod.build_us_features(observations, asof_cutoffs={"2026-10-23": "2026-10-25T23:00:00Z"}, live_asof_date="2026-10-23")
    assert before["report"]["latest_snapshot"]["^GSPC"]["selected_session"] == "2026-10-22"
    assert monday["report"]["latest_snapshot"]["^GSPC"]["selected_session"] == "2026-10-23"
    assert monday["features"].iloc[0].us_spx_return_1d == pytest.approx(104 / 102 - 1)


def test_us_holiday_and_weekend_do_not_make_latest_close_stale():
    mod = module()
    observations = canonical(raw(("2026-07-01", "2026-07-02"), fetched="2026-07-03T00:00:00Z"))
    result = mod.build_us_features(observations, asof_cutoffs={"2026-07-03": "2026-07-05T23:00:00Z"}, live_asof_date="2026-07-03")
    assert result["report"]["status"] == "ready"
    assert all(row["expected_session"] == "2026-07-02" and row["stale_sessions"] == 0 for row in result["report"]["latest_snapshot"].values())


def test_korean_holiday_does_not_skip_completed_us_session():
    mod = module()
    observations = canonical(raw(("2026-10-08", "2026-10-09"), fetched="2026-10-10T00:00:00Z"))
    result = mod.build_us_features(observations, asof_cutoffs={"2026-10-08": "2026-10-11T23:00:00Z"})
    assert result["report"]["latest_snapshot"]["^SOX"]["selected_session"] == "2026-10-09"


def test_live_requires_actual_first_seen_but_reconstruction_is_explicit():
    mod = module()
    observations = canonical()
    kwargs = {"asof_cutoffs": {"2026-10-23": "2026-10-23T21:00:00Z"}, "reporting_cutoff": "2026-10-23T23:30:00Z"}
    retrospective = mod.build_us_features(observations, **kwargs)
    live = mod.build_us_features(observations, live_asof_date="2026-10-23", **kwargs)
    assert not retrospective["features"].empty
    assert retrospective["report"]["historical_mode"] == "RETROSPECTIVE_END_OF_SESSION_RECONSTRUCTION"
    assert live["features"].empty
    assert "first_seen_after_cutoff" in live["report"]["latest_snapshot"]["^GSPC"]["reasons"]


def test_missing_expected_session_or_required_previous_session_is_not_filled():
    mod = module()
    observations = canonical()
    missing_latest = observations.loc[~((observations.symbol == "^GSPC") & (observations.session_date == "2026-10-23"))]
    kwargs = {"asof_cutoffs": {"2026-10-23": "2026-10-25T23:00:00Z"}}
    stale = mod.build_us_features(missing_latest, **kwargs)
    assert stale["features"].empty
    assert stale["report"]["latest_snapshot"]["^GSPC"]["stale_sessions"] == 1
    gap = observations.loc[~((observations.symbol == "^GSPC") & (observations.session_date == "2026-10-22"))]
    result = mod.build_us_features(gap, **kwargs)
    assert result["features"].empty
    assert "missing_previous_us_session" in result["report"]["latest_snapshot"]["^GSPC"]["reasons"]


def test_late_same_date_release_and_future_revision_do_not_enter_earlier_cutoff():
    mod = module()
    observations = canonical()
    cutoff = {"asof_cutoffs": {"2026-10-23": "2026-10-23T21:00:00Z"}, "reporting_cutoff": "2026-10-23T23:30:00Z"}
    before = mod.build_us_features(observations, **cutoff)
    revision = observations.loc[(observations.symbol == "^GSPC") & (observations.session_date == "2026-10-23")].copy()
    revision["close"], revision["revision"] = 99999., 2
    revision["available_at"], revision["first_seen_at"] = "2027-01-01T00:00:00Z", "2027-01-01T00:00:00Z"
    after = mod.build_us_features(pd.concat([observations, revision]), **cutoff)
    pd.testing.assert_frame_equal(before["features"], after["features"])
    delayed = observations.copy()
    delayed.loc[(delayed.symbol == "^GSPC") & (delayed.session_date == "2026-10-23"), "available_at"] = "2026-10-23T22:00:00Z"
    assert mod.build_us_features(delayed, **cutoff)["features"].empty
    live_cutoff = {"asof_cutoffs": {"2026-10-23": "2026-10-23T23:30:00Z"}, "live_asof_date": "2026-10-23"}
    live_before = mod.build_us_features(observations, **live_cutoff)
    revision["available_at"] = observations.loc[(observations.symbol == "^GSPC") & (observations.session_date == "2026-10-23"), "available_at"].iloc[0]
    live_after = mod.build_us_features(pd.concat([observations, revision]), **live_cutoff)
    pd.testing.assert_frame_equal(live_before["features"], live_after["features"])


def test_retrospective_dataset_cannot_know_a_snapshot_after_reporting_time():
    mod = module()
    observations = canonical()
    kwargs = {"asof_cutoffs": {"2026-10-23": "2026-10-23T21:00:00Z"},
              "reporting_cutoff": "2026-10-23T23:30:00Z"}
    before = mod.build_us_features(observations, **kwargs)
    tomorrow = observations.loc[(observations.symbol == "^GSPC") & (observations.session_date == "2026-10-23")].copy()
    tomorrow["close"] = 99999.
    tomorrow["first_seen_at"] = "2027-01-01T00:00:00Z"
    after = mod.build_us_features(pd.concat([observations, tomorrow]), **kwargs)
    assert not before["features"].empty
    pd.testing.assert_frame_equal(before["features"], after["features"])

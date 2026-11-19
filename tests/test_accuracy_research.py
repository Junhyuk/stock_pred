"""Leakage and eligibility tests for the isolated rolling-window experiment."""
import json
from copy import deepcopy
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest


@pytest.fixture(scope="module")
def research_inputs():
    dates = pd.bdate_range("2019-01-02", periods=1420)
    days = dates.strftime("%Y-%m-%d")
    sessions = pd.DataFrame({"date": days,
        "close_at": pd.to_datetime(days + "T06:30:00Z"),
        "open_at": pd.to_datetime(days + "T00:00:00Z")})
    rng = np.random.default_rng(8)
    returns = rng.normal(.0004, .012, len(days))
    for i in range(1, len(returns)):
        returns[i] += .65 * returns[i - 1]
    close = 1000 * np.exp(np.cumsum(returns))
    prices = pd.DataFrame({"index_code": "KOSPI", "session_date": days, "close": close})
    records = []
    for i in range(550, 1418):
        anchor = i // 21 * 21
        records.append({"asof_date": days[i], "target_date": days[i + 1],
            "prediction_cutoff": days[i] + "T09:00:00+00:00",
            "fit_asof": days[anchor], "fit_cutoff": days[anchor] + "T09:00:00+00:00",
            "target_close_at": days[i + 1] + "T06:30:00+00:00",
            "label_available_at": days[i + 1] + "T06:30:00+00:00",
            "actual_return": close[i + 1] / close[i] - 1,
            "expected_return": .001, "p_up": .51, "calibration_ready": True,
            "range_low": -.03, "range_high": .03,
            "baselines": {"mean": {"expected_return": .0005, "p_up": .52,
                "range_low": -.03, "range_high": .03}, "up_rate": {"p_up": .5}}})
    evaluation = {"candidate_observations": {"linear:price": records},
        "observations": records[602:],
        "selected": {"family": "linear", "group": "price"},
        "development_start": days[900], "selection_frozen_before": days[1152],
        "settings": {"issuance_protocol": "after_close_daily_v1"}}
    archive = {"markets": [{"market": "KOSPI", "evaluation": evaluation}]}
    return prices, archive, sessions, pd.Timestamp(days[1400] + "T09:00:00Z")


def evaluate(inputs):
    from roboquant.index_forecast.accuracy_research import evaluate_accuracy_research
    prices, archive, sessions, as_of = inputs
    return evaluate_accuracy_research(prices, archive, sessions=sessions, as_of=as_of)


@pytest.fixture(scope="module")
def research_result(research_inputs):
    return evaluate(research_inputs)


def test_independent_common_dates_and_no_promotion(research_result):
    market = research_result["markets"]["KOSPI"]
    assert market["selected"] in {"ridge_rolling504", "huber_vol_rolling504"}
    assert market["windows"]["development"]["n"] == 252
    for window in market["windows"].values():
        for metrics in window["models"].values():
            assert metrics["n"] == window["n"]
    assert research_result["production_weight"] == 0
    assert market["forward_gate"]["required_new_matched_actuals"] == 252
    assert market["forward_gate"]["status"] == "not_started"
    assert research_result["markets"]["KOSDAQ"]["status"] == "not_ready"
    json.dumps(research_result, allow_nan=False)


def test_training_uses_fixed_grid_purge_and_full_504(research_inputs, research_result):
    _, _, sessions, _ = research_inputs
    dates = sessions["date"].tolist()
    rows = research_result["markets"]["KOSPI"]["observations"]
    assert rows
    for row in rows:
        position = dates.index(row["asof_date"])
        anchor = position // 21 * 21
        assert row["fit_asof"] == dates[anchor]
        assert row["train_n"] == 504
        assert row["train_last_asof"] < dates[anchor - 1]
        assert row["train_latest_label_available_at"] <= row["fit_cutoff"]
        assert row["prior_oof_n"] == 252
        assert row["prior_oof_last_target"] <= row["asof_date"]


def test_future_prices_cannot_change_past_forecasts_or_selection(research_inputs, research_result):
    prices, archive, sessions, as_of = research_inputs
    changed = prices.copy()
    changed.loc[changed.index >= 1360, "close"] *= 1.2
    result = evaluate((changed, archive, sessions, as_of))
    before = research_result["markets"]["KOSPI"]
    after = result["markets"]["KOSPI"]
    boundary = sessions.iloc[1359]["date"]
    assert [r for r in before["observations"] if r["target_date"] < boundary] == [
        r for r in after["observations"] if r["target_date"] < boundary]
    assert before["selected"] == after["selected"]
    assert before["windows"]["development"] == after["windows"]["development"]


def test_asof_ignores_future_input_and_last30_uses_completed_sessions(research_inputs):
    prices, archive, sessions, _ = research_inputs
    as_of = pd.Timestamp(sessions.iloc[1320]["date"] + "T09:00:00Z")
    full = evaluate((prices, archive, sessions, as_of))
    clipped = evaluate((prices.iloc[:1321], archive, sessions, as_of))
    assert full == clipped
    market = full["markets"]["KOSPI"]
    recent = market["windows"]["last30_descriptive"]
    assert recent["n"] == 30
    assert recent["target_dates"] == sessions.iloc[1291:1321]["date"].tolist()
    assert set(recent["target_dates"]).isdisjoint(
        market["windows"]["older_terminal_exploratory"]["target_dates"])


def test_off_session_prices_do_not_enter_features(research_inputs, research_result):
    prices, archive, sessions, as_of = research_inputs
    extra = pd.DataFrame([{"index_code": "KOSPI", "session_date": "2023-01-07", "close": 1e9}])
    result = evaluate((pd.concat([prices, extra], ignore_index=True), archive, sessions, as_of))
    assert result["markets"]["KOSPI"]["observations"] == research_result["markets"]["KOSPI"]["observations"]


def test_missing_anchor_price_does_not_reanchor(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    changed = prices.drop(index=1260)
    result = evaluate((changed, archive, sessions, as_of))
    rows = result["markets"]["KOSPI"]["observations"]
    for row in rows:
        position = sessions.index[sessions["date"] == row["asof_date"]][0]
        assert row["fit_asof"] == sessions.iloc[position // 21 * 21]["date"]
    assert not any(row["asof_date"] == sessions.iloc[1260]["date"] for row in rows)


def test_invalid_closes_abstain_and_insufficient_development_never_selects(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    invalid = prices.copy()
    invalid["close"] = np.inf
    result = evaluate((invalid, archive, sessions, as_of))
    assert result["markets"]["KOSPI"]["selected"] is None
    assert result["markets"]["KOSPI"]["observations"] == []
    short = deepcopy(archive)
    short["markets"][0]["evaluation"]["development_start"] = sessions.iloc[1150]["date"]
    result = evaluate((prices, short, sessions, as_of))
    assert result["markets"]["KOSPI"]["selected"] is None


def test_grid_requires_fixed_origin(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    with pytest.raises(ValueError, match="origin"):
        evaluate((prices, archive, sessions.iloc[1:], as_of))


def test_delayed_actuals_wait_for_their_explicit_publication(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    delayed = prices.copy()
    delayed.loc[1400, "published_at"] = (as_of + timedelta(hours=2)).isoformat()
    market = evaluate((delayed, archive, sessions, as_of))["markets"]["KOSPI"]
    target = sessions.iloc[1400]["date"]
    assert target not in market["windows"]["last30_descriptive"]["target_dates"]
    assert market["windows"]["last30_descriptive"]["exclusions"][target] == "price_publication_after_as_of"


def test_conflicting_same_release_closes_are_excluded(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    conflict = prices.iloc[[1400]].assign(close=1e9)
    market = evaluate((pd.concat([prices, conflict]), archive, sessions, as_of))["markets"]["KOSPI"]
    target = sessions.iloc[1400]["date"]
    assert target not in market["windows"]["last30_descriptive"]["target_dates"]
    assert market["windows"]["last30_descriptive"]["exclusions"][target] == "conflicting_price"


def test_laplace_window_excludes_outcomes_unpublished_at_forecast_time(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    delayed = prices.copy()
    delayed.loc[1335, "published_at"] = sessions.iloc[1390]["close_at"].isoformat()
    result = evaluate((delayed, archive, sessions, as_of))
    rows = result["markets"]["KOSPI"]["observations"]
    row = next(row for row in rows if row["asof_date"] == sessions.iloc[1385]["date"])
    # 1335's actual matures late, and 1336's archived origin close was unavailable.
    known = [i for i in range(1132, 1386) if i not in (1335, 1336)]
    wins = sum(prices.iloc[i]["close"] > prices.iloc[i - 1]["close"] for i in known)
    assert row["prior_oof_n"] == 252
    assert row["probability"]["laplace_prior252"] == (wins + 1) / 254


def test_archive_forecast_fitted_after_publication_is_excluded(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    modified = deepcopy(archive)
    selected = modified["markets"][0]["evaluation"]["candidate_observations"]["linear:price"]
    target = sessions.iloc[1400]["date"]
    record = next(r for r in selected if r["target_date"] == target)
    record["fit_asof"] = target
    record["fit_cutoff"] = as_of.isoformat()
    windows = evaluate((prices, modified, sessions, as_of))["markets"]["KOSPI"]["windows"]
    assert windows["last30_descriptive"]["exclusions"][target] == "invalid_archive_fit_clock"


def test_misstamped_archived_close_uses_official_outcome_time(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    modified = deepcopy(archive)
    selected = modified["markets"][0]["evaluation"]["candidate_observations"]["linear:price"]
    target = sessions.iloc[1400]["date"]
    record = next(r for r in selected if r["target_date"] == target)
    record["target_close_at"] = record["label_available_at"] = target + "T05:30:00+00:00"
    market = evaluate((prices, modified, sessions, as_of))["markets"]["KOSPI"]
    assert target in market["windows"]["last30_descriptive"]["target_dates"]
    row = next(row for row in market["observations"] if row["target_date"] == target)
    assert row["label_available_at"] == target + "T06:30:00+00:00"
    assert set(row["archive_target_clock_flags"]) == {
        "stored_target_close_mismatch", "stored_label_before_official_close",
    }


def test_probability_candidate_must_beat_expanding_up_rate_as_well_as_incumbent(research_inputs):
    prices, archive, sessions, as_of = research_inputs
    modified = deepcopy(archive)
    selected = modified["markets"][0]["evaluation"]["candidate_observations"]["linear:price"]
    for row in selected:
        row["p_up"] = .01
    market = evaluate((prices, modified, sessions, as_of))["markets"]["KOSPI"]
    scores = market["windows"]["development"]["probabilities"]
    candidate = scores["laplace_prior252"]
    assert candidate["brier_difference_ci95"]["linear:price"]["high"] < 0
    assert candidate["log_loss"] < scores["linear:price"]["log_loss"]
    assert candidate["brier_difference_ci95"]["expanding_up_rate"]["high"] >= 0
    gate = market["development_gate"]
    assert gate["selected_probability_candidate"] is None
    assert "brier_improvement_not_significant_vs_expanding_up_rate" in gate["probability_reasons"]

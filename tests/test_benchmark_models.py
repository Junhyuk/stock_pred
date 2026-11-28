"""Behavioral tests for target-specific forecasts and frozen evaluation evidence."""

import importlib.util
import json

import numpy as np
import pandas as pd
import pytest


def sample_frame(n=240, seed=16):
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    returns = 0.022 * np.roll(signal, 1) + rng.normal(0, 0.009, n)
    return pd.DataFrame(
        {
            "close": 250 * np.cumprod(1 + returns),
            "parent_close": 2000 * np.cumprod(1 + rng.normal(0, 0.008, n)),
            "us_signal": signal,
            # This is the issuance condition known at the row's own close,
            # not the next-session return being predicted.
            "broad_trigger": np.arange(n) % 3 == 0,
        },
        index=pd.bdate_range("2020-01-02", periods=n),
    )


def predict(frame, **kwargs):
    from roboquant.index_forecast.benchmark_models import evaluate_and_predict

    target = frame.index[-1] + pd.offsets.BDay()
    return evaluate_and_predict(
        "KOSPI200", frame, run_c=kwargs.pop("run_c", True),
        target_date=target.date().isoformat(),
        cutoff=f"{frame.index[-1].date()}T18:00:00+09:00",
        feature_columns=["us_signal"],
        settings={"initial_train": 60, "calibration_rows": 20,
                  "development_rows": 60, "terminal_rows": 40, "fold_size": 20,
                  "min_trigger_rows": 10, "bootstrap_samples": 100},
        **kwargs,
    )


def test_model_module_is_available():
    assert importlib.util.find_spec("roboquant.index_forecast.benchmark_models") is not None


def test_models_use_target_close_and_have_version_linked_finite_outputs():
    frame = sample_frame()
    result = predict(frame)
    json.dumps(result, allow_nan=False)
    assert result["status"] == "ready"
    for letter, row in result["models"].items():
        assert row["status"] == "ready"
        assert row["close"] == frame.close.iloc[-1]
        assert row["expected_index"] == pytest.approx(row["close"] * (1 + row["expected_return"]))
        assert row["interval"]["low"] <= row["expected_index"] <= row["interval"]["high"]
        assert row["evaluation"]["model_version"] == row["model_version"]
        assert row["production_weight"] == 0
        assert row["evaluation"]["terminal_rows"] == 40
        assert row["evaluation"]["metric_scope"] == (
            "prior_broad_triggered_sessions" if letter == "C" else "all_sessions"
        )
    c = result["models"]["C"]
    assert c["p_up"] + c["p_down"] + c["p_normal"] == pytest.approx(1)


def test_terminal_outcomes_cannot_change_selection_or_development_scores():
    frame = sample_frame()
    before = predict(frame)
    terminal_start = pd.Timestamp(before["evaluation_cache"]["terminal_start"])
    changed = frame.copy()
    # Only returns whose outcome occurs after the first terminal as-of change.
    rows = changed.index > terminal_start
    changed.loc[rows, "close"] *= np.cumprod(np.full(rows.sum(), 1.012))
    after = predict(changed)
    for letter in "ABC":
        first = before["models"][letter]["evaluation"]
        second = after["models"][letter]["evaluation"]
        assert first["selected"] == second["selected"]
        assert first["development_candidates"] == second["development_candidates"]
        assert first["direction_baseline"] == second["direction_baseline"]
    assert before["models"]["B"]["evaluation"]["brier"] != after["models"]["B"]["evaluation"]["brier"]


def test_appending_sessions_reuses_the_sealed_terminal_without_reselection():
    full = sample_frame(260)
    first = predict(full.iloc[:240])
    cache = json.loads(json.dumps(first["evaluation_cache"]))
    warm = predict(full, evaluation_cache=cache)
    assert warm["cache_reused"] is True
    for letter in "ABC":
        assert warm["models"][letter]["evaluation"] == first["models"][letter]["evaluation"]
        assert warm["models"][letter]["trained_through"] == full.index[-3].date().isoformat()
    assert warm["evaluation_cache"]["terminal_end"] == cache["terminal_end"]
    assert cache == first["evaluation_cache"], "caller-owned cache must remain immutable"


def test_historical_revisions_do_not_silently_reselect_on_the_holdout():
    frame = sample_frame()
    first = predict(frame)
    changed = frame.copy()
    changed.iloc[100, changed.columns.get_loc("close")] *= 1.03
    result = predict(changed, evaluation_cache=first["evaluation_cache"])
    assert result["status"] == "not_ready"
    assert result["reason"] == "cache_history_changed"
    assert all(row["p_up"] is None for row in result["models"].values())


def test_c_issuance_gate_does_not_filter_target_training_labels():
    frame = sample_frame()
    result = predict(frame, run_c=False)
    assert result["models"]["C"]["status"] == "not_triggered"
    assert result["models"]["C"]["p_up"] is None
    active = predict(frame, evaluation_cache=result["evaluation_cache"], run_c=True)
    assert active["models"]["C"]["status"] == "ready"
    assert active["models"]["C"]["train_n"] > int(frame.broad_trigger.sum())
    terminal = active["models"]["C"]["evaluation"]
    start, end = terminal["terminal_start"], terminal["terminal_end"]
    assert terminal["n"] == int(frame.loc[start:end, "broad_trigger"].sum())


def test_missing_target_close_abstains_instead_of_using_parent_proxy():
    frame = sample_frame()
    frame.loc[frame.index[-3], "close"] = np.nan
    result = predict(frame)
    assert result["status"] == "not_ready"
    assert result["reason"] == "incomplete_target_anchor"
    assert all(m["expected_index"] is None for m in result["models"].values())


def test_short_history_is_not_given_fabricated_probabilities():
    result = predict(sample_frame(100))
    assert result["status"] == "not_ready"
    assert result["reason"] == "insufficient_history"
    assert result["models"]["A"]["p_up"] is None


def test_unknown_trigger_history_is_not_reported_as_a_valid_c_backtest():
    frame = sample_frame().drop(columns="broad_trigger")
    result = predict(frame)
    report = result["models"]["C"]["evaluation"]
    assert report["n"] == 0
    assert "insufficient_triggered_oos" in report["gate_reasons"]
    assert report["hit_rate"] is None


def test_leaking_feature_names_and_nonchronological_dates_are_rejected():
    from roboquant.index_forecast.benchmark_models import evaluate_and_predict

    frame = sample_frame()
    with pytest.raises(ValueError, match="feature"):
        evaluate_and_predict("KOSPI200", frame, run_c=True, target_date="2022-01-01",
                             cutoff="2021-12-31T18:00:00+09:00", feature_columns=["label_return"])
    with pytest.raises(ValueError, match="chronological"):
        predict(frame.iloc[::-1])


def test_new_live_feature_hole_retains_the_original_evaluation():
    full = sample_frame(250)
    first = predict(full.iloc[:240])
    full.loc[full.index[-1], "us_signal"] = np.nan
    result = predict(full, evaluation_cache=first["evaluation_cache"])
    assert result["cache_reused"] is True
    for letter in "ABC":
        assert result["models"][letter]["evaluation"] == first["models"][letter]["evaluation"]
    # The synthetic external signal predicts the next return. It should win B,
    # and a missing current value must abstain rather than switch to another fit.
    assert "us_signal" in first["models"]["B"]["features"]
    assert result["models"]["B"]["status"] == "not_ready"
    assert result["models"]["B"]["reason"] == "missing_selected_live_features"


def test_a_evaluates_registered_linear_lightgbm_and_ensemble_families():
    result = predict(sample_frame())
    families = {candidate["family"] for candidate in result["models"]["A"]["evaluation"]["development_candidates"]}
    assert {"linear", "lightgbm", "ensemble"} <= families


def test_same_anchor_live_feature_refresh_does_not_reopen_terminal():
    frame = sample_frame()
    first = predict(frame)
    frame.loc[frame.index[-1], "us_signal"] += 1
    fresh = predict(frame, evaluation_cache=first["evaluation_cache"])
    assert fresh["cache_reused"] is True
    assert fresh["models"]["B"]["status"] == "ready"
    assert fresh["models"]["B"]["evaluation"] == first["models"]["B"]["evaluation"]


def test_matured_live_prediction_uses_its_issued_inputs_not_reconstructed_inputs():
    full = sample_frame(241)
    first = predict(full.iloc[:240])
    issued_day = full.index[239]
    full.loc[issued_day, "us_signal"] += 10  # later historical nominal-clock reconstruction
    second = predict(full, evaluation_cache=first["evaluation_cache"])
    assert second["cache_reused"] is True
    observed = next(row for row in second["evaluation_cache"]["calibration"]["B"] if row["date"] == str(issued_day.date()))
    assert observed["p"] == first["models"]["B"]["p_up"]
    assert observed["actual_return"] == pytest.approx(full.close.iloc[240] / full.close.iloc[239] - 1)


def test_cache_corruption_does_not_publish_mismatched_model_evidence():
    frame = sample_frame()
    cache = predict(frame)["evaluation_cache"]
    cache["reports"]["A"]["hit_rate"] = 1.0
    result = predict(frame, evaluation_cache=cache)
    assert result["status"] == "not_ready"
    assert result["reason"] == "cache_integrity_failed"


def test_verified_post_seal_price_revision_refreshes_calibration_not_terminal():
    full = sample_frame(250)
    first = predict(full.iloc[:240])
    second = predict(full, evaluation_cache=first["evaluation_cache"])
    revised = full.copy()
    revised.loc[revised.index[-4], "close"] *= 1.01
    blocked = predict(revised, evaluation_cache=second["evaluation_cache"])
    assert blocked["reason"] == "cache_post_seal_revision"
    refreshed = predict(revised, evaluation_cache=second["evaluation_cache"], refresh_post_seal=True)
    assert refreshed["cache_reused"] is True
    assert refreshed["evaluation_cache"]["post_seal_corrections_applied"] is True
    for letter in "ABC":
        assert refreshed["models"][letter]["evaluation"] == first["models"][letter]["evaluation"]


def test_an_unavailable_same_day_cash_close_cannot_be_scored():
    from roboquant.index_forecast.benchmark_models import evaluate_and_predict

    frame = sample_frame()
    with pytest.raises(ValueError, match="completed"):
        evaluate_and_predict("KOSPI200", frame, run_c=True,
                             target_date=str((frame.index[-1] + pd.offsets.BDay()).date()),
                             cutoff=f"{frame.index[-1].date()}T08:00:00+09:00")


def test_higher_hit_rate_wins_when_brier_optimum_differs_and_guardrails_pass():
    from roboquant.index_forecast.benchmark_models import BenchmarkSettings, _select

    records = {}
    candidates = []
    dates = {f"day-{i}" for i in range(100)}
    for name, accuracy, confidence in (("higher_hit", 60, .51), ("lower_brier", 55, .55)):
        spec = {"id": name, "kind": "logit", "features": ["x"], "group": "price"}
        candidates.append(spec)
        rows = []
        for i in range(100):
            actual_up = i % 2 == 0
            predicted_up = actual_up if i < accuracy else not actual_up
            probability = confidence if predicted_up else 1 - confidence
            rows.append({"date": f"day-{i}", "actual_return": .01 if actual_up else -.01,
                         "p": probability, "baseline_p": .5, "predicted": 0.,
                         "low": -.02, "high": .02})
        records[name] = rows
    selected, reports = _select(candidates, records, dates, c=False,
                                cfg=BenchmarkSettings(development_rows=100))
    assert reports[0]["metrics"]["brier"] > reports[1]["metrics"]["brier"]
    assert selected["id"] == "higher_hit"
    assert reports[0]["metrics"]["up_recall"] == pytest.approx(.6)
    assert reports[0]["metrics"]["down_recall"] == pytest.approx(.6)


def test_hit_gain_and_recent_window_are_paired_to_the_frozen_baseline():
    result = predict(sample_frame())
    for letter in "ABC":
        report = result["models"][letter]["evaluation"]
        assert report["selection_metric_order"][0] == "direction_hit_rate"
        assert report["direction_baseline"] in {"majority", "previous_direction", "simple_trend", "logistic"}
        assert report["hit_rate_gain_ci95"]["mean"] == pytest.approx(report["hit_rate"] - report["baseline_hit_rate"])
        assert report["windows"]["last_30"]["n"] <= 30
        assert report["windows"]["sealed_terminal"]["n"] == report["n"]


def test_prepended_history_does_not_reopen_or_invalidate_a_frozen_experiment():
    full = sample_frame(280)
    first_frame = full.iloc[40:]
    first = predict(first_frame)
    warm = predict(full, evaluation_cache=first["evaluation_cache"])
    assert warm["cache_reused"] is True
    for letter in "ABC":
        assert warm["models"][letter]["evaluation"] == first["models"][letter]["evaluation"]

"""No-future, anchor-parity and research-only invariants for p_up research candidates."""
from __future__ import annotations

import importlib.util
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from roboquant.index_forecast import probability_research as pr
from roboquant.index_forecast import reliable
from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.reliable_inputs import session_schedule
from roboquant.index_forecast.us_market_inputs import (
    US_FEATURES,
    build_us_features,
    us_close_schedule,
)

ROOT = Path(__file__).resolve().parents[1]
FEATURES = [*PRICE_FEATURES, *US_FEATURES]


def settings():
    return reliable.ReliableSettings(initial_train_days=60, purge_days=1, retrain_step_days=21,
                                     calibration_window=30, min_calibration_rows=10, development_days=20,
                                     test_days=20, bootstrap_samples=20, bootstrap_block_days=5,
                                     min_oos_rows=10, model_families=("linear",), issuance_protocol="premarket_v1")


def premarket_frame(seed=7, end="2019-12-31"):
    schedule, _ = session_schedule("2019-01-02", end)
    days = schedule["date"].tolist()
    rng = np.random.default_rng(seed)
    frame = pd.DataFrame(rng.normal(0, 1, (len(days) - 1, len(FEATURES))), columns=FEATURES)
    frame["volatility_20d"] = np.abs(frame["volatility_20d"]) * .01 + .005
    frame["asof_date"] = days[:-1]
    frame["target_date"] = days[1:]
    frame["prediction_cutoff"] = [d + "T08:00:00+09:00" for d in days[1:]]
    frame["target_close_at"] = [d + "T15:30:00+09:00" for d in days[1:]]
    frame["label_available_at"] = frame["target_close_at"]
    frame["asof_close_at"] = [d + "T15:30:00+09:00" for d in days[:-1]]
    frame["label_return"] = .004 * frame["us_nasdaq_return_1d"] + rng.normal(0, .01, len(frame))
    return frame


def walk(frame, features=FEATURES, kind="logistic"):
    return pr.walk_candidate(reliable._prepare(frame), reliable._prepare(frame, keep_unlabeled=True),
                             list(features), kind, settings())


def test_lag_rule_never_exposes_an_observation_before_its_lag_th_later_session():
    schedule, _ = session_schedule("2024-01-02", "2024-06-28")
    days = schedule["date"].tolist()
    calendar = pd.date_range("2023-12-20", "2024-06-28", freq="D").strftime("%Y-%m-%d")
    series = pd.DataFrame({"observation_date": calendar, "value": np.linspace(100, 140, len(calendar))})
    for lag in (1, 2, 3):
        out = pr.lagged_series_features(days, series, lag_sessions=lag, changes={"x": (1, "log")})
        for index, row in out.iterrows():
            observed = row["_latest_observation_date"]
            if observed is None:
                continue
            later_sessions = [d for d in days[: index + 1] if d > observed]
            assert len(later_sessions) >= lag, (row["asof_date"], observed, lag)
            # And it is the newest observation satisfying the rule.
            newer = series.loc[series["observation_date"] > observed, "observation_date"]
            for candidate in newer:
                assert len([d for d in days[: index + 1] if d > candidate]) < lag


def test_lag_rule_keeps_missing_history_missing_and_scales_changes():
    days = ["2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05"]
    series = pd.DataFrame({"observation_date": ["2024-01-02", "2024-01-03"], "value": [3.0, 3.25]})
    out = pr.lagged_series_features(days, series, lag_sessions=1,
                                    changes={"bp": (1, "bp"), "log": (1, "log"), "far": (5, "bp")})
    assert math.isnan(out.loc[0, "bp"]) and out.loc[0, "_latest_observation_date"] is None
    assert math.isnan(out.loc[1, "bp"])  # only one usable observation on 01-03
    assert out.loc[2, "bp"] == pytest.approx(25.0)
    assert out.loc[2, "log"] == pytest.approx(math.log(3.25 / 3.0))
    assert out["far"].isna().all()  # insufficient history is NaN, never zero
    with pytest.raises(ValueError):
        pr.lagged_series_features(days, series, lag_sessions=0, changes={"bp": (1, "bp")})


def test_candidate_walk_uses_the_incumbent_anchor_snapshots():
    frame = premarket_frame()
    data, grid = reliable._prepare(frame), reliable._prepare(frame, keep_unlabeled=True)
    incumbent = reliable._walk(data, {"price": list(PRICE_FEATURES)}, settings(), {}, grid)["linear:price"]
    records, fits = pr.walk_candidate(data, grid, list(PRICE_FEATURES), "logistic", settings())
    by_anchor = {f["anchor_asof"]: f for f in fits}
    assert records and {r["asof_date"] for r in records} == {r["asof_date"] for r in incumbent}
    for row in incumbent:
        fit = by_anchor[row["fit_asof"]]
        assert fit["snapshot_hash"] == row["fit_snapshot_hash"]
        assert fit["train_n"] == row["train_n"]


def test_candidate_predictions_ignore_future_rows():
    frame = premarket_frame()
    records, _ = walk(frame)
    split = records[len(records) // 2]["asof_date"]
    changed = frame.copy()
    later = changed["asof_date"] > split
    changed.loc[later, FEATURES] = 50.0
    changed.loc[later, "label_return"] = -changed.loc[later, "label_return"]
    altered, _ = walk(changed)
    before = {r["asof_date"]: r["p_up"] for r in records if r["asof_date"] <= split}
    after = {r["asof_date"]: r["p_up"] for r in altered if r["asof_date"] <= split}
    assert before == after


def test_preprocessing_is_fitted_on_training_rows_only():
    frame = reliable._prepare(premarket_frame()).iloc[:120]
    fitted = pr._fit_logistic(frame, FEATURES)
    scaler = fitted["model"].named_steps["standardscaler"]
    np.testing.assert_allclose(scaler.mean_, frame[FEATURES].to_numpy(dtype=float).mean(axis=0))


def test_regime_cells_shrink_sparse_cells_toward_the_overall_rate():
    train = pd.DataFrame({"volatility_20d": [.01] * 40 + [.03] * 2, "return_20d": [.1] * 40 + [-.1] * 2,
                          "label_return": [.01] * 20 + [-.01] * 20 + [-.01] * 2})
    fitted = pr._fit_regime_cells(train, pr.REGIME_FEATURES)
    overall = (20 + 1) / (42 + 2)
    sparse = fitted["cells"][(True, False)]
    assert sparse == pytest.approx((0 + pr.PSEUDO_COUNT * overall) / (2 + pr.PSEUDO_COUNT))
    assert 0 < sparse < overall


def test_scoring_helpers_match_reference_values():
    p = np.array([.05, .15, .55, .95])
    y = np.array([0, 1, 1, 1])
    assert pr.ece10(p, y) == pytest.approx(.25 * (.05 + .85 + .45 + .05))
    adjusted = pr.holm({"a": .01, "b": .04, "c": .03})
    assert adjusted["a"]["p_holm"] == pytest.approx(.03)
    assert adjusted["c"]["p_holm"] == pytest.approx(.06) and adjusted["b"]["p_holm"] == pytest.approx(.06)
    assert adjusted["a"]["reject_at_alpha"] and not adjusted["c"]["reject_at_alpha"]
    difference = np.random.default_rng(1).normal(-.01, .05, 300)
    ours = pr.block_bootstrap(difference)
    theirs = reliable._bootstrap(difference, reliable.ReliableSettings())
    assert (ours["low"], ours["high"]) == pytest.approx((theirs["low"], theirs["high"]))
    assert 0 < ours["p_one_sided_not_better"] < .5
    curve = pr.abstain_curve(np.array([.4, .6, .9]), np.array([0, 0, 1]), taus=(0, .3))
    assert curve[0]["hit"] == pytest.approx(2 / 3) and curve[1] == {"min_abs_p_minus_half": .3, "coverage": pytest.approx(1 / 3), "n": 1, "hit": 1.0}


def test_shadow_forecast_replays_the_walk_and_stays_research_only():
    frame = premarket_frame()
    records, _ = walk(frame)
    target = records[-5]
    history = frame.loc[frame["asof_date"] < target["asof_date"]]
    latest = frame.loc[frame["asof_date"] == target["asof_date"]].drop(columns=["label_return"])
    forecast = pr.forecast_premarket_us_shadow(history, latest, settings=settings(), prediction_grid=frame)
    assert forecast["status"] == "shadow_research" and forecast["production_weight"] == 0.0
    assert forecast["p_up"] == pytest.approx(target["p_up"], abs=1e-12)
    assert forecast["p_down"] == pytest.approx(1 - forecast["p_up"]) and forecast["p_flat"] == 0.0
    assert forecast["spec_hash"] == pr.shadow_spec_hash() and forecast["fit_asof"] == target["fit_asof"]
    broken = latest.copy()
    broken["us_sox_return_1d"] = np.nan
    blocked = pr.forecast_premarket_us_shadow(history, broken, settings=settings(), prediction_grid=frame)
    assert blocked["status"] == "not_ready" and blocked["p_up"] is None


def test_premarket_clock_sees_us_session_t_while_postclose_sees_only_t_minus_1():
    schedule = us_close_schedule("2024-03-01", "2024-03-12")
    rows = [{"schema_version": "us_market_observation_v2", "symbol": r.symbol, "session_date": r.session_date,
             "close": 100.0 * (1.01 if r.session_date == "2024-03-07" else 1.0) * (1.02 if r.session_date >= "2024-03-08" else 1.0),
             "session_close_at": r.session_close_at,
             "available_at": (datetime.fromisoformat(r.session_close_at) + timedelta(minutes=15)).isoformat(),
             "first_seen_at": "2024-04-01T00:00:00+00:00", "revision": 0, "is_final": True}
            for r in schedule.itertuples()]
    observations = pd.DataFrame(rows)
    # KRX as-of Thursday 2024-03-07; next KRX session Friday 03-08.
    postclose = build_us_features(observations, asof_cutoffs={"2024-03-07": "2024-03-07T18:00:00+09:00"},
                                  reporting_cutoff="2024-04-02T00:00:00+00:00")["report"]["latest_snapshot"]
    premarket = build_us_features(observations, asof_cutoffs={"2024-03-07": "2024-03-08T08:00:00+09:00"},
                                  reporting_cutoff="2024-04-02T00:00:00+00:00")["report"]["latest_snapshot"]
    assert postclose["^IXIC"]["selected_session"] == "2024-03-06"
    assert premarket["^IXIC"]["selected_session"] == "2024-03-07"


def test_config_freezes_the_preregistered_spec_and_keeps_zero_weight():
    config = yaml.safe_load((ROOT / "configs/probability_shadow.yaml").read_text())
    assert config["shadow"]["spec_hash"] == pr.shadow_spec_hash()
    # Forward logging was enabled by the PM on 2026-11-22; the weight stays 0.
    assert config["shadow"]["enabled"] is True and config["shadow"]["production_weight"] == 0.0
    assert pr.PREMARKET_US_SHADOW["features"] == FEATURES and pr.PREMARKET_US_SHADOW["production_weight"] == 0.0
    assert not str(config["shadow"]["ledger"]).startswith("docs")


def test_research_script_refuses_docs_and_disabled_shadow(tmp_path):
    spec = importlib.util.spec_from_file_location("run_probability_research", ROOT / "scripts/run_probability_research.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(SystemExit):
        module._refuse_docs(ROOT / "docs/data/should_not_exist")
    assert module._refuse_docs(tmp_path / "ok") == (tmp_path / "ok").resolve()
    disabled = tmp_path / "disabled.yaml"
    config = yaml.safe_load((ROOT / "configs/probability_shadow.yaml").read_text())
    config["shadow"]["enabled"] = False
    disabled.write_text(yaml.safe_dump(config))
    with pytest.raises(SystemExit, match="disabled"):
        module.main(["shadow", "--config", str(disabled), "--ledger", str(tmp_path / "ledger.jsonl")])
    tampered = tmp_path / "tampered.yaml"
    config = yaml.safe_load((ROOT / "configs/probability_shadow.yaml").read_text())
    config["shadow"]["spec_hash"] = "0" * 64
    tampered.write_text(yaml.safe_dump(config))
    with pytest.raises(SystemExit, match="hash mismatch"):
        module.main(["shadow", "--config", str(tampered), "--ledger", str(tmp_path / "ledger.jsonl")])
    assert not (tmp_path / "ledger.jsonl").exists()


def test_ohlc_features_use_only_the_same_session_bar():
    frame = pd.DataFrame({"open": [100., 100.], "high": [110., 100.], "low": [90., 100.], "close": [105., 100.]})
    out = pr.add_ohlc_features(frame)
    assert out.loc[0, "intraday_return_1d"] == pytest.approx(.05)
    assert out.loc[0, "range_1d"] == pytest.approx(math.log(110 / 90))
    assert out.loc[0, "clv_1d"] == pytest.approx(.5)
    assert math.isnan(out.loc[1, "clv_1d"])  # zero range is undefined, not zero

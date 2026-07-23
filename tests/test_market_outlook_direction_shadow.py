from __future__ import annotations

import pandas as pd

from roboquant.db import connect_database
from roboquant.market_outlook_direction_shadow import (
    _persist_shadow_run,
    _shadow_status,
    _summaries,
    evaluate_kospi_direction_walk_forward,
    market_outlook_direction_shadow_settings,
)


def test_kospi_direction_shadow_is_purged_and_calibrates_intervals() -> None:
    settings = market_outlook_direction_shadow_settings(_shadow_config())
    observations = evaluate_kospi_direction_walk_forward(_dataset(), settings)

    selected = observations[observations["candidate_name"].eq("online_selected")].copy()
    assert not selected.empty
    assert (pd.to_datetime(selected["train_end_date"]) < pd.to_datetime(selected["asof_date"])).all()
    assert selected["purge_days"].eq(2).all()
    assert selected["selection_source"].notna().all()
    assert selected["rolling_direction_accuracy"].notna().any()
    assert selected["conformal_calibration_count"].max() >= 12
    assert selected["conformal_covered"].notna().any()
    assert selected["conformal_width"].dropna().gt(0.0).all()
    production = observations[observations["candidate_name"].eq("production_ridge")]
    assert production["conformal_covered"].notna().any()


def test_kospi_direction_shadow_persists_rolling_and_confidence_metrics(tmp_path) -> None:
    conn = connect_database(tmp_path / "direction_shadow.duckdb")
    settings = market_outlook_direction_shadow_settings(_shadow_config())
    observations = evaluate_kospi_direction_walk_forward(_dataset(), settings)
    run_id = "fixture-direction-shadow"
    observations = observations.copy()
    observations["run_id"] = run_id
    summaries = _summaries(observations, settings)

    _persist_shadow_run(conn, run_id, observations, summaries, settings)

    stored_runs = conn.execute(
        """
        SELECT candidate_name, production_weight, metrics_json
        FROM market_outlook_direction_shadow_runs
        WHERE run_id = ?
        """,
        [run_id],
    ).fetchdf()
    stored_observations = conn.execute(
        """
        SELECT train_end_date, asof_date, conformal_calibration_count, rolling_direction_accuracy
        FROM market_outlook_direction_shadow_observations
        WHERE run_id = ? AND candidate_name = 'online_selected'
        """,
        [run_id],
    ).fetchdf()

    assert "online_selected" in set(stored_runs["candidate_name"])
    assert stored_runs["production_weight"].eq(0.0).all()
    assert any("direction_confidence_interval" in value for value in stored_runs["metrics_json"])
    assert not stored_observations.empty
    assert (pd.to_datetime(stored_observations["train_end_date"]) < pd.to_datetime(stored_observations["asof_date"])).all()
    assert stored_observations["conformal_calibration_count"].max() >= 12
    conn.close()


def test_kospi_direction_shadow_requires_mae_coverage_and_width_gates() -> None:
    settings = market_outlook_direction_shadow_settings(_shadow_config())
    settings.update({"min_oos_rows": 10, "coverage_lower_bound": 0.75, "coverage_upper_bound": 0.85})
    selected = {
        "direction_sample_count": 20,
        "action_coverage": 1.0,
        "direction_accuracy": 0.70,
        "mae": 0.01,
        "conformal_coverage": 0.90,
        "conformal_average_width": 0.02,
    }
    always_up = {"direction_accuracy": 0.50}
    production = {
        "candidate_name": "production_ridge",
        "direction_accuracy": 0.60,
        "mae": 0.01,
        "conformal_average_width": 0.02,
    }

    status, reason = _shadow_status(selected, always_up, production, settings)

    assert status == "rejected"
    assert reason == "selected interval coverage outside gate"


def _shadow_config() -> dict:
    return {
        "market_outlook": {
            "index_weight": 0.65,
            "breadth_weight": 0.35,
            "direction_return_threshold": 0.005,
        },
        "market_outlook_direction_shadow": {
            "initial_train_days": 70,
            "purge_days": 2,
            "retrain_step_days": 10,
            "selection_window": 20,
            "min_selection_observations": 10,
            "rolling_window": 12,
            "conformal_window": 24,
            "min_conformal_observations": 12,
            "conformal_coverage": 0.80,
            "min_action_coverage": 0.50,
            "candidate_index_weights": [1.0, 0.65, 0.0],
            "candidate_direction_thresholds": [0.0, 0.005],
        },
    }


def _dataset(rows: int = 145) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-02", periods=rows)
    records = []
    for index, asof in enumerate(dates):
        # Balanced positive/negative labels let the always-up baseline lose.
        signal = 1.0 if index % 4 in {0, 1} else -1.0
        noise = 0.0005 if index % 5 else -0.0005
        label = signal * 0.012 + noise
        records.append(
            {
                "asof_date": asof.date(),
                "target_date": (asof + pd.offsets.BDay(1)).date(),
                "market": "KOSPI",
                "horizon": "TODAY",
                "label_return": label,
                "index_return_1d": signal * 0.01,
                "index_return_5d": signal * 0.008,
                "index_return_20d": signal * 0.006,
                "index_volatility_5d": 0.01,
                "index_volatility_20d": 0.012,
                "index_ma_gap_5d": signal * 0.003,
                "index_ma_gap_20d": signal * 0.002,
                # Breadth is intentionally unhelpful so the online candidate
                # can demonstrate a domestic-index-only selection.
                "top50_up_share_21d": 0.5 - signal * 0.05,
                "top50_avg_momentum_score": 0.5,
                "top50_avg_risk_score": 0.5,
                "top50_avg_koru_impact_score": 0.5,
                "prediction_up_probability_avg": 0.5,
                "prediction_down_probability_avg": 0.5,
                "prediction_return_avg": 0.0,
            }
        )
    return pd.DataFrame(records)

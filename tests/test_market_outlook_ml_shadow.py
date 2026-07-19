from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from roboquant.db import connect_database
from roboquant.market_outlook_ml_shadow import (
    _evaluate_gate,
    get_market_outlook_ml_shadow,
    run_market_outlook_ml_shadow,
)


def _config(tmp_path) -> dict:
    return {
        "paths": {"model_dir": str(tmp_path / "models"), "report_dir": str(tmp_path / "reports")},
        "market_outlook": {},
        "market_outlook_ml_shadow": {
            "initial_train_days": 30,
            "purge_days": 1,
            "retrain_step_days": 20,
            "calibration_window": 20,
            "min_calibration_rows": 10,
            "min_signal_days": 5,
            "min_mature_dates": 5,
            "lgbm": {
                "n_estimators": 12,
                "learning_rate": 0.05,
                "num_leaves": 5,
                "min_child_samples": 5,
                "reg_lambda": 1.0,
                "feature_fraction": 1.0,
                "bagging_fraction": 1.0,
                "bagging_freq": 0,
                "random_state": 42,
            },
        },
    }


def _seed_market_history(conn) -> pd.DatetimeIndex:
    dates = pd.bdate_range("2025-01-02", periods=100)
    now = datetime.now(UTC).replace(tzinfo=None)
    for market, base in (("KOSPI", 2600.0), ("KOSDAQ", 700.0)):
        rows = []
        close = base
        for index, trade_date in enumerate(dates):
            close *= 1.0 + (np.sin(index / 4.0) + (0.15 if market == "KOSPI" else -0.1)) / 100.0
            rows.append((trade_date.date(), market, close))
        conn.executemany("INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, ?, ?)", rows)
    signal_rows = [
        (
            trade_date.date(),
            "market",
            "ALL",
            0.2,
            0.3,
            0.4,
            0.5,
            0.1,
            0.7,
            1.0,
            '{"fixture":1}',
            2,
            "[]",
            "fixture-v1",
            now,
        )
        for trade_date in dates
    ]
    conn.executemany(
        """
        INSERT INTO local_llm_news_signal_daily (
          signal_date, scope, symbol, llm_news_attention, llm_news_risk, llm_news_direction,
          llm_news_semiconductor, llm_news_macro, llm_news_confidence, llm_news_coverage,
          source_coverage_json, assessment_count, evidence_json, feature_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        signal_rows,
    )
    return dates


def test_market_outlook_lgbm_shadow_is_chronological_and_never_changes_production(tmp_path) -> None:
    conn = connect_database(tmp_path / "shadow.duckdb")
    dates = _seed_market_history(conn)
    before = conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0]

    expected_target = (dates[-1] + pd.offsets.BDay(1)).date()
    result = run_market_outlook_ml_shadow(
        conn,
        _config(tmp_path),
        asof_date="latest",
        now=datetime.combine(expected_target, datetime.min.time(), tzinfo=ZoneInfo("Asia/Seoul")),
    )
    payload = get_market_outlook_ml_shadow(conn)

    assert result["production_weight"] == 0.0
    assert conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0] == before
    assert len(payload["items"]) == 2
    assert {item["target_date"][:10] for item in payload["items"]} == {expected_target.isoformat()}
    assert all(item["production_weight"] == 0.0 for item in payload["items"])
    assert all(item["q10"] <= item["q50"] <= item["q90"] for item in payload["items"])
    assert all(item["range_low"] is not None and item["range_high"] is not None for item in payload["items"])
    assert all(item["range_low"] <= item["range_high"] for item in payload["items"])
    earliest = conn.execute("SELECT MIN(asof_date) FROM market_outlook_ml_shadow_forecasts").fetchone()[0]
    assert pd.to_datetime(earliest).date() >= dates[31].date()
    conn.close()


def test_market_outlook_shadow_gate_accepts_only_all_interval_conditions() -> None:
    settings = {
        "min_signal_days": 5,
        "min_mature_dates": 5,
        "min_calibration_rows": 5,
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "max_low_breach_worsening": 0.02,
    }
    rows = [
        {
            "actual_return": 0.01 if index < 6 else 0.03,
            "q50": 0.01,
            "baseline_expected_return": 0.01,
            "range_low": -0.01,
            "range_high": 0.02,
            "baseline_range_low": -0.01,
            "baseline_range_high": 0.02,
        }
        for index in range(8)
    ]
    status, _, _, _ = _evaluate_gate({"backtest": rows, "signal_days": 8, "mature_dates": 8}, settings)
    assert status == "accepted_shadow"
    rows[0] = {**rows[0], "q50": 0.1}
    status, reason, _, _ = _evaluate_gate({"backtest": rows, "signal_days": 8, "mature_dates": 8}, settings)
    assert status == "rejected"
    assert reason is not None

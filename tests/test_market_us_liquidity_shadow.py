from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.db import connect_database
from roboquant.market_us_liquidity_shadow import (
    _evaluate_gate,
    build_market_us_liquidity_features,
    get_market_us_liquidity_shadow,
    run_market_us_liquidity_shadow,
    update_market_us_liquidity_feedback,
)


def _config(tmp_path) -> dict:
    return {
        "paths": {"model_dir": str(tmp_path / "models")},
        "market_outlook": {"use_pykrx_calendar": False, "require_confirmed_inputs": False},
        "market_us_liquidity_shadow": {
            "initial_train_days": 30,
            "purge_days": 1,
            "retrain_step_days": 10,
            "calibration_window": 12,
            "min_calibration_rows": 5,
            "feedback_window": 8,
            "min_feedback_rows": 5,
            "min_oos_rows": 12,
            "min_live_observations": 1,
            "max_overlay_weight": 0.20,
            "skill_improvement_for_max_weight": 0.05,
            "min_effective_weight": 0.001,
        },
    }


def _seed_history(conn, periods: int = 90) -> pd.DatetimeIndex:
    dates = pd.bdate_range("2025-01-02", periods=periods)
    now = datetime.now(UTC).replace(tzinfo=None)
    etfs = ("QQQ", "SOXX", "SMH", "SPY", "EWY", "KORU")
    global_rows = []
    benchmark_rows = []
    premarket_rows = []
    for index, trade_date in enumerate(dates):
        qqq = 0.010 * np.sin(index / 5.0)
        soxx = qqq + 0.004 * np.cos(index / 7.0)
        spy = 0.007 * np.sin(index / 6.0)
        ewy = 0.005 * np.cos(index / 8.0)
        returns = {"QQQ": qqq, "SOXX": soxx, "SMH": soxx * 1.1, "SPY": spy, "EWY": ewy, "KORU": ewy * 2.2}
        for symbol in etfs:
            volume = 1_000_000.0 * (1.0 + 0.3 * np.sin(index / 3.0 + len(symbol)))
            global_rows.append((trade_date.date(), symbol, 100.0 + index, volume, returns[symbol], now))
    for market, base, sensitivity in (("KOSPI", 2600.0, 0.46), ("KOSDAQ", 720.0, 0.22)):
        close = base
        closes = []
        for index, trade_date in enumerate(dates):
            target_return = 0.0002 + sensitivity * 0.010 * np.sin(index / 5.0)
            close *= 1.0 + target_return
            closes.append(close)
            benchmark_rows.append((trade_date.date(), market, close))
        for index in range(len(dates) - 1):
            asof, target = dates[index].date(), dates[index + 1].date()
            premarket_rows.append(
                (
                    target,
                    asof,
                    market,
                    datetime.combine(target, datetime.min.time()).replace(hour=8),
                    datetime.combine(target, datetime.min.time()).replace(hour=6, minute=30),
                    asof,
                    closes[index + 1] / closes[index] - 1.0,
                    now,
                )
            )
    conn.executemany("INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, ?, ?)", benchmark_rows)
    conn.executemany(
        """
        INSERT INTO global_market_daily (
          trade_date, symbol, market_group, display_name, close, volume, return_1d,
          source_name, source_timestamp, ingested_at
        ) VALUES (?, ?, 'US_ETF', ?, ?, ?, ?, 'fixture', ?, ?)
        """,
        [(day, symbol, symbol, close, volume, ret, datetime.combine(day, datetime.min.time()), created) for day, symbol, close, volume, ret, created in global_rows],
    )
    conn.executemany(
        """
        INSERT INTO premarket_features_daily (
          prediction_date, asof_date, market, forecast_stage, prediction_cutoff,
          available_at, global_trade_date, is_final_close, label_return,
          feature_version, created_at
        ) VALUES (?, ?, ?, 'final_premarket', ?, ?, ?, TRUE, ?, 'fixture', ?)
        """,
        premarket_rows,
    )
    return dates


def test_liquidity_features_are_cutoff_safe_and_use_rolling_volume(tmp_path) -> None:
    conn = connect_database(tmp_path / "liquidity.duckdb")
    dates = _seed_history(conn)
    frame = build_market_us_liquidity_features(conn, _config(tmp_path))

    ready = frame[frame["prediction_date"].eq(dates[25].date()) & frame["market"].eq("KOSPI")].iloc[0]
    assert ready["global_trade_date"] == dates[24].date()
    assert np.isfinite(ready["us_qqq_volume_z20"])
    assert '"cutoff_safe": true' in ready["data_quality_json"]
    conn.close()


def test_liquidity_shadow_is_separate_from_production_forecasts(tmp_path) -> None:
    conn = connect_database(tmp_path / "liquidity.duckdb")
    _seed_history(conn)
    before = conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0]

    result = run_market_us_liquidity_shadow(conn, _config(tmp_path), include_live=False)
    payload = get_market_us_liquidity_shadow(conn)

    assert result["production_weight"] == 0.0
    assert conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM market_us_liquidity_features_daily").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM market_us_liquidity_shadow_forecasts WHERE is_backtest = TRUE").fetchone()[0] > 0
    assert len(payload["runs"]) == 2
    conn.close()


def test_liquidity_gate_rejects_degraded_candidate() -> None:
    settings = {
        "min_oos_rows": 12,
        "min_live_observations": 1,
        "min_effective_weight": 0.001,
        "coverage_lower_bound": 0.75,
        "coverage_upper_bound": 0.85,
        "max_average_width_ratio": 1.10,
        "max_low_breach_worsening": 0.02,
    }
    rows = [
        {
            "actual_return": 0.01 if index < 24 else 0.03,
            "baseline_expected_return": 0.0,
            "liquidity_expected_return": 0.01,
            "candidate_expected_return": 0.009,
            "baseline_range_low": -0.02,
            "baseline_range_high": 0.02,
            "candidate_range_low": -0.02,
            "candidate_range_high": 0.02,
            "dynamic_overlay_weight": 0.10,
        }
        for index in range(30)
    ]
    accepted, _, _ = _evaluate_gate(rows, settings, live_count=1)
    rejected, reason, _ = _evaluate_gate([{**row, "candidate_expected_return": -0.03} for row in rows], settings, live_count=1)
    assert accepted == "accepted_shadow"
    assert rejected == "rejected"
    assert reason is not None


def test_liquidity_feedback_waits_for_then_records_target_close(tmp_path) -> None:
    conn = connect_database(tmp_path / "feedback.duckdb")
    conn.executemany("INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, 'KOSPI', ?)", [(pd.Timestamp("2025-02-03").date(), 100.0), (pd.Timestamp("2025-02-04").date(), 102.0)])
    now = datetime.now(UTC).replace(tzinfo=None)
    conn.execute(
        """
        INSERT INTO market_us_liquidity_shadow_forecasts (
          prediction_date, asof_date, market, model_name, model_version,
          dynamic_overlay_weight, baseline_expected_return, liquidity_expected_return,
          candidate_expected_return, baseline_range_low, baseline_range_high,
          control_range_low, control_range_high, candidate_range_low, candidate_range_high,
          status, production_weight, is_backtest, data_quality_json, created_at
        ) VALUES (?, ?, 'KOSPI', 'fixture', 'fixture-v1', 0.1, 0.01, 0.02, 0.011,
                  -0.02, 0.03, -0.02, 0.03, -0.02, 0.03, 'experimental', 0.0, FALSE, '{}', ?)
        """,
        [pd.Timestamp("2025-02-04").date(), pd.Timestamp("2025-02-03").date(), now],
    )
    result = update_market_us_liquidity_feedback(conn)
    assert result["updated"] == 1
    assert conn.execute("SELECT COUNT(*) FROM market_us_liquidity_shadow_feedback_daily").fetchone()[0] == 1
    conn.close()

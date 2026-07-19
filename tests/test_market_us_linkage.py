from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from roboquant.db import connect_database
from roboquant.market_us_linkage import (
    _dynamic_overlay_weight,
    _evaluate_gate,
    build_market_us_linkage_features,
    get_market_us_linkage_shadow,
    market_us_linkage_settings,
    run_market_us_linkage_shadow,
    update_market_us_linkage_feedback,
)


def _config(tmp_path) -> dict:
    return {
        "paths": {"model_dir": str(tmp_path / "models")},
        "market_outlook": {"use_pykrx_calendar": False, "require_confirmed_inputs": False},
        "market_us_linkage_shadow": {
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
    global_rows = []
    benchmark_rows = []
    premarket_rows = []
    for market, base, sensitivity in (("KOSPI", 2600.0, 0.48), ("KOSDAQ", 720.0, 0.16)):
        close = base
        closes: list[float] = []
        for index, trade_date in enumerate(dates):
            nasdaq = 0.012 * np.sin(index / 5.0)
            sox = nasdaq + 0.008 * np.cos(index / 7.0)
            target_return = 0.0002 + sensitivity * nasdaq + 0.18 * (sox - nasdaq)
            close *= 1.0 + target_return
            closes.append(close)
            benchmark_rows.append((trade_date.date(), market, close))
            if market == "KOSPI":
                global_rows.extend(
                    [
                        (trade_date.date(), "^IXIC", nasdaq, nasdaq * 1.8, abs(nasdaq) + 0.01, now),
                        (trade_date.date(), "^SOX", sox, sox * 1.8, abs(sox) + 0.01, now),
                    ]
                )
        for index in range(len(dates) - 1):
            asof = dates[index].date()
            target = dates[index + 1].date()
            label = closes[index + 1] / closes[index] - 1.0
            premarket_rows.append(
                (
                    target,
                    asof,
                    market,
                    datetime.combine(target, datetime.min.time()).replace(hour=8),
                    datetime.combine(target, datetime.min.time()).replace(hour=6, minute=30),
                    asof,
                    True,
                    None,
                    None,
                    label,
                    "premarket_signed_global_v1",
                    now,
                )
            )
    conn.executemany("INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, ?, ?)", benchmark_rows)
    conn.executemany(
        """
        INSERT INTO global_market_daily (
          trade_date, symbol, market_group, display_name, return_1d, return_5d, volatility_20d,
          source_name, source_timestamp, ingested_at
        ) VALUES (?, ?, 'US_INDEX', ?, ?, ?, ?, 'fixture', ?, ?)
        """,
        [
            (trade_date, symbol, symbol, ret1, ret5, volatility, datetime.combine(trade_date, datetime.min.time()), collected)
            for trade_date, symbol, ret1, ret5, volatility, collected in global_rows
        ],
    )
    conn.executemany(
        """
        INSERT INTO premarket_features_daily (
          prediction_date, asof_date, market, forecast_stage, prediction_cutoff, available_at,
          global_trade_date, is_final_close, us_nasdaq_return_1d, us_sox_return_1d,
          label_return, feature_version, created_at
        ) VALUES (?, ?, ?, 'final_premarket', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        premarket_rows,
    )
    return dates


def test_us_linkage_features_use_only_aligned_final_global_session(tmp_path) -> None:
    conn = connect_database(tmp_path / "linkage.duckdb")
    dates = _seed_history(conn, periods=40)
    frame = build_market_us_linkage_features(conn, _config(tmp_path))

    assert not frame.empty
    first = frame[frame["market"].eq("KOSPI")].iloc[0]
    assert first["global_trade_date"] == dates[0].date()
    assert first["prediction_date"] == dates[1].date()
    assert first["sox_nasdaq_spread_1d"] == first["us_sox_return_1d"] - first["us_nasdaq_return_1d"]
    assert '"cutoff_safe": true' in first["data_quality_json"]
    conn.close()


def test_close_loop_weight_increases_only_for_better_global_candidate(tmp_path) -> None:
    settings = market_us_linkage_settings(_config(tmp_path))
    good = [
        {"actual_return": 0.02, "baseline_expected_return": 0.0, "global_expected_return": 0.02}
        for _ in range(10)
    ]
    bad = [
        {"actual_return": 0.02, "baseline_expected_return": 0.02, "global_expected_return": -0.02}
        for _ in range(10)
    ]

    assert _dynamic_overlay_weight(good, settings)["weight"] > 0.0
    assert _dynamic_overlay_weight(bad, settings)["weight"] == 0.0


def test_gate_separates_improved_kospi_from_degraded_kosdaq(tmp_path) -> None:
    settings = market_us_linkage_settings(_config(tmp_path))
    rows = [
        {
            # Keep 80% of the fixture inside the nominal 80% interval. This
            # exercises the coverage gate instead of accidentally testing a
            # 100%-coverage interval as a success case.
            "actual_return": 0.01 if index < 24 else 0.03,
            "baseline_expected_return": 0.0,
            "global_expected_return": 0.01,
            "candidate_expected_return": 0.009,
            "baseline_range_low": -0.02,
            "baseline_range_high": 0.02,
            "candidate_range_low": -0.02,
            "candidate_range_high": 0.02,
            "dynamic_overlay_weight": 0.10,
            "market": "KOSPI",
            "prediction_date": pd.Timestamp("2025-01-01").date(),
            "model_name": "fixture",
            "model_version": "fixture",
            "is_backtest": True,
        }
        for index in range(30)
    ]
    accepted, _, _ = _evaluate_gate(rows, settings, live_count=1)
    degraded = [{**row, "candidate_expected_return": -0.03} for row in rows]
    rejected, reason, _ = _evaluate_gate(degraded, settings, live_count=1)

    assert accepted == "accepted_shadow"
    assert rejected == "rejected"
    assert reason is not None


def test_shadow_backtest_never_changes_production_forecasts(tmp_path) -> None:
    conn = connect_database(tmp_path / "linkage.duckdb")
    _seed_history(conn)
    before = conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0]

    result = run_market_us_linkage_shadow(conn, _config(tmp_path), include_live=False)
    payload = get_market_us_linkage_shadow(conn)

    assert result["production_weight"] == 0.0
    assert conn.execute("SELECT COUNT(*) FROM market_outlook_forecasts").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM market_us_linkage_features_daily").fetchone()[0] > 0
    assert conn.execute("SELECT COUNT(*) FROM market_us_linkage_shadow_forecasts WHERE is_backtest = TRUE").fetchone()[0] > 0
    assert len(payload["runs"]) == 2
    assert payload["backtest_count"] > 0
    assert all(item["production_weight"] == 0.0 for item in payload["items"])
    conn.close()


def test_shadow_forecasts_keep_first_seen_values_on_repeat_runs(tmp_path) -> None:
    conn = connect_database(tmp_path / "immutable.duckdb")
    _seed_history(conn)

    run_market_us_linkage_shadow(conn, _config(tmp_path), include_live=False)
    before = conn.execute(
        """
        SELECT candidate_expected_return, created_at
        FROM market_us_linkage_shadow_forecasts
        WHERE market = 'KOSPI' AND is_backtest = TRUE
        ORDER BY prediction_date
        LIMIT 1
        """
    ).fetchone()
    run_market_us_linkage_shadow(conn, _config(tmp_path), include_live=False)
    after = conn.execute(
        """
        SELECT candidate_expected_return, created_at
        FROM market_us_linkage_shadow_forecasts
        WHERE market = 'KOSPI' AND is_backtest = TRUE
        ORDER BY prediction_date
        LIMIT 1
        """
    ).fetchone()

    assert after == before
    conn.close()


def test_live_feedback_waits_for_target_close_then_records_actual(tmp_path) -> None:
    conn = connect_database(tmp_path / "feedback.duckdb")
    conn.executemany(
        "INSERT INTO benchmark_daily (date, benchmark, close) VALUES (?, 'KOSPI', ?)",
        [(pd.Timestamp("2025-02-03").date(), 100.0), (pd.Timestamp("2025-02-04").date(), 102.0)],
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    conn.execute(
        """
        INSERT INTO market_us_linkage_shadow_forecasts (
          prediction_date, asof_date, market, model_name, model_version,
          dynamic_overlay_weight, baseline_expected_return, global_expected_return,
          candidate_expected_return, baseline_range_low, baseline_range_high,
          control_range_low, control_range_high, candidate_range_low, candidate_range_high,
          status, production_weight, is_backtest, data_quality_json, created_at
        ) VALUES (?, ?, 'KOSPI', 'fixture', 'fixture-v1', 0.1, 0.01, 0.02, 0.011,
                  -0.02, 0.03, -0.02, 0.03, -0.02, 0.03, 'experimental', 0.0, FALSE, '{}', ?)
        """,
        [pd.Timestamp("2025-02-04").date(), pd.Timestamp("2025-02-03").date(), now],
    )

    result = update_market_us_linkage_feedback(conn)

    assert result["updated"] == 1
    actual = conn.execute("SELECT actual_return FROM market_us_linkage_shadow_forecasts").fetchone()[0]
    assert actual == pytest.approx(0.02)
    assert conn.execute("SELECT COUNT(*) FROM market_us_linkage_feedback_daily").fetchone()[0] == 1
    conn.close()

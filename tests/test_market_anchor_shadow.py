from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from roboquant.db import connect_database
from roboquant.market_anchor_shadow import (
    MODEL_FEATURE_COLUMNS,
    build_market_anchor_shadow_forecasts,
    fit_market_anchor_shadow_model,
    get_market_anchor_shadow,
    refresh_market_anchor_backtest_runs,
    refresh_market_anchor_shadow_forecasts,
    run_market_anchor_shadow_backtest,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _config() -> dict:
    return {
        "market_anchor_shadow": {
            "model_name": "test-anchor-shadow",
            "feature_version": "test_anchor_v1",
            "forecast_stage": "final_premarket",
            "kospi_anchors": ["005930", "000660", "005380"],
            "kosdaq_top_n": 3,
            "initial_train_days": 50,
            "purge_days": 1,
            "retrain_step_days": 7,
            "calibration_window": 20,
            "min_calibration_rows": 10,
            "min_feature_support_rows": 10,
            "min_feature_support_fraction": 0.05,
            "target_coverage": 0.80,
            "coverage_lower_bound": 0.0,
            "coverage_upper_bound": 1.0,
            "max_average_width_ratio": 10.0,
            "max_low_breach_worsening": 1.0,
            "minimum_live_final_days": 60,
            "quantiles": [0.10, 0.50, 0.90],
            "lightgbm": {
                "num_leaves": 7,
                "min_child_samples": 10,
                "learning_rate": 0.05,
                "n_estimators": 30,
                "reg_lambda": 10.0,
                "feature_fraction": 1.0,
                "random_state": 42,
                "n_jobs": 1,
            },
        }
    }


def _frame(days: int = 125) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=days)
    rows: list[dict] = []
    for market_index, market in enumerate(("KOSPI", "KOSDAQ")):
        for index, asof in enumerate(dates):
            signal = ((index % 19) - 9.0) / 100.0 + market_index * 0.0005
            row = {
                "asof_date": asof.date(),
                "prediction_date": (asof + pd.offsets.BDay(1)).date(),
                "market": market,
                "forecast_stage": "final_premarket",
                "prediction_cutoff": datetime.combine(
                    (asof + pd.offsets.BDay(1)).date(), datetime.min.time()
                ).replace(hour=8),
                "is_final_close": True,
                "anchor_count": 3,
                "label_return": 0.45 * signal + (0.001 if index % 2 else -0.001),
                "data_quality_json": "{}",
            }
            for column_index, column in enumerate(MODEL_FEATURE_COLUMNS, start=1):
                row[column] = signal * (1.0 + column_index / 100.0)
            rows.append(row)
    return pd.DataFrame(rows)


def test_anchor_quantile_shadow_is_chronological_calibrated_and_never_production() -> None:
    config = _config()
    frame = _frame()
    bundle = fit_market_anchor_shadow_model(frame, config)
    forecasts, summaries = run_market_anchor_shadow_backtest(frame, config, model_version="test-v1")

    assert bundle["production_weight"] == 0.0
    assert set(bundle["models"]) == {"KOSPI", "KOSDAQ"}
    assert not forecasts.empty
    assert forecasts["production_weight"].eq(0.0).all()
    assert (forecasts["q10_return"] <= forecasts["q50_return"]).all()
    assert (forecasts["q50_return"] <= forecasts["q90_return"]).all()
    ready = forecasts[forecasts["status"].eq("ready")]
    assert not ready.empty
    assert int(ready.iloc[0]["calibration_count"]) == 10
    assert all(summary["sample_count"] >= 0 for summary in summaries)
    assert all(summary["gate_status"] == "not_ready" for summary in summaries)


def test_anchor_shadow_payload_and_api_keep_production_forecasts_untouched(tmp_path, monkeypatch) -> None:
    from app import main as app_main

    conn = connect_database(tmp_path / "anchor.duckdb")
    frame = _frame(70)
    bundle = fit_market_anchor_shadow_model(frame, _config())
    latest = build_market_anchor_shadow_forecasts(frame, bundle)
    refresh_market_anchor_shadow_forecasts(conn, latest)
    refresh_market_anchor_backtest_runs(
        conn,
        [
            {
                "model_name": "test-anchor-shadow",
                "model_version": bundle["model_version"],
                "market": "KOSPI",
                "forecast_stage": "final_premarket",
                "sample_count": 0,
                "target_coverage": 0.8,
                "gate_status": "rejected",
                "fail_reason": "insufficient calibrated OOS rows",
                "metrics": {},
            }
        ],
    )
    payload = get_market_anchor_shadow(conn)
    conn.close()

    assert payload["production_weight"] == 0.0
    assert payload["items"]
    assert not payload["backtests"] or payload["backtests"][0]["gate_status"] == "rejected"

    monkeypatch.setattr(app_main, "_conn", lambda: connect_database(tmp_path / "anchor.duckdb", read_only=True))
    api_payload = app_main.market_anchor_shadow()
    assert api_payload["production_weight"] == 0.0
    assert api_payload["items"]


def test_anchor_feature_signals_are_asof_only() -> None:
    from roboquant.market_anchor_shadow import _load_anchor_price_signals

    conn = connect_database(":memory:")
    dates = pd.bdate_range("2026-01-02", periods=22)
    rows = []
    for index, trade_date in enumerate(dates):
        rows.append(
            {
                "date": trade_date.date(),
                "symbol": "005930",
                "open": 100.0 + index,
                "high": 101.0 + index,
                "low": 99.0 + index,
                "close": 10_000.0 if index == 11 else 100.0 + index,
                "volume": 1000.0,
                "trading_value": None,
                "source": "fixture",
                "collected_at": datetime.now(UTC).replace(tzinfo=None),
            }
        )
    conn.executemany(
        """
        INSERT INTO prices_daily (
          date, symbol, open, high, low, close, volume, trading_value, source, collected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [tuple(item.values()) for item in rows],
    )
    signals = _load_anchor_price_signals(conn, ["005930"])
    conn.close()
    day = signals[signals["date"].eq(dates[10].date())].iloc[0]
    assert day["anchor_return_1d"] == pytest.approx((110.0 / 109.0) - 1.0)


def test_anchor_flow_features_require_after_close_final_collection() -> None:
    from roboquant.market_anchor_shadow import _load_anchor_investor_flows

    conn = connect_database(":memory:")
    conn.execute(
        """
        INSERT INTO investor_flows_daily (
          date, symbol, foreign_net_value, institution_net_value, retail_net_value, source, collected_at
        ) VALUES
          ('2026-07-10', '005930', 1, 2, 3, 'fixture', '2026-07-10 08:00:00'),
          ('2026-07-10', '005930', 10, 20, 30, 'fixture', '2026-07-11 00:00:00')
        """
    )
    flows = _load_anchor_investor_flows(conn, ["005930"])
    conn.close()

    assert len(flows) == 1
    assert flows.iloc[0]["foreign_net_value"] == 10


def test_anchor_global_links_exclude_values_after_prediction_cutoff() -> None:
    from roboquant.market_anchor_shadow import _linked_global_values, _load_anchor_global_signals

    conn = connect_database(":memory:")
    conn.execute(
        """
        INSERT INTO global_market_daily (
          trade_date, symbol, market_group, display_name, close, return_1d,
          source_name, source_timestamp, ingested_at
        ) VALUES
          ('2026-07-09', 'TSM', 'test', 'TSM', 1, 0.01, 'fixture', '2026-07-09 00:00:00', CURRENT_TIMESTAMP),
          ('2026-07-10', 'TSM', 'test', 'TSM', 1, 0.90, 'fixture', '2026-07-10 00:00:00', CURRENT_TIMESTAMP),
          ('2026-07-09', '^DJI', 'test', 'Dow', 1, 0.02, 'fixture', '2026-07-09 00:00:00', CURRENT_TIMESTAMP),
          ('2026-07-09', '^N225', 'test', 'Nikkei', 1, 0.03, 'fixture', '2026-07-09 00:00:00', CURRENT_TIMESTAMP)
        """
    )
    signals = _load_anchor_global_signals(conn)
    conn.close()
    base = pd.Series(
        {
            "global_trade_date": "2026-07-10",
            "prediction_cutoff": "2026-07-10 08:00:00",
            "us_sox_return_1d": 0.04,
            "us_nasdaq_return_1d": 0.05,
            "us_sp500_return_1d": 0.06,
            "us_usdkrw_return_1d": 0.07,
        }
    )
    values = _linked_global_values(base, signals, market="KOSPI")

    assert values["us_tsm_return_1d"] == pytest.approx(0.01)
    assert values["semiconductor_global_link_1d"] == pytest.approx((0.04 + 0.05 + 0.01 + 0.07) / 4.0)
    assert values["auto_global_link_1d"] == pytest.approx((0.06 + 0.02 + 0.03 + 0.07) / 4.0)


def test_anchor_backtest_gate_requires_nonworsening_interval_and_q50_metrics() -> None:
    from roboquant.market_anchor_shadow import _summarize_backtest

    dates = pd.bdate_range("2025-01-02", periods=252)
    rows = [
        {
            "asof_date": current.date(),
            "status": "ready",
            "actual_return": 0.0,
            "calibrated_range_low": -0.01,
            "calibrated_range_high": 0.01,
            "control_range_low": -0.01,
            "control_range_high": 0.01,
            "q50_return": 0.0,
            "baseline_expected_return": 0.0,
        }
        for current in dates
    ]
    accepted = _summarize_backtest(rows, market="KOSPI", settings=_config()["market_anchor_shadow"], model_version="test")
    assert accepted["gate_status"] == "accepted_shadow"

    for row in rows:
        row["q50_return"] = 0.02
    rejected = _summarize_backtest(rows, market="KOSPI", settings=_config()["market_anchor_shadow"], model_version="test")
    assert rejected["gate_status"] == "rejected"
    assert "q50 MAE" in str(rejected["fail_reason"])

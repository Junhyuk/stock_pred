from __future__ import annotations

from datetime import date, datetime, timedelta

import pandas as pd

from roboquant.db import append_dedup_table, connect_database
from roboquant.premarket_shadow import (
    FINAL_STAGE,
    MODEL_FEATURE_COLUMNS,
    PRELIMINARY_STAGE,
    build_premarket_feature_history,
    build_premarket_shadow_forecasts,
    fit_premarket_shadow_model,
    load_premarket_shadow_model,
    refresh_premarket_feature_store,
    refresh_premarket_shadow_forecasts,
    save_premarket_shadow_model,
)


def test_premarket_feature_history_aligns_preliminary_and_final_cutoffs(tmp_path) -> None:
    conn = connect_database(tmp_path / "premarket.duckdb")
    _seed_benchmark(conn)
    _seed_global(conn)
    config = {"market_outlook": {"prediction_horizon": "2M"}}

    preliminary = build_premarket_feature_history(
        conn,
        config,
        stage=PRELIMINARY_STAGE,
        asof_date="2026-06-23",
    )
    final = build_premarket_feature_history(
        conn,
        config,
        stage=FINAL_STAGE,
        asof_date="2026-06-23",
    )
    conn.close()

    preliminary_row = _latest_kospi(preliminary)
    final_row = _latest_kospi(final)
    assert preliminary_row["asof_date"] == date(2026, 6, 23)
    assert preliminary_row["prediction_date"] == date(2026, 6, 24)
    assert str(preliminary_row["global_trade_date"].date()) == "2026-06-20"
    assert str(final_row["global_trade_date"].date()) == "2026-06-23"
    assert str(preliminary_row["prediction_cutoff"]) == "2026-06-23 18:00:00"
    assert str(final_row["prediction_cutoff"]) == "2026-06-24 08:00:00"
    assert bool(preliminary_row["is_final_close"]) is False
    assert bool(final_row["is_final_close"]) is True
    assert final_row["us_sp500_return_1d"] == 0.01


def test_premarket_model_features_use_signed_returns_not_regime_scores() -> None:
    assert "us_sp500_return_1d" in MODEL_FEATURE_COLUMNS
    assert "us_sox_return_1d" in MODEL_FEATURE_COLUMNS
    assert "global_risk_score" not in MODEL_FEATURE_COLUMNS
    assert "semiconductor_score" not in MODEL_FEATURE_COLUMNS
    assert "futures_score" not in MODEL_FEATURE_COLUMNS


def test_premarket_feature_store_persists_stage_and_availability(tmp_path) -> None:
    conn = connect_database(tmp_path / "premarket_store.duckdb")
    _seed_benchmark(conn)
    _seed_global(conn)
    frame = build_premarket_feature_history(
        conn,
        {"market_outlook": {"prediction_horizon": "2M"}},
        stage=FINAL_STAGE,
        asof_date="2026-06-23",
    )

    refresh_premarket_feature_store(conn, frame)

    stored = conn.execute(
        """
        SELECT forecast_stage, prediction_cutoff, global_trade_date, is_final_close
        FROM premarket_features_daily
        WHERE prediction_date = '2026-06-24' AND market = 'KOSPI'
        """
    ).fetchone()
    conn.close()
    assert stored == (
        FINAL_STAGE,
        datetime(2026, 6, 24, 8, 0),
        date(2026, 6, 23),
        True,
    )


def test_premarket_shadow_trains_ridge_heads_and_classifier_without_risk_features(
    tmp_path,
) -> None:
    frame = _synthetic_model_frame()

    bundle = fit_premarket_shadow_model(frame, stage=FINAL_STAGE)
    forecasts = build_premarket_shadow_forecasts(frame, bundle)
    model_path = tmp_path / "model.joblib"
    save_premarket_shadow_model(bundle, model_path)
    loaded = load_premarket_shadow_model(model_path)

    assert bundle["production_weight"] == 0.0
    assert "global_risk_score" not in bundle["feature_columns"]
    assert "KOSPI" in bundle["models"]
    assert bundle["models"]["KOSPI"]["total_regressor"] is not None
    assert bundle["models"]["KOSPI"]["gap_regressor"] is not None
    assert bundle["models"]["KOSPI"]["intraday_regressor"] is not None
    assert bundle["models"]["KOSPI"]["direction_classifier"] is not None
    assert loaded["model_version"] == bundle["model_version"]
    assert len(forecasts) == 1
    assert forecasts.iloc[0]["risk_gate_status"] == "risk_off"
    assert forecasts.iloc[0]["model_version"].startswith("premarket-shadow-v1")

    conn = connect_database(tmp_path / "forecast.duckdb")
    refresh_premarket_shadow_forecasts(conn, forecasts)
    assert conn.execute("SELECT COUNT(*) FROM premarket_shadow_forecasts").fetchone()[0] == 1
    conn.close()


def test_final_premarket_shadow_requires_completed_us_close() -> None:
    frame = _synthetic_model_frame()
    bundle = fit_premarket_shadow_model(frame, stage=FINAL_STAGE)
    frame.loc[frame.index[-1], "is_final_close"] = False

    forecasts = build_premarket_shadow_forecasts(frame, bundle)

    assert forecasts.empty


def _latest_kospi(frame: pd.DataFrame) -> pd.Series:
    return frame[(frame["market"] == "KOSPI") & (frame["asof_date"] == date(2026, 6, 23))].iloc[0]


def _seed_benchmark(conn) -> None:
    rows = []
    current = date(2026, 3, 2)
    index = 0
    while current <= date(2026, 6, 24):
        if current.weekday() < 5:
            for market, base in (("KOSPI", 1000.0), ("KOSDAQ", 800.0)):
                close = base + index * 2.0
                rows.append(
                    {
                        "date": current,
                        "benchmark": market,
                        "open": close - 1.0,
                        "high": close + 2.0,
                        "low": close - 2.0,
                        "close": close,
                        "volume": 1000.0,
                        "trading_value": 1_000_000.0,
                        "collected_at": datetime(2026, 6, 24, 16),
                    }
                )
            index += 1
        current += timedelta(days=1)
    append_dedup_table(conn, "benchmark_daily", pd.DataFrame(rows), ["date", "benchmark"])


def _seed_global(conn) -> None:
    rows = []
    for trade_date, return_1d in ((date(2026, 6, 20), -0.01), (date(2026, 6, 23), 0.01)):
        for symbol in ("^GSPC", "^IXIC", "^SOX", "^VIX", "EWY", "KORU", "USDKRW=X"):
            rows.append(
                {
                    "trade_date": trade_date,
                    "symbol": symbol,
                    "market_group": "test",
                    "display_name": symbol,
                    "close": 20.0 if symbol == "^VIX" else 100.0,
                    "return_1d": return_1d,
                    "source_name": "fixture",
                    "source_timestamp": datetime.combine(trade_date, datetime.min.time()),
                    "ingested_at": datetime(2026, 6, 24, 7),
                }
            )
    append_dedup_table(
        conn,
        "global_market_daily",
        pd.DataFrame(rows),
        ["trade_date", "symbol", "source_name"],
    )


def _synthetic_model_frame() -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-02", periods=120)
    rows = []
    for index, asof in enumerate(dates):
        signal = ((index % 12) - 5.5) / 100.0
        gap = 0.35 * signal
        intraday = -0.08 * signal + (0.002 if index % 2 else -0.002)
        label = (1.0 + gap) * (1.0 + intraday) - 1.0
        row = {
            "prediction_date": (asof + pd.offsets.BDay(1)).date(),
            "asof_date": asof.date(),
            "market": "KOSPI",
            "forecast_stage": FINAL_STAGE,
            "prediction_cutoff": datetime.combine(
                (asof + pd.offsets.BDay(1)).date(),
                datetime.min.time(),
            ).replace(hour=8),
            "available_at": datetime.combine(asof.date(), datetime.min.time()).replace(hour=6),
            "global_trade_date": asof.date(),
            "global_age_days": 1,
            "is_final_close": True,
            "gap_return": gap,
            "intraday_return": intraday,
            "label_return": label,
            "global_risk_score": 55.0,
            "regime": "risk_off",
            "recommended_cash_ratio": 0.3,
            "risk_gate_status": "risk_off",
            "data_quality_json": "{}",
        }
        for column_index, column in enumerate(MODEL_FEATURE_COLUMNS, start=1):
            row[column] = signal * (1.0 + column_index / 100.0)
        rows.append(row)
    return pd.DataFrame(rows)
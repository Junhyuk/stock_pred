from __future__ import annotations

import json
from datetime import date, datetime

import pandas as pd

from roboquant.db import append_dedup_table, connect_database
from roboquant.koru import (
    attach_koru_features,
    build_koru_korea_linkage,
    decide_koru_overlay_weights,
)


def test_koru_linkage_uses_current_intraday_snapshot_before_daily_fallback(tmp_path) -> None:
    conn = connect_database(tmp_path / "koru_current.duckdb")
    _seed_base(conn)
    append_dedup_table(
        conn,
        "global_market_intraday_snapshot",
        pd.DataFrame(
            [
                {
                    "snapshot_at": datetime(2026, 6, 23, 1),
                    "symbol": "KORU",
                    "market_group": "US_KOREA_ETF",
                    "price": 18.0,
                    "change_rate": 0.06,
                    "source_name": "fixture",
                    "source_timestamp": datetime(2026, 6, 23, 1),
                    "freshness_seconds": 0,
                },
                {
                    "snapshot_at": datetime(2026, 6, 23, 1),
                    "symbol": "EWY",
                    "market_group": "US_KOREA_ETF",
                    "price": 80.0,
                    "change_rate": 0.015,
                    "source_name": "fixture",
                    "source_timestamp": datetime(2026, 6, 23, 1),
                    "freshness_seconds": 0,
                },
            ]
        ),
        ["snapshot_at", "symbol", "source_name"],
    )

    frame = build_koru_korea_linkage(conn, asof_date="2026-06-23")
    latest = frame[frame["trade_date"].eq(date(2026, 6, 23))].iloc[0]
    quality = json.loads(latest["data_quality_json"])

    assert latest["koru_return_1d"] == 0.06
    assert latest["ewy_return_1d"] == 0.015
    assert quality["signal_sources"]["koru"] == "intraday_snapshot_current_price"


def test_koru_linkage_does_not_use_same_day_us_daily_close_for_korea_date(tmp_path) -> None:
    conn = connect_database(tmp_path / "koru_daily.duckdb")
    _seed_base(conn)

    frame = build_koru_korea_linkage(conn, asof_date="2026-06-23")
    latest = frame[frame["trade_date"].eq(date(2026, 6, 23))].iloc[0]

    assert latest["koru_return_1d"] == -0.03
    assert latest["ewy_return_1d"] == -0.01
    assert latest["us_signal_date"] == date(2026, 6, 22)


def test_attach_koru_features_fills_missing_with_neutral_values() -> None:
    features = pd.DataFrame(
        [{"date": date(2026, 6, 23), "symbol": "005930", "horizon": "2M", "horizon_days": 42}]
    )

    output = attach_koru_features(features, pd.DataFrame(), missing_factor_default=0.5)

    assert output.iloc[0]["koru_impact_score"] == 0.5
    assert output.iloc[0]["koru_market_shock_flag"] == 0.0
    assert output.iloc[0]["koru_return_1d"] == 0.0


def test_koru_overlay_weight_decisions() -> None:
    full = decide_koru_overlay_weights(
        {"2M": {"precision_at_k": 0.50, "rank_ic": 0.02, "rmse": 1.0}},
        {"2M": {"precision_at_k": 0.52, "rank_ic": 0.04, "rmse": 1.04}},
    )
    partial = decide_koru_overlay_weights(
        {"3M": {"precision_at_k": 0.50, "rank_ic": 0.02, "rmse": 1.0}},
        {"3M": {"precision_at_k": 0.52, "rank_ic": 0.04, "rmse": 1.20}},
    )
    failed = decide_koru_overlay_weights(
        {"2M": {"precision_at_k": 0.50, "rank_ic": 0.02, "rmse": 1.0}},
        {"2M": {"precision_at_k": 0.50, "rank_ic": 0.02, "rmse": 1.20}},
    )

    assert full["2M"]["overlay_weight"] == 0.07
    assert partial["3M"]["overlay_weight"] == 0.02
    assert failed["2M"]["overlay_weight"] == 0.0
    assert full["6M"]["overlay_weight"] == 0.0


def _seed_base(conn) -> None:
    append_dedup_table(
        conn,
        "global_market_daily",
        pd.DataFrame(
            [
                _global("2026-06-21", "KORU", 100.0, None, 1000.0),
                _global("2026-06-22", "KORU", 97.0, -0.03, 1200.0),
                _global("2026-06-23", "KORU", 110.0, 0.134, 1400.0),
                _global("2026-06-21", "EWY", 100.0, None, 1000.0),
                _global("2026-06-22", "EWY", 99.0, -0.01, 1000.0),
                _global("2026-06-23", "EWY", 105.0, 0.061, 1000.0),
                _global("2026-06-22", "SPY", 100.0, 0.004, 1000.0),
                _global("2026-06-22", "QQQ", 100.0, 0.005, 1000.0),
                _global("2026-06-22", "USDKRW=X", 100.0, 0.002, 1000.0),
            ]
        ),
        ["trade_date", "symbol", "source_name"],
    )
    append_dedup_table(
        conn,
        "benchmark_daily",
        pd.DataFrame(
            [
                _benchmark("2026-06-22", "KOSPI", 100.0),
                _benchmark("2026-06-23", "KOSPI", 97.8),
                _benchmark("2026-06-22", "KOSDAQ", 100.0),
                _benchmark("2026-06-23", "KOSDAQ", 100.5),
            ]
        ),
        ["date", "benchmark"],
    )
    append_dedup_table(
        conn,
        "prices_daily",
        pd.DataFrame(
            [
                _price("2026-06-22", "005930", 100.0),
                _price("2026-06-23", "005930", 98.0),
                _price("2026-06-22", "000660", 100.0),
                _price("2026-06-23", "000660", 101.0),
            ]
        ),
        ["date", "symbol", "source"],
    )


def _global(day: str, symbol: str, close: float, return_1d: float | None, volume: float) -> dict:
    return {
        "trade_date": date.fromisoformat(day),
        "symbol": symbol,
        "market_group": "US_KOREA_ETF",
        "display_name": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": volume,
        "return_1d": return_1d,
        "source_name": "fixture",
        "source_timestamp": datetime.fromisoformat(f"{day}T00:00:00"),
    }


def _benchmark(day: str, benchmark: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(day),
        "benchmark": benchmark,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "trading_value": 1000.0 * close,
        "collected_at": datetime.fromisoformat(f"{day}T15:30:00"),
    }


def _price(day: str, symbol: str, close: float) -> dict:
    return {
        "date": date.fromisoformat(day),
        "symbol": symbol,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1000.0,
        "trading_value": 1000.0 * close,
        "source": "fixture",
        "collected_at": datetime.fromisoformat(f"{day}T15:30:00"),
    }

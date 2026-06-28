from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from roboquant.dashboard.dashboard_service import get_focus_stocks_demo
from roboquant.db import append_dedup_table, connect_database
from roboquant.global_market.regime import build_market_regime_row, regime_row_to_frame


def test_market_regime_builds_panic_from_synthetic_shock(tmp_path) -> None:
    conn = connect_database(tmp_path / "regime.duckdb")
    _seed_global_shock_data(conn)

    row = build_market_regime_row(
        conn,
        prediction_date=date(2026, 6, 8),
        prediction_cutoff=datetime(2026, 6, 8, 8, tzinfo=ZoneInfo("Asia/Seoul")),
        config={"regime": {"feature_version": "domestic_plus_global_regime_v1"}},
    )

    assert row["regime"] == "panic"
    assert row["recommended_cash_ratio"] == 0.5
    assert row["global_risk_score"] >= 70
    assert row["semiconductor_score"] == 25
    assert row["futures_score"] == 10
    assert "SOX -4% 이하" in row["reasons_json"]


def test_focus_demo_adjustment_ready_after_regime_row(tmp_path) -> None:
    conn = connect_database(tmp_path / "focus_regime.duckdb")
    _seed_focus_demo_base(conn)
    _seed_global_shock_data(conn)
    row = build_market_regime_row(
        conn,
        prediction_date=date(2026, 6, 8),
        prediction_cutoff=datetime(2026, 6, 8, 8, tzinfo=ZoneInfo("Asia/Seoul")),
        config={"regime": {"feature_version": "domestic_plus_global_regime_v1"}},
    )
    append_dedup_table(
        conn,
        "market_regime_daily",
        regime_row_to_frame(row),
        ["prediction_date", "prediction_cutoff", "feature_version"],
    )

    payload = get_focus_stocks_demo(conn, horizon="3M")

    assert payload["regime"]["status"] == "ready"
    samsung = next(item for item in payload["items"] if item["symbol"] == "005930")
    hynix = next(item for item in payload["items"] if item["symbol"] == "000660")
    assert samsung["global_adjustment"]["status"] == "ready"
    assert hynix["global_adjustment"]["regime_adjusted_score"] < hynix["display_score"]


def _seed_global_shock_data(conn) -> None:
    rows = pd.DataFrame(
        [
            _daily("2026-06-05", "^GSPC", "S&P 500", 100.0, None),
            _daily("2026-06-08", "^GSPC", "S&P 500", 97.0, -0.03),
            _daily("2026-06-05", "^IXIC", "Nasdaq Composite", 100.0, None),
            _daily("2026-06-08", "^IXIC", "Nasdaq Composite", 96.0, -0.04),
            _daily("2026-06-05", "^SOX", "SOX", 100.0, None),
            _daily("2026-06-08", "^SOX", "SOX", 95.0, -0.05),
            _daily("2026-06-05", "^VIX", "VIX", 20.0, None),
            _daily("2026-06-08", "^VIX", "VIX", 28.0, 0.40),
            _daily("2026-06-05", "USDKRW=X", "USD/KRW", 1300.0, None, "FX"),
            _daily("2026-06-08", "USDKRW=X", "USD/KRW", 1320.0, 0.015, "FX"),
        ]
    )
    snapshots = pd.DataFrame(
        [
            {
                "snapshot_at": datetime(2026, 6, 7, 23),
                "symbol": "NQ=F",
                "market_group": "US_FUTURES",
                "price": 98.0,
                "change_rate": -0.02,
                "source_name": "fixture",
                "source_timestamp": datetime(2026, 6, 7, 22, 55),
                "freshness_seconds": 300,
            }
        ]
    )
    append_dedup_table(conn, "global_market_daily", rows, ["trade_date", "symbol", "source_name"])
    append_dedup_table(
        conn,
        "global_market_intraday_snapshot",
        snapshots,
        ["snapshot_at", "symbol", "source_name"],
    )


def _seed_focus_demo_base(conn) -> None:
    symbols = pd.DataFrame(
        {
            "symbol": ["005930", "000660", "005850"],
            "name": ["삼성전자", "SK하이닉스", "에스엘"],
            "market": ["KOSPI", "KOSPI", "KOSPI"],
            "sector": ["반도체", "반도체", "자동차부품"],
            "is_active": [True, True, True],
        }
    )
    predictions = pd.DataFrame(
        {
            "asof_date": ["2026-06-05"] * 3,
            "symbol": ["005930", "000660", "005850"],
            "horizon": ["3M"] * 3,
            "pred_return": [0.08, 0.07, 0.03],
            "pred_prob_top20": [0.7, 0.65, 0.4],
            "pred_risk": [0.3, 0.35, 0.45],
            "confidence": [0.7, 0.68, 0.6],
            "model_version": ["demo"] * 3,
        }
    )
    append_dedup_table(conn, "symbols", symbols, ["symbol"])
    append_dedup_table(conn, "predictions", predictions, ["asof_date", "symbol", "horizon", "model_version"])


def _daily(
    trade_date: str,
    symbol: str,
    display_name: str,
    close: float,
    return_1d: float | None,
    market_group: str = "US_INDEX",
) -> dict:
    return {
        "trade_date": trade_date,
        "symbol": symbol,
        "market_group": market_group,
        "display_name": display_name,
        "close": close,
        "return_1d": return_1d,
        "source_name": "fixture",
    }

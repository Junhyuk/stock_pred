from __future__ import annotations

import sys
import types

import pandas as pd

from roboquant.data.collectors.market_metrics import (
    fetch_market_metrics_by_date,
    fetch_market_metrics_from_universe,
)
from roboquant.db import connect_database


def test_market_metrics_collector_captures_bad_pykrx_market_response(monkeypatch) -> None:
    class FakeStock:
        @staticmethod
        def get_market_cap_by_ticker(*args, **kwargs):
            raise KeyError("None of [Index(['종가', '시가총액', '거래량', '거래대금'], dtype='object')] are in the [columns]")

        @staticmethod
        def get_market_fundamental_by_ticker(*args, **kwargs):
            return pd.DataFrame()

    fake_pykrx = types.ModuleType("pykrx")
    fake_pykrx.stock = FakeStock
    monkeypatch.setitem(sys.modules, "pykrx", fake_pykrx)

    errors: list[str] = []
    frame = fetch_market_metrics_by_date("2026-06-26", ["KOSPI"], errors=errors)

    assert frame.empty
    assert errors
    assert "KOSPI" in errors[0]
    assert "시가총액" in errors[0]


def test_market_metrics_fallback_uses_current_prediction_universe_market_cap(tmp_path) -> None:
    conn = connect_database(tmp_path / "metrics.duckdb")
    conn.execute(
        """
        INSERT INTO prediction_universe_snapshot (
          snapshot_date,
          symbol,
          name,
          market,
          raw_market_cap_rank,
          prediction_rank,
          market_cap,
          security_type,
          provider,
          universe_rule,
          is_enabled,
          exclusion_reason,
          created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "2026-06-26",
            "005930",
            "삼성전자",
            "KOSPI",
            1,
            1,
            1_984_812_000_000_000,
            "COMMON",
            "fdr_poc",
            "prediction_top_market_cap",
            True,
            None,
            "2026-06-28 06:00:00",
        ],
    )

    frame = fetch_market_metrics_from_universe(conn, "2026-06-26", ["KOSPI"])

    assert len(frame) == 1
    assert frame.iloc[0]["date"].isoformat() == "2026-06-26"
    assert frame.iloc[0]["symbol"] == "005930"
    assert frame.iloc[0]["market_cap"] == 1_984_812_000_000_000
    assert frame.iloc[0]["source"] == "universe_market_cap_fallback"

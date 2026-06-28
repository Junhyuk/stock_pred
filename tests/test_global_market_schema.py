from __future__ import annotations

from datetime import date

import pytest

from roboquant.db import connect_database, ensure_schema


def test_global_market_schema_is_idempotent(tmp_path) -> None:
    conn = connect_database(tmp_path / "global_schema.duckdb")

    ensure_schema(conn)
    ensure_schema(conn)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
        ).fetchall()
    }
    assert {
        "global_market_daily",
        "global_market_intraday_snapshot",
        "market_regime_daily",
        "stock_global_exposure",
    }.issubset(tables)

    conn.execute(
        """
        INSERT INTO global_market_daily (
          trade_date, symbol, market_group, display_name, close, source_name
        )
        VALUES ('2026-06-05', '^IXIC', 'US_INDEX', 'Nasdaq Composite', 100.0, 'fixture')
        """
    )

    with pytest.raises(Exception, match="Duplicate key"):
        conn.execute(
            """
            INSERT INTO global_market_daily (
              trade_date, symbol, market_group, display_name, close, source_name
            )
            VALUES ('2026-06-05', '^IXIC', 'US_INDEX', 'Nasdaq Composite', 100.0, 'fixture')
            """
        )

    conn.execute(
        """
        INSERT INTO market_regime_daily (
          prediction_date,
          prediction_cutoff,
          global_risk_score,
          regime,
          recommended_cash_ratio,
          signals_json,
          reasons_json,
          feature_version
        )
        VALUES (
          '2026-06-08',
          '2026-06-08 08:00:00',
          70,
          'panic',
          0.50,
          '{"nasdaq_return_1d": -0.04}',
          '["Nasdaq daily shock"]',
          'domestic_plus_global_regime_v1'
        )
        """
    )

    row = conn.execute(
        """
        SELECT prediction_date, regime, recommended_cash_ratio, reasons_json
        FROM market_regime_daily
        """
    ).fetchone()
    assert row == (date(2026, 6, 8), "panic", 0.5, '["Nasdaq daily shock"]')

from __future__ import annotations

from datetime import date

import pytest

from roboquant.db import connect_database, ensure_schema

UNIVERSE_RULE = "prediction_top_market_cap"


def _insert_prediction_snapshot(
    conn,
    snapshot_date: str,
    symbol: str,
    *,
    enabled: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO prediction_universe_snapshot (
          snapshot_date,
          symbol,
          name,
          market,
          raw_market_cap_rank,
          prediction_rank,
          provider,
          universe_rule,
          is_enabled
        )
        VALUES (?, ?, ?, 'KOSPI', 1, 1, 'fixture', ?, ?)
        """,
        [snapshot_date, symbol, f"Stock {symbol}", UNIVERSE_RULE, enabled],
    )


def _insert_refresh_run(
    conn,
    run_id: str,
    snapshot_date: str,
    status: str,
    *,
    completed_at: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO universe_refresh_runs (
          run_id,
          snapshot_date,
          universe_rule,
          provider,
          status,
          completed_at
        )
        VALUES (?, ?, ?, 'fixture', ?, ?)
        """,
        [run_id, snapshot_date, UNIVERSE_RULE, status, completed_at],
    )


def test_universe_snapshot_primary_keys_preserve_dates(tmp_path) -> None:
    conn = connect_database(tmp_path / "universe_keys.duckdb")

    conn.execute(
        """
        INSERT INTO raw_market_cap_snapshot (
          snapshot_date,
          symbol,
          name,
          market,
          provider,
          universe_rule
        )
        VALUES ('2026-06-05', '005930', 'Samsung Electronics', 'KOSPI', 'fixture', ?)
        """,
        [UNIVERSE_RULE],
    )
    _insert_prediction_snapshot(conn, "2026-06-05", "005930")

    with pytest.raises(Exception, match="Duplicate key"):
        conn.execute(
            """
            INSERT INTO raw_market_cap_snapshot (
              snapshot_date,
              symbol,
              name,
              market,
              provider,
              universe_rule
            )
            VALUES ('2026-06-05', '005930', 'Duplicate', 'KOSPI', 'fixture', ?)
            """,
            [UNIVERSE_RULE],
        )

    with pytest.raises(Exception, match="Duplicate key"):
        _insert_prediction_snapshot(conn, "2026-06-05", "005930")

    _insert_prediction_snapshot(conn, "2026-06-06", "005930")
    assert conn.execute("SELECT COUNT(*) FROM prediction_universe_snapshot").fetchone()[0] == 2


def test_current_universe_uses_latest_ready_snapshot_only(tmp_path) -> None:
    conn = connect_database(tmp_path / "current_universe.duckdb")

    _insert_prediction_snapshot(conn, "2026-06-05", "005930")
    _insert_prediction_snapshot(conn, "2026-06-05", "000660", enabled=False)
    _insert_prediction_snapshot(conn, "2026-06-06", "035420")
    _insert_prediction_snapshot(conn, "2026-06-07", "068270")
    _insert_prediction_snapshot(conn, "2026-06-08", "005380")

    _insert_refresh_run(
        conn,
        "ready-20260605",
        "2026-06-05",
        "ready",
        completed_at="2026-06-05 18:00:00",
    )
    _insert_refresh_run(conn, "failed-20260606", "2026-06-06", "failed")
    _insert_refresh_run(conn, "refreshing-20260607", "2026-06-07", "refreshing")
    _insert_refresh_run(conn, "stale-20260608", "2026-06-08", "stale")

    current = conn.execute(
        """
        SELECT snapshot_date, symbol, refresh_provider, refresh_status
        FROM current_prediction_universe
        """
    ).fetchall()

    assert current == [(date(2026, 6, 5), "005930", "fixture", "ready")]

    _insert_prediction_snapshot(conn, "2026-06-09", "000270")
    _insert_refresh_run(
        conn,
        "ready-20260609",
        "2026-06-09",
        "ready",
        completed_at="2026-06-09 18:00:00",
    )

    current = conn.execute(
        "SELECT snapshot_date, symbol FROM current_prediction_universe"
    ).fetchall()
    assert current == [(date(2026, 6, 9), "000270")]


def test_refresh_status_constraint_and_schema_are_idempotent(tmp_path) -> None:
    conn = connect_database(tmp_path / "universe_idempotent.duckdb")
    _insert_prediction_snapshot(conn, "2026-06-05", "005930")
    _insert_refresh_run(conn, "ready-1", "2026-06-05", "ready")

    with pytest.raises(Exception, match="CHECK constraint failed"):
        _insert_refresh_run(conn, "invalid-1", "2026-06-06", "unknown")

    ensure_schema(conn)
    ensure_schema(conn)

    assert conn.execute("SELECT COUNT(*) FROM prediction_universe_snapshot").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM universe_refresh_runs").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM current_prediction_universe").fetchone()[0] == 1

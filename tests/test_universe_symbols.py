from __future__ import annotations

from roboquant.db import connect_database
from roboquant.universe.symbols import load_prediction_universe_symbols, sync_prediction_universe_symbols

UNIVERSE_RULE = "prediction_top_market_cap"


def _insert_prediction_snapshot(
    conn,
    snapshot_date: str,
    symbol: str,
    *,
    name: str,
    market: str,
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
        VALUES (?, ?, ?, ?, 1, 1, 'fixture', ?, TRUE)
        """,
        [snapshot_date, symbol, name, market, UNIVERSE_RULE],
    )


def _insert_refresh_run(conn, snapshot_date: str) -> None:
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
        VALUES (?, ?, ?, 'fixture', 'ready', CURRENT_TIMESTAMP)
        """,
        [f"ready-{snapshot_date.replace('-', '')}", snapshot_date, UNIVERSE_RULE],
    )


def test_sync_prediction_universe_symbols_inserts_and_updates_market(tmp_path) -> None:
    db_path = tmp_path / "symbols.duckdb"
    conn = connect_database(db_path)
    _insert_prediction_snapshot(
        conn,
        "2024-05-31",
        "005930",
        name="Samsung",
        market="KOSPI",
    )
    _insert_prediction_snapshot(
        conn,
        "2024-05-31",
        "035720",
        name="Kakao",
        market="KOSDAQ",
    )
    _insert_refresh_run(conn, "2024-05-31")
    conn.execute(
        """
        INSERT INTO symbols (symbol, name, market, is_active, collected_at)
        VALUES ('005930', 'Old Name', NULL, TRUE, CURRENT_TIMESTAMP)
        """
    )

    synced = sync_prediction_universe_symbols(conn, universe_rule=UNIVERSE_RULE)
    assert synced == 2

    symbols = conn.execute("SELECT symbol, name, market FROM symbols ORDER BY symbol").fetchdf()
    assert symbols.loc[symbols["symbol"] == "005930", "market"].iloc[0] == "KOSPI"
    assert symbols.loc[symbols["symbol"] == "005930", "name"].iloc[0] == "Samsung"
    assert symbols.loc[symbols["symbol"] == "035720", "market"].iloc[0] == "KOSDAQ"

    universe = load_prediction_universe_symbols(conn, universe_rule=UNIVERSE_RULE)
    assert len(universe) == 2
    conn.close()

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from roboquant.db import append_dedup_table, connect_database
from roboquant.universe.seed_loader import UniverseSeedExistsError, seed_prediction_universe

SNAPSHOT_DATE = date(2026, 6, 5)
UNIVERSE_RULE = "prediction_top_market_cap"
PROVIDER = "v8_seed_document"


def _seed(conn, *, force: bool = False):
    return seed_prediction_universe(
        conn,
        snapshot_date=SNAPSHOT_DATE,
        universe_rule=UNIVERSE_RULE,
        provider=PROVIDER,
        force=force,
    )


def test_seed_stores_raw_and_prediction_universes(tmp_path) -> None:
    conn = connect_database(tmp_path / "seed.duckdb")

    result = _seed(conn)

    assert result["raw_count"] == 52
    assert result["prediction_count"] == 50
    assert conn.execute("SELECT COUNT(*) FROM raw_market_cap_snapshot").fetchone()[0] == 52
    assert conn.execute("SELECT COUNT(*) FROM prediction_universe_snapshot").fetchone()[0] == 50
    assert conn.execute("SELECT COUNT(*) FROM current_prediction_universe").fetchone()[0] == 50
    assert conn.execute(
        """
        SELECT market, COUNT(*)
        FROM current_prediction_universe
        GROUP BY market
        ORDER BY market
        """
    ).fetchall() == [("KOSDAQ", 20), ("KOSPI", 30)]
    assert conn.execute(
        "SELECT COUNT(*) FROM current_prediction_universe WHERE symbol = '005930'"
    ).fetchone()[0] == 1


def test_seed_keeps_exclusions_in_raw_only(tmp_path) -> None:
    conn = connect_database(tmp_path / "exclusions.duckdb")
    _seed(conn)

    exclusions = conn.execute(
        """
        SELECT symbol, security_type, exclusion_reason
        FROM raw_market_cap_snapshot
        WHERE exclusion_reason IS NOT NULL
        ORDER BY symbol
        """
    ).fetchall()

    assert exclusions == [
        ("005935", "PREFERRED", "excluded security_type=PREFERRED"),
        ("069500", "ETF", "excluded security_type=ETF"),
    ]
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM prediction_universe_snapshot
        WHERE symbol IN ('005935', '069500')
        """
    ).fetchone()[0] == 0
    assert conn.execute(
        """
        SELECT raw_market_cap_rank, prediction_rank
        FROM prediction_universe_snapshot
        WHERE symbol = '000150'
        """
    ).fetchone() == (32, 30)


def test_seed_preserves_existing_symbol_metadata_and_adds_missing_symbols(tmp_path) -> None:
    conn = connect_database(tmp_path / "symbols.duckdb")
    append_dedup_table(
        conn,
        "symbols",
        pd.DataFrame(
            {
                "symbol": ["005930"],
                "name": ["기존 삼성전자"],
                "market": ["KOSPI"],
                "sector": ["기존 섹터"],
                "listing_date": ["1975-06-11"],
                "is_active": [True],
            }
        ),
        ["symbol"],
    )

    _seed(conn)

    existing = conn.execute(
        "SELECT name, sector, listing_date FROM symbols WHERE symbol = '005930'"
    ).fetchone()
    assert existing == ("기존 삼성전자", "기존 섹터", date(1975, 6, 11))
    assert conn.execute("SELECT COUNT(DISTINCT symbol) FROM symbols").fetchone()[0] == 52
    assert conn.execute(
        "SELECT name, market FROM symbols WHERE symbol = '196170'"
    ).fetchone() == ("알테오젠", "KOSDAQ")


def test_seed_requires_force_to_replace_same_snapshot(tmp_path) -> None:
    conn = connect_database(tmp_path / "force.duckdb")
    first = _seed(conn)

    with pytest.raises(UniverseSeedExistsError):
        _seed(conn)

    second = _seed(conn, force=True)

    assert first["run_id"] != second["run_id"]
    assert conn.execute("SELECT COUNT(*) FROM raw_market_cap_snapshot").fetchone()[0] == 52
    assert conn.execute("SELECT COUNT(*) FROM prediction_universe_snapshot").fetchone()[0] == 50
    assert conn.execute("SELECT COUNT(*) FROM universe_refresh_runs").fetchone()[0] == 1
    assert conn.execute(
        "SELECT status, kospi_selected_count, kosdaq_selected_count FROM universe_refresh_runs"
    ).fetchone() == ("ready", 30, 20)

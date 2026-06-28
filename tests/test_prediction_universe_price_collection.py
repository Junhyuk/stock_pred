from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd
import pytest

from roboquant.db import append_dedup_table, connect_database
from roboquant.universe.price_collection import (
    PredictionUniversePriceCollectionError,
    collect_prediction_universe_prices,
)

UNIVERSE_RULE = "prediction_top_market_cap"
SNAPSHOT_DATE = "2026-06-09"

BUILD_FEATURE_MATRIX_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_feature_matrix.py"
SPEC = importlib.util.spec_from_file_location("build_feature_matrix_script", BUILD_FEATURE_MATRIX_PATH)
assert SPEC is not None
build_feature_matrix_script = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_feature_matrix_script)


def test_collector_requires_complete_top50_current_universe(tmp_path) -> None:
    conn = connect_database(tmp_path / "short_universe.duckdb")
    _insert_universe(conn, kospi_count=29, kosdaq_count=20)

    with pytest.raises(PredictionUniversePriceCollectionError, match="expected=50, actual=49"):
        collect_prediction_universe_prices(
            conn,
            start_date="2019-01-01",
            end_date="2026-06-19",
            sleep_seconds=0,
            fetch_prices_fn=_fake_fetcher([]),
        )


def test_collector_incremental_for_existing_and_full_for_missing_symbols(tmp_path) -> None:
    conn = connect_database(tmp_path / "price_collection.duckdb")
    kospi_symbols, kosdaq_symbols = _insert_universe(conn)
    for symbol in kospi_symbols:
        append_dedup_table(conn, "prices_daily", _price_frame(symbol, "2026-06-09"), ["date", "symbol"])

    calls: list[tuple[str, str, str | None]] = []
    summary = collect_prediction_universe_prices(
        conn,
        start_date="2019-01-01",
        end_date="2026-06-19",
        sleep_seconds=0,
        fetch_prices_fn=_fake_fetcher(calls),
    )

    starts = {symbol: start for symbol, start, _ in calls}
    assert all(starts[symbol] == "2026-06-10" for symbol in kospi_symbols)
    assert all(starts[symbol] == "2019-01-01" for symbol in kosdaq_symbols)
    assert summary.symbol_count == 50
    assert summary.inserted_rows == 50
    assert conn.execute(
        """
        SELECT COUNT(DISTINCT p.symbol)
        FROM prices_daily AS p
        INNER JOIN current_prediction_universe AS u ON p.symbol = u.symbol
        WHERE u.market = 'KOSDAQ'
        """
    ).fetchone()[0] == 20


def test_build_feature_matrix_top50_config_filters_prices_to_current_universe(tmp_path) -> None:
    conn = connect_database(tmp_path / "feature_scope.duckdb")
    kospi_symbols, _ = _insert_universe(conn, kospi_count=1, kosdaq_count=1)
    universe_symbols = [*kospi_symbols, "200001"]
    outside_symbol = "999999"
    for symbol in [*universe_symbols, outside_symbol]:
        append_dedup_table(conn, "prices_daily", _price_frame(symbol, "2026-06-09"), ["date", "symbol"])

    prices = build_feature_matrix_script._load_prices_for_config(
        conn,
        {"universe": {"rule": UNIVERSE_RULE}},
    )

    assert sorted(prices["symbol"].astype(str).unique().tolist()) == sorted(universe_symbols)


def _insert_universe(
    conn,
    *,
    kospi_count: int = 30,
    kosdaq_count: int = 20,
) -> tuple[list[str], list[str]]:
    conn.execute(
        """
        INSERT INTO universe_refresh_runs (
          run_id,
          snapshot_date,
          universe_rule,
          provider,
          status,
          kospi_selected_count,
          kosdaq_selected_count,
          completed_at
        )
        VALUES ('ready-fixture', ?, ?, 'fixture', 'ready', ?, ?, '2026-06-09 18:00:00')
        """,
        [SNAPSHOT_DATE, UNIVERSE_RULE, kospi_count, kosdaq_count],
    )
    kospi_symbols = [f"10{i:04d}" for i in range(1, kospi_count + 1)]
    kosdaq_symbols = [f"20{i:04d}" for i in range(1, kosdaq_count + 1)]
    for market, symbols in (("KOSPI", kospi_symbols), ("KOSDAQ", kosdaq_symbols)):
        for rank, symbol in enumerate(symbols, start=1):
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
                VALUES (?, ?, ?, ?, ?, ?, 'fixture', ?, TRUE)
                """,
                [SNAPSHOT_DATE, symbol, f"Stock {symbol}", market, rank, rank, UNIVERSE_RULE],
            )
    return kospi_symbols, kosdaq_symbols


def _fake_fetcher(calls: list[tuple[str, str, str | None]]):
    def fetch(symbol: str, start_date: str, end_date: str | None = None) -> pd.DataFrame:
        calls.append((symbol, start_date, end_date))
        return _price_frame(symbol, start_date)

    return fetch


def _price_frame(symbol: str, value_date: str) -> pd.DataFrame:
    close = 100.0
    return pd.DataFrame(
        {
            "date": [pd.Timestamp(value_date).date()],
            "symbol": [str(symbol).zfill(6)],
            "open": [close],
            "high": [close + 1],
            "low": [close - 1],
            "close": [close],
            "adj_close": [close],
            "volume": [1000.0],
            "trading_value": [close * 1000],
            "market_cap": [None],
            "source": ["fixture"],
            "collected_at": [pd.Timestamp("2026-06-19T09:00:00Z")],
        }
    )

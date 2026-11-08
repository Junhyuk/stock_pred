"""Closed-session price append without a network call."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from roboquant.index_forecast.sqlite_store import connect_index_forecast
from roboquant.index_forecast.us_market_inputs import US_SYMBOLS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "refresh_premarket_prices.py"
SPEC = importlib.util.spec_from_file_location("refresh_premarket_prices", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)

KST = ZoneInfo("Asia/Seoul")


def test_symbols_are_kospi_kosdaq_and_the_four_us_indexes():
    assert refresh.KR_SYMBOLS == {"KOSPI": "^KS11", "KOSDAQ": "^KQ11"}
    assert refresh.US_PRICE_SYMBOLS == US_SYMBOLS == ("^GSPC", "^IXIC", "^SOX", "^VIX")


def test_premarket_append_keeps_closed_sessions_and_does_not_replace(tmp_path):
    fetched = datetime(2026, 10, 23, 7, 50, tzinfo=KST)
    sessions = ["2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23"]
    bars = pd.DataFrame([
        {"session_date": "2026-10-20", "open": 1, "high": 1, "low": 1, "close": None, "volume": None},
        {"session_date": "2026-10-21", "open": 1, "high": 2, "low": 1, "close": 100, "volume": 5},
        {"session_date": "2026-10-22", "open": 1, "high": 2, "low": 1, "close": 110, "volume": None},
        {"session_date": "2026-10-23", "open": 1, "high": 2, "low": 1, "close": 120, "volume": 9},
    ])
    rows = refresh.completed_kr_rows(bars, index_code="KOSPI", fetched_at=fetched, sessions=sessions)
    assert [row["session_date"] for row in rows] == ["2026-10-21", "2026-10-22"]
    assert all(row["published_at"] is None and row["turnover"] is None for row in rows)
    assert rows[1]["volume"] is None
    assert all("yfinance" in row["source"] and "original publication time is unknown" in row["source"] for row in rows)
    with pytest.raises(ValueError):
        refresh.completed_kr_rows(bars, index_code="KOSPI100", fetched_at=fetched, sessions=sessions)

    conn = connect_index_forecast(tmp_path / "index_forecast.sqlite")
    conn.execute(
        "INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
        "VALUES ('KOSPI', '2026-10-21', 1.5, 'existing-source', '2026-10-21', '2026-10-21T08:00:00+00:00')"
    )
    conn.commit()
    assert refresh.append_kr_rows(conn, rows) == 1
    rows[0]["close"] = 999
    rows[1]["close"] = 999
    assert refresh.append_kr_rows(conn, rows) == 0
    stored = {
        row["session_date"]: row
        for row in conn.execute("SELECT session_date, close, source, published_at, volume, turnover FROM index_eod")
    }
    assert stored["2026-10-21"]["close"] == 1.5
    assert stored["2026-10-21"]["source"] == "existing-source"
    assert stored["2026-10-22"]["close"] == 110
    assert stored["2026-10-22"]["published_at"] is None
    assert stored["2026-10-22"]["volume"] is None
    assert stored["2026-10-22"]["turnover"] is None
    assert "yfinance" in stored["2026-10-22"]["source"]
    assert "2026-10-23" not in stored
    assert "2026-10-20" not in stored
    assert conn.execute("SELECT COUNT(*) FROM investor_flow").fetchone()[0] == 0
    kosdaq = refresh.completed_kr_rows(
        bars.iloc[1:2].assign(close=50), index_code="KOSDAQ", fetched_at=fetched, sessions=sessions)
    refresh.append_kr_rows(conn, kosdaq)
    codes = {row[0] for row in conn.execute("SELECT DISTINCT index_code FROM index_eod")}
    assert codes == {"KOSPI", "KOSDAQ"}
    conn.close()


def test_dated_us_csv_refuses_a_changed_snapshot(tmp_path):
    fetched = datetime(2026, 11, 9, 7, 50, tzinfo=KST)
    assert refresh.dated_us_csv(tmp_path, fetched).name == "us_market_observations_v2_20261109.csv"
    raw = pd.DataFrame([{
        "symbol": "^GSPC",
        "session_date": "2026-10-23",
        "close": 5800.0,
        "source": refresh.YFINANCE_SOURCE,
        "source_url": "https://finance.yahoo.com/quote/%5EGSPC/history/",
        "fetched_at": "2026-10-24T12:00:00+00:00",
    }])
    destination = tmp_path / "us_market_observations_v2_20261024.csv"
    written = refresh.write_us_observations(raw, destination)
    assert written == destination
    assert "yfinance" in destination.read_text(encoding="utf-8")
    changed = raw.copy()
    changed.loc[0, "close"] = 1111
    with pytest.raises(ValueError, match="immutable"):
        refresh.write_us_observations(changed, destination)
    refresh.write_us_observations(raw, destination)
    with pytest.raises(ValueError):
        refresh.write_us_observations(pd.DataFrame(), destination)


def test_sqlite_schema_is_created_for_a_clean_path(tmp_path):
    conn = connect_index_forecast(tmp_path / "index_forecast.sqlite")
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='index_eod'").fetchone()[0] == "index_eod"
    conn.close()

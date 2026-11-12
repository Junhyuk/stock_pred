"""Closed-session price append without a network call."""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from roboquant.index_forecast.sqlite_store import connect_index_forecast
from roboquant.index_forecast.us_market_inputs import US_SYMBOLS, normalize_us_observations

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "refresh_premarket_prices.py"
SPEC = importlib.util.spec_from_file_location("refresh_premarket_prices", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
refresh = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = refresh
SPEC.loader.exec_module(refresh)

KST = ZoneInfo("Asia/Seoul")


def test_timezone_aware_yahoo_bar_uses_the_seoul_session_date():
    frame = pd.DataFrame(
        {"Open": [1.0], "High": [1.0], "Low": [1.0], "Close": [5059.0], "Volume": [1.0]},
        index=pd.to_datetime(["2026-11-09 15:00:00+00:00"]),
    )
    frame.index.name = "Date"
    bars = refresh.bars_from_download(frame)
    assert bars["session_date"].tolist() == ["2026-11-10"]


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


def _seed_backfill(conn, *, sessions: int = 1000) -> None:
    start = datetime(2019, 1, 2)
    rows = []
    for offset in range(sessions):
        day = (start + timedelta(days=offset)).date().isoformat()
        for index_code in ("KOSPI", "KOSDAQ"):
            rows.append((index_code, day, 10.0, "seed", None, "2019-01-02T10:00:00+00:00"))
    conn.executemany(
        "INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


class _RecordingYahoo:
    def __init__(self):
        self.calls = []

    def download(self, symbol, start, end, **kwargs):
        self.calls.append((symbol, start, end))
        return pd.DataFrame()


def test_short_history_still_requests_the_2019_backfill(tmp_path):
    conn = connect_index_forecast(tmp_path / "index_forecast.sqlite")
    conn.execute(
        "INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
        "VALUES ('KOSPI', '2019-01-02', 1, 'seed', NULL, '2019-01-02T00:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
        "VALUES ('KOSDAQ', '2024-01-02', 1, 'seed', NULL, '2024-01-02T00:00:00+00:00')"
    )
    conn.commit()
    summary = refresh.kr_history_summary(conn)
    assert refresh.series_history_covered(summary, set(refresh.KR_SYMBOLS), requested_start="2019-01-01") is False
    assert refresh.incremental_start("2024-01-02", requested_start="2019-01-01", covered=False) == "2019-01-01"
    conn.close()
    yahoo = _RecordingYahoo()
    with pytest.raises(RuntimeError, match="no completed KOSPI/KOSDAQ"):
        refresh.refresh_premarket_prices(
            sqlite_path=tmp_path / "empty.sqlite", output_dir=tmp_path, yf_module=yahoo,
            fetched_at=datetime(2026, 11, 9, 7, 50, tzinfo=KST),
        )
    assert yahoo.calls == [("^KS11", "2019-01-01", "2026-11-10"), ("^KQ11", "2019-01-01", "2026-11-10")]


def test_cached_2019_history_appends_instead_of_rebuilding(tmp_path):
    db_path = tmp_path / "index_forecast.sqlite"
    conn = connect_index_forecast(db_path)
    _seed_backfill(conn)
    summary = refresh.kr_history_summary(conn)
    conn.close()
    expected = refresh.incremental_start(
        refresh.lagging_latest(summary), requested_start="2019-01-01", covered=True)
    assert expected != "2019-01-01"
    start = datetime(2019, 1, 2)
    rows = []
    for offset in range(1000):
        day = (start + timedelta(days=offset)).date().isoformat()
        for symbol in US_SYMBOLS:
            rows.append({"symbol": symbol, "session_date": day, "close": 10.0})
    prior = tmp_path / "us_market_observations_v2_20261106.csv"
    pd.DataFrame(rows).to_csv(prior, index=False)
    yahoo = _RecordingYahoo()
    fetched = datetime(2026, 11, 9, 7, 50, tzinfo=KST)
    result = refresh.refresh_premarket_prices(
        sqlite_path=db_path, output_dir=tmp_path, yf_module=yahoo, fetched_at=fetched)
    assert {start_day for _symbol, start_day, _end in yahoo.calls} == {expected}
    assert all(end == "2026-11-10" for _symbol, _start, end in yahoo.calls)
    assert [symbol for symbol, _start, _end in yahoo.calls] == ["^KS11", "^KQ11", *US_SYMBOLS]
    written = Path(result["us_csv"])
    assert written.name == "us_market_observations_v2_20261109.csv"
    assert "2019-01-02" in written.read_text(encoding="utf-8")
    stored = connect_index_forecast(db_path)
    assert stored.execute("SELECT COUNT(*) FROM index_eod").fetchone()[0] == 2000
    assert stored.execute("SELECT close FROM index_eod WHERE index_code='KOSPI' AND session_date='2019-01-02'").fetchone()[0] == 10
    stored.close()
    prior_text = prior.read_text(encoding="utf-8")
    assert prior_text.startswith("symbol,session_date,close")


def test_us_merge_keeps_an_existing_close_and_appends_a_new_session():
    old = pd.DataFrame([{
        "symbol": "^GSPC",
        "session_date": "2026-10-22",
        "close": 111.0,
        "source": refresh.YFINANCE_SOURCE,
        "source_url": "https://finance.yahoo.com/quote/%5EGSPC/history/",
        "fetched_at": "2026-10-24T12:00:00+00:00",
    }])
    prior = normalize_us_observations(old)["observations"]
    assert len(prior) == 1
    recent = pd.DataFrame([
        {**old.iloc[0].to_dict(), "close": 999.0},
        {**old.iloc[0].to_dict(), "session_date": "2026-10-23", "close": 5800.0},
    ])
    merged = refresh.merge_us_observations(prior, recent)
    by_day = dict(zip(merged["session_date"], merged["close"], strict=True))
    assert by_day["2026-10-22"] == pytest.approx(111.0)
    assert by_day["2026-10-23"] == pytest.approx(5800.0)


KR_SESSIONS = ["2026-11-11", "2026-11-12", "2026-11-13"]
# 2026-11-12 20:05:40 KST, after the cash close and before the next open.
NOV12_QUOTE_TIME = 1794481540


def _quote_metadata(**overrides):
    metadata = {
        "regularMarketPrice": 5342.73,
        "fulldayPrice": 5342.73,
        "regularMarketTime": NOV12_QUOTE_TIME,
        "regularMarketDayHigh": 0.0,
        "regularMarketDayLow": 0.0,
        "regularMarketVolume": 0,
    }
    metadata.update(overrides)
    return metadata


def _daily_frame(dates, closes):
    return pd.DataFrame(
        {
            "Open": [1.0] * len(dates),
            "High": [2.0] * len(dates),
            "Low": [0.5] * len(dates),
            "Close": closes,
            "Volume": [10.0] * len(dates),
        },
        index=pd.to_datetime(dates),
    )


class _QuoteYahoo:
    def __init__(self, frame, metadata):
        self.frame = frame
        self.metadata = metadata
        self.quote_calls = []

    def download(self, symbol, start, end, **kwargs):
        if symbol in refresh.KR_SYMBOLS.values():
            return self.frame
        return pd.DataFrame()

    def Ticker(self, symbol):
        self.quote_calls.append(symbol)
        metadata = self.metadata[symbol]

        class _Ticker:
            def get_history_metadata(_self):
                return metadata

        return _Ticker()


def test_quote_close_fills_a_completed_session_the_daily_history_omits():
    fetched = datetime(2026, 11, 13, 7, 50, tzinfo=KST)
    closed = refresh.latest_closed_session(fetched, KR_SESSIONS)
    assert closed["session_date"] == "2026-11-12"
    bar = refresh.quote_bar_for_session(_quote_metadata(), closed, fetched_at=fetched)
    assert bar == {
        "session_date": "2026-11-12",
        "open": None,
        "high": None,
        "low": None,
        "close": pytest.approx(5342.73),
        "volume": None,
    }
    preopen = int(datetime(2026, 11, 13, 7, 40, tzinfo=KST).timestamp())
    morning = refresh.quote_bar_for_session(
        _quote_metadata(regularMarketTime=preopen), closed, fetched_at=fetched)
    assert morning["session_date"] == "2026-11-12"
    assert morning["close"] == pytest.approx(5342.73)


def test_quote_during_the_session_or_after_the_next_open_is_not_a_close():
    fetched = datetime(2026, 11, 13, 7, 50, tzinfo=KST)
    closed = refresh.latest_closed_session(fetched, KR_SESSIONS)
    during = int(datetime(2026, 11, 12, 14, 0, tzinfo=KST).timestamp())
    assert refresh.quote_bar_for_session(
        _quote_metadata(regularMarketTime=during), closed, fetched_at=fetched) is None
    live_fetch = datetime(2026, 11, 13, 10, 0, tzinfo=KST)
    live_closed = refresh.latest_closed_session(live_fetch, KR_SESSIONS)
    assert live_closed["session_date"] == "2026-11-12"
    live_time = int(live_fetch.timestamp())
    assert refresh.quote_bar_for_session(
        _quote_metadata(regularMarketTime=live_time, regularMarketPrice=5400.0, fulldayPrice=5400.0),
        live_closed, fetched_at=live_fetch) is None
    assert refresh.quote_bar_for_session(
        _quote_metadata(fulldayPrice=5300.0), closed, fetched_at=fetched) is None
    assert refresh.quote_bar_for_session(
        _quote_metadata(regularMarketPrice=0), closed, fetched_at=fetched) is None


def test_refresh_appends_the_quote_close_and_keeps_an_existing_daily_bar(tmp_path, capsys):
    fetched = datetime(2026, 11, 13, 7, 50, tzinfo=KST)
    db_path = tmp_path / "index_forecast.sqlite"
    conn = connect_index_forecast(db_path)
    conn.execute(
        "INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
        "VALUES ('KOSPI', '2026-11-11', 1.5, 'existing-source', '2026-11-11', '2026-11-11T08:00:00+00:00')"
    )
    conn.commit()
    conn.close()
    yahoo = _QuoteYahoo(
        _daily_frame(["2026-11-11"], [5375.36]),
        {
            "^KS11": _quote_metadata(),
            "^KQ11": _quote_metadata(regularMarketPrice=696.53, fulldayPrice=696.53),
        },
    )
    with pytest.raises(ValueError, match="no completed US bars"):
        refresh.refresh_premarket_prices(
            sqlite_path=db_path, output_dir=tmp_path, yf_module=yahoo, fetched_at=fetched, sessions=KR_SESSIONS)
    assert yahoo.quote_calls == ["^KS11", "^KQ11"]
    assert "quote close was not used" not in capsys.readouterr().err
    stored = connect_index_forecast(db_path)
    kospi_old = stored.execute(
        "SELECT close, source FROM index_eod WHERE index_code='KOSPI' AND session_date='2026-11-11'"
    ).fetchone()
    assert kospi_old[0] == 1.5
    assert kospi_old[1] == "existing-source"
    kospi = stored.execute(
        "SELECT open, high, low, close, volume, turnover, published_at, source "
        "FROM index_eod WHERE index_code='KOSPI' AND session_date='2026-11-12'"
    ).fetchone()
    assert kospi[:7] == (None, None, None, pytest.approx(5342.73), None, None, None)
    assert kospi[7] == "yfinance ^KS11; original publication time is unknown"
    kosdaq = stored.execute(
        "SELECT close, volume, published_at, source FROM index_eod WHERE index_code='KOSDAQ' AND session_date='2026-11-12'"
    ).fetchone()
    assert kosdaq[0] == pytest.approx(696.53)
    assert kosdaq[1] is None and kosdaq[2] is None
    assert "yfinance ^KQ11" in kosdaq[3]
    assert stored.execute("SELECT COUNT(*) FROM investor_flow").fetchone()[0] == 0
    codes = {row[0] for row in stored.execute("SELECT DISTINCT index_code FROM index_eod")}
    assert codes == {"KOSPI", "KOSDAQ"}
    stored.close()

    already = _QuoteYahoo(
        _daily_frame(["2026-11-11", "2026-11-12"], [5375.36, 111.0]),
        {"^KS11": _quote_metadata(regularMarketPrice=999.0, fulldayPrice=999.0),
         "^KQ11": _quote_metadata(regularMarketPrice=999.0, fulldayPrice=999.0)},
    )
    fresh = tmp_path / "fresh.sqlite"
    with pytest.raises(ValueError, match="no completed US bars"):
        refresh.refresh_premarket_prices(
            sqlite_path=fresh, output_dir=tmp_path, yf_module=already, fetched_at=fetched, sessions=KR_SESSIONS)
    assert already.quote_calls == []
    saved = connect_index_forecast(fresh)
    closes = {
        (row[0], row[1]): row[2]
        for row in saved.execute("SELECT index_code, session_date, close FROM index_eod")
    }
    assert closes[("KOSPI", "2026-11-12")] == 111.0
    assert closes[("KOSDAQ", "2026-11-12")] == 111.0
    saved.close()


def test_unusable_quote_does_not_invent_a_close(tmp_path, capsys):
    fetched = datetime(2026, 11, 13, 7, 50, tzinfo=KST)
    yahoo = _QuoteYahoo(
        _daily_frame(["2026-11-11"], [5375.36]),
        {"^KS11": _quote_metadata(fulldayPrice=5300.0), "^KQ11": _quote_metadata(regularMarketPrice=0)},
    )
    with pytest.raises(ValueError, match="no completed US bars"):
        refresh.refresh_premarket_prices(
            sqlite_path=tmp_path / "index_forecast.sqlite", output_dir=tmp_path,
            yf_module=yahoo, fetched_at=fetched, sessions=KR_SESSIONS)
    err = capsys.readouterr().err
    assert "yahoo daily history omits ^KS11 2026-11-12; quote close was not used" in err
    assert "yahoo daily history omits ^KQ11 2026-11-12; quote close was not used" in err
    stored = connect_index_forecast(tmp_path / "index_forecast.sqlite")
    dates = {row[0] for row in stored.execute("SELECT session_date FROM index_eod")}
    assert "2026-11-12" not in dates
    stored.close()


def test_existing_us_snapshot_is_not_rewritten_when_it_already_covers_the_merge(tmp_path):
    old = pd.DataFrame([{
        "symbol": "^GSPC",
        "session_date": "2026-10-22",
        "close": 111.0,
        "source": refresh.YFINANCE_SOURCE,
        "source_url": "https://finance.yahoo.com/quote/%5EGSPC/history/",
        "fetched_at": "2026-10-24T12:00:00+00:00",
    }])
    destination = tmp_path / "us_market_observations_v2_20261024.csv"
    original = old.to_csv(index=False).encode("utf-8")
    destination.write_bytes(original)
    changed = old.copy()
    changed.loc[0, "close"] = 999.0
    written = refresh.write_merged_us_snapshot(old, changed, destination)
    assert written.read_bytes() == original
    added = pd.concat([changed, changed.assign(session_date="2026-10-23", close=5800.0)], ignore_index=True)
    with pytest.raises(ValueError, match="immutable"):
        refresh.write_merged_us_snapshot(old, added, destination)
    assert destination.read_bytes() == original

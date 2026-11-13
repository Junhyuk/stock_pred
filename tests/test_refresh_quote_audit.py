"""Adversarial tests for a missing latest-session close in Yahoo daily bars."""
import importlib.util
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('refresh_quote_audit', ROOT / 'scripts/refresh_premarket_prices.py')
refresh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(refresh)
KST = ZoneInfo('Asia/Seoul')
SESSIONS = ['2026-11-12', '2026-11-13', '2026-11-16']
FETCHED = datetime(2026, 11, 14, 1, 30, tzinfo=KST)
QUOTE_TIME = int(datetime(2026, 11, 13, 20, 0, tzinfo=KST).timestamp())


class Yahoo:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = 0

    def Ticker(self, symbol):
        self.calls += 1
        owner = self

        class Ticker:
            def get_history_metadata(self):
                return owner.metadata

        return Ticker()


def _metadata(**changes):
    return {'regularMarketPrice': 5342.73, 'fulldayPrice': 5342.73,
            'regularMarketTime': QUOTE_TIME, **changes}


def _bars(last_close):
    return pd.DataFrame([{'session_date': '2026-11-12', 'close': 5200.0},
                         {'session_date': '2026-11-13', 'close': last_close}])


@pytest.mark.parametrize('invalid_close', [np.nan, 0.0, np.inf, -np.inf])
def test_invalid_existing_latest_close_is_replaced_only_by_valid_close_quote(invalid_close):
    yahoo = Yahoo(_metadata())
    bars = _bars(invalid_close)
    before = bars.copy(deep=True)
    supplemented = refresh.supplement_kr_bars(bars, '^KS11', yahoo, fetched_at=FETCHED, sessions=SESSIONS)
    rows = refresh.completed_kr_rows(supplemented, index_code='KOSPI', fetched_at=FETCHED, sessions=SESSIONS)
    latest = [row for row in rows if row['session_date'] == '2026-11-13']
    assert yahoo.calls == 1
    assert len(latest) == 1 and latest[0]['close'] == pytest.approx(5342.73)
    assert latest[0]['published_at'] is None
    pd.testing.assert_frame_equal(bars, before)


def test_valid_existing_close_remains_unchanged_and_does_not_request_quote():
    yahoo = Yahoo(_metadata(regularMarketPrice=9999, fulldayPrice=9999))
    bars = _bars(5320.0)
    supplemented = refresh.supplement_kr_bars(bars, '^KS11', yahoo, fetched_at=FETCHED, sessions=SESSIONS)
    assert yahoo.calls == 0
    assert supplemented.loc[supplemented.session_date == '2026-11-13', 'close'].iloc[0] == 5320.0


@pytest.mark.parametrize('bad_time', [
    int(datetime(2026, 11, 13, 14, 0, tzinfo=KST).timestamp()),
    int(datetime(2026, 11, 16, 9, 0, tzinfo=KST).timestamp()),
    int(datetime(2026, 11, 14, 2, 0, tzinfo=KST).timestamp()),
])
def test_unverified_quote_time_cannot_fill_latest_placeholder(bad_time):
    yahoo = Yahoo(_metadata(regularMarketTime=bad_time))
    supplemented = refresh.supplement_kr_bars(_bars(np.nan), '^KS11', yahoo, fetched_at=FETCHED, sessions=SESSIONS)
    rows = refresh.completed_kr_rows(supplemented, index_code='KOSPI', fetched_at=FETCHED, sessions=SESSIONS)
    assert not any(row['session_date'] == '2026-11-13' for row in rows)


@pytest.mark.parametrize('invalid_close', [np.nan, np.inf])
@pytest.mark.parametrize('changes', [
    {'regularMarketPrice': np.inf, 'fulldayPrice': None},
    {'regularMarketPrice': 5342.73, 'fulldayPrice': 5300.0},
    {'regularMarketPrice': 5342.73, 'fulldayPrice': np.inf},
])
def test_bad_quote_or_infinite_placeholder_never_produces_a_valid_close(invalid_close, changes):
    yahoo = Yahoo(_metadata(**changes))
    supplemented = refresh.supplement_kr_bars(_bars(invalid_close), '^KS11', yahoo, fetched_at=FETCHED, sessions=SESSIONS)
    rows = refresh.completed_kr_rows(supplemented, index_code='KOSPI', fetched_at=FETCHED, sessions=SESSIONS)
    assert not any(row['session_date'] == '2026-11-13' for row in rows)


def test_placeholder_recovery_appends_new_session_without_replacing_existing_db_row(tmp_path):
    from roboquant.index_forecast.sqlite_store import connect_index_forecast

    conn = connect_index_forecast(tmp_path / 'prices.sqlite')
    try:
        conn.execute("INSERT INTO index_eod (index_code, session_date, close, source, published_at, ingested_at) "
                     "VALUES ('KOSPI','2026-11-12',5100,'existing',NULL,'2026-11-12T10:00:00+00:00')")
        conn.commit()
        bars = refresh.supplement_kr_bars(_bars(np.nan), '^KS11', Yahoo(_metadata()),
                                          fetched_at=FETCHED, sessions=SESSIONS)
        rows = refresh.completed_kr_rows(bars, index_code='KOSPI', fetched_at=FETCHED, sessions=SESSIONS)
        assert refresh.append_kr_rows(conn, rows) == 1
        old = conn.execute("SELECT close, source FROM index_eod WHERE index_code='KOSPI' AND session_date='2026-11-12'").fetchone()
        new = conn.execute("SELECT close, published_at, source FROM index_eod WHERE index_code='KOSPI' AND session_date='2026-11-13'").fetchone()
        assert old[0] == 5100 and old[1] == 'existing'
        assert new[0] == pytest.approx(5342.73) and new[1] is None
        assert 'original publication time is unknown' in new[2]
    finally:
        conn.close()

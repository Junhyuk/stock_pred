import sqlite3

import pandas as pd
import pytest

from roboquant.index_forecast.session_input_store import append_inputs, read_inputs
from scripts.import_robo_session_inputs import kofia_credit_rows


def row(**overrides):
    return {
        "market": "GLOBAL",
        "series": "US_YIELD_3Y",
        "observation_date": "2026-11-17",
        "value": 3.65,
        "unit": "percent",
        "source": "Treasury",
        "received_at": "2026-11-19T18:20:00+09:00",
        "published_at": "",
        "is_final": True,
        "availability_policy": "historical_dated_yield",
        **overrides,
    }


def test_append_keeps_first_receipt_and_preserves_revisions(tmp_path):
    db = tmp_path / "inputs.sqlite"
    assert append_inputs(db, pd.DataFrame([row()])) == 1
    assert append_inputs(db, pd.DataFrame([row(received_at="2026-11-19T18:40:00+09:00")])) == 0
    assert append_inputs(db, pd.DataFrame([row(value=3.67, received_at="2026-11-19T18:41:00+09:00")])) == 1
    known = read_inputs(db, as_of="2026-11-19T18:30:00+09:00")
    assert known["value"].tolist() == [3.65]
    assert pd.Timestamp(known.iloc[0]["received_at"]) == pd.Timestamp("2026-11-19T09:20:00Z")
    all_rows = read_inputs(db, as_of="2026-11-19T18:41:00+09:00")
    assert all_rows["value"].tolist() == [3.65, 3.67]


def test_append_preserves_a_newly_discovered_earlier_receipt(tmp_path):
    db = tmp_path / "inputs.sqlite"
    assert append_inputs(db, pd.DataFrame([row()])) == 1
    earlier = pd.DataFrame([row(received_at="2026-11-19T18:10:00+09:00")])
    assert append_inputs(db, earlier) == 1
    assert append_inputs(db, earlier) == 0
    known = read_inputs(db, as_of="2026-11-19T18:15:00+09:00")
    assert known["value"].tolist() == [3.65]


def test_units_scopes_and_policy_are_checked_before_db_write(tmp_path):
    db = tmp_path / "inputs.sqlite"
    with pytest.raises(ValueError, match="unit"):
        append_inputs(db, pd.DataFrame([row(), row(unit="bps")]))
    assert not db.exists()
    with pytest.raises(ValueError, match="market"):
        append_inputs(db, pd.DataFrame([row(market="KOSPI")]))
    with pytest.raises(ValueError, match="policy"):
        append_inputs(db, pd.DataFrame([row(series="FOREIGN_NET", market="KOSPI", unit="KRW")]))


def test_missing_timestamps_and_early_receipts_are_invalid(tmp_path):
    db = tmp_path / "inputs.sqlite"
    with pytest.raises(ValueError, match="timezone"):
        append_inputs(db, pd.DataFrame([row(received_at="2026-11-19T09:00:00")]))
    with pytest.raises(ValueError, match="Publication"):
        append_inputs(db, pd.DataFrame([row(published_at="2026-11-20T13:00:00+09:00")]))
    with pytest.raises(ValueError, match="Receipt"):
        append_inputs(db, pd.DataFrame([row(observation_date="2026-11-20")]))


def test_unverified_flow_can_be_archived_and_cannot_be_rewritten(tmp_path):
    db = tmp_path / "inputs.sqlite"
    flow = row(market="KOSDAQ", series="FOREIGN_NET", unit="KRW", value=-12_000_000,
               is_final=False, availability_policy="observed")
    assert append_inputs(db, pd.DataFrame([flow])) == 1
    assert read_inputs(db, as_of="2026-11-19T18:20:00+09:00")["is_final"].tolist() == [False]
    with sqlite3.connect(db) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE session_input_observations SET is_final = 1")


def test_kofia_credit_export_retains_market_scope_and_won_units(tmp_path):
    header = [
        ("신용공여 잔고 추이",), ("[단위 :백만원]",),
        ("구 분", "신용거래융자"), (None, "전체", "유가증권", "코스닥"),
    ]
    valid = ("2026/11/18", "26,266,366", "20,323,672", "5,942,694")
    received_at = "2026-11-19T21:17:37+09:00"
    parsed = kofia_credit_rows(iter([*header, valid]), received_at)
    db = tmp_path / "inputs.sqlite"
    assert append_inputs(db, parsed) == 3
    stored = read_inputs(db, as_of=received_at).set_index("market")
    assert stored["value"].to_dict() == {
        "GLOBAL": 26_266_366_000_000, "KOSPI": 20_323_672_000_000, "KOSDAQ": 5_942_694_000_000,
    }
    assert stored["published_at"].isna().all()
    assert stored["is_final"].isna().all()
    with pytest.raises(ValueError, match="inconsistent"):
        kofia_credit_rows(iter([*header, valid, valid]), received_at)
    with pytest.raises(ValueError, match="policy"):
        append_inputs(db, parsed.assign(source="external_feed"))


def test_append_rejects_an_existing_forecast_db(tmp_path):
    db = tmp_path / "index_forecast.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE index_eod (close REAL)")
    with pytest.raises(ValueError, match="dedicated"):
        append_inputs(db, pd.DataFrame([row()]))
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("index_eod",)]

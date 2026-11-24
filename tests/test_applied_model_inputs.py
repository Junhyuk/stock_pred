import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_applied_model_inputs", ROOT / "scripts" / "collect_applied_model_inputs.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_stored_series_requests_only_the_missing_tail():
    assert MODULE.fetch_plan("2026-11-23", "2026-11-23") == ("skip", None)
    assert MODULE.fetch_plan(None, "2026-11-23") == ("full", None)
    mode, start = MODULE.fetch_plan("2026-11-20", "2026-11-23")
    assert mode == "range" and start == "2026-11-17"
    # A stopped source (latest 10/30) is asked again every run, never marked as checked.
    assert MODULE.fetch_plan("2026-10-30", "2026-11-23", overlap_days=0) == ("range", "2026-10-30")


def test_quiet_runs_never_rewrite_first_seen_times(monkeypatch):
    import sqlite3

    conn = sqlite3.connect(":memory:")
    MODULE._ensure_tables(conn)
    seen = "2026-11-20T10:00:00+00:00"
    for code, day in (("US_TREASURY_10Y", "2026-11-20"), ("JP_JGB_10Y", "2026-10-30")):
        conn.execute(
            "INSERT INTO macro_series (series_code, observation_date, value, source, ingested_at) VALUES (?, ?, 1.0, 'x', ?)",
            (code, day, seen))
    monkeypatch.setattr(MODULE, "treasury_yields", lambda year: {})
    monkeypatch.setattr(MODULE, "japan_yields", lambda start=None: {})
    assert MODULE._treasury_series(conn, "2026-11-23") == {}
    assert MODULE._japan_series(conn, "2026-11-23") == {}
    assert {row[0] for row in conn.execute("SELECT ingested_at FROM macro_series")} == {seen}


def test_naver_index_parser_rejects_a_broken_range(monkeypatch):
    payload = [
        {"localTradedAt": "2026-11-23", "openPrice": "4,734.90", "highPrice": "4,759.88",
         "lowPrice": "4,586.80", "closePrice": "4,708.54"},
        {"localTradedAt": "2026-11-20", "openPrice": "10", "highPrice": "9", "lowPrice": "8", "closePrice": "9"},
    ]

    def fake_fetch(url, timeout=40):
        assert "KOSPI" in url
        return json.dumps(payload).encode()

    monkeypatch.setattr(MODULE, "fetch", fake_fetch)
    bars = MODULE.naver_index_bars("KOSPI")
    assert bars["2026-11-23"]["close"] == 4708.54
    assert "2026-11-20" not in bars


def test_ecos_block_separates_empty_range_from_refusal():
    block = {"list_total_count": 1, "row": []}
    assert MODULE.ecos_block({"StatisticSearch": block}) is block
    assert MODULE.ecos_block({"RESULT": {"CODE": "INFO-200", "MESSAGE": "no data"}}) is None
    try:
        MODULE.ecos_block({"RESULT": {"CODE": "ERROR-301", "MESSAGE": "bad count"}})
    except RuntimeError as exc:
        assert "ERROR-301" in str(exc)
    else:
        raise AssertionError("a refusal must raise with the ECOS code, not KeyError")


def test_one_failing_source_keeps_the_others_and_is_reported(monkeypatch, tmp_path, capsys):
    import sqlite3
    import sys

    db = tmp_path / "inputs.sqlite"
    monkeypatch.setattr(MODULE, "insert_index_bars", lambda conn, ingested_at, today: 0)
    monkeypatch.setattr(MODULE, "_yahoo_series", lambda conn, today: {"YAHOO_DXY": [("2026-11-20", 100.5)]})
    monkeypatch.setattr(MODULE, "_treasury_series", lambda conn, today: {})
    monkeypatch.setattr(MODULE, "_japan_series", lambda conn, today: {})

    def refused(conn, today):
        raise RuntimeError("ECOS returned ERROR-301: refused")

    monkeypatch.setattr(MODULE, "_korea_series", refused)
    result = MODULE.collect(db)
    assert result["yahoo_levels"] == 1 and result["kr_yields"] is None
    assert result["errors"] == {"kr_yields": "RuntimeError: ECOS returned ERROR-301: refused"}
    stored = sqlite3.connect(db).execute("SELECT series_code, value FROM macro_series").fetchall()
    assert stored == [("YAHOO_DXY", 100.5)]  # the earlier source was committed, not rolled back

    monkeypatch.setattr(sys, "argv", ["collect_applied_model_inputs.py", "--sqlite", str(db)])
    assert MODULE.main() == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].startswith("::warning title=collect_applied_model_inputs::kr_yields failed")
    assert json.loads(lines[-1])["errors"]["kr_yields"].startswith("RuntimeError")

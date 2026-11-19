import json
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "export_margin_credit_chart.py"
SOURCE = "kofia_freesis_margin_credit"
CUTOFF = "2026-11-19T22:00:00+09:00"


def run_export(db, output):
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sqlite", str(db), "--output", str(output), "--as-of", CUTOFF],
        capture_output=True, text=True, check=False,
    )


def populate(db, rows):
    with sqlite3.connect(db) as connection:
        connection.execute("""CREATE TABLE session_input_observations (
            market TEXT, series TEXT, observation_date TEXT, value REAL, unit TEXT,
            source TEXT, received_at TEXT, published_at TEXT, is_final INTEGER
        )""")
        connection.executemany("INSERT INTO session_input_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def observation(market, day, balance, *, source=SOURCE, received="2026-11-19T12:00:00.000000+00:00",
                confirmed=None, series="MARGIN_CREDIT_BALANCE"):
    return market, series, day, balance, "KRW", source, received, None, confirmed


def test_public_export_contains_only_recent_official_balances_by_market(tmp_path):
    db, output = tmp_path / "input.sqlite", tmp_path / "page.json"
    dates = [(date(2026, 5, 1) + timedelta(days=i)).isoformat() for i in range(92)]
    rows = [observation(market, day, 1_000_000_000 + i)
            for market in ("KOSPI", "KOSDAQ") for i, day in enumerate(dates)]
    rows += [
        observation("KOSPI", dates[-1], 2_000_000_000, received="2026-11-19T21:15:00+09:00"),
        observation("KOSPI", dates[-1], 900, received="2026-11-19T12:59:00-05:00"),
        observation("KOSPI", dates[-1], 900, received="2026-11-19T14:01:00.000000+00:00"),
        observation("KOSPI", "2026-11-18", 900, received="2026-11-19T12:30:00"),
        observation("KOSDAQ", "2026-11-18", 900, source="different_source"),
        observation("KOSDAQ", "2026-11-18", 900, confirmed=0),
        observation("KOSDAQ", "2026-11-18", 900, series="FOREIGN_NET"),
        observation("GLOBAL", "2026-11-18", 900),
    ]
    populate(db, rows)
    before = db.read_bytes()
    result = run_export(db, output)
    assert result.returncode == 0, result.stderr
    data = json.loads(output.read_bytes())
    assert data["schema_version"] == "margin_credit_chart_v1"
    assert data["snapshot_received_at"] == "2026-11-19T12:15:00.000000+00:00"
    assert data["unit"] == "KRW"
    assert set(data["markets"]) == {"KOSPI", "KOSDAQ"}
    assert [row["date"] for row in data["markets"]["KOSDAQ"]] == dates[-90:]
    assert data["markets"]["KOSPI"][-1] == {"date": dates[-1], "balance_krw": 2_000_000_000}
    assert db.read_bytes() == before


def test_missing_market_leaves_last_publishable_snapshot_in_place(tmp_path):
    db, output = tmp_path / "input.sqlite", tmp_path / "page.json"
    populate(db, [observation("KOSPI", "2026-11-18", 100)])
    output.write_text('{"previous":true}', encoding="utf-8")
    result = run_export(db, output)
    assert result.returncode != 0
    assert "KOSDAQ" in result.stderr
    assert output.read_text(encoding="utf-8") == '{"previous":true}'

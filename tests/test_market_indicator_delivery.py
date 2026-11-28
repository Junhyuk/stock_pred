import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from scripts.export_market_indicator_catalog import build_catalog
from scripts.stage_market_indicator_pages import stage_database

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/data"
DB = "market-indicators-v1.sqlite"
MANIFEST = "market-indicators-v1.manifest.json"


def invoke(script, *args):
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def export(folder, *args):
    return invoke("export_market_indicator_sqlite.py", "--output-dir", folder, *args)


def test_real_public_sqlite_is_reproducible_with_complete_catalog_and_treasury(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for folder in (first, second):
        run = export(folder)
        assert run.returncode == 0, run.stderr
    raw = (first / DB).read_bytes()
    assert raw == (second / DB).read_bytes()
    catalog = json.loads((DATA / "market-indicator-catalog.json").read_text())
    manifest = json.loads((first / MANIFEST).read_text())
    assert manifest == dict(
        schema_version=1,
        generated_at=catalog["generated_at"],
        size_bytes=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    with sqlite3.connect(first / DB) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (1,)
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("SELECT count(*) FROM series").fetchone() == (len(catalog["series"]),)
        assert set(
            x[0] for x in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ) == {"meta", "series", "observations", "events", "releases", "model_links"}
        for item in catalog["series"]:
            meta, first_day, last_day = db.execute(
                "SELECT metadata_json,first_date,last_date FROM series WHERE id=?", (item["id"],)
            ).fetchone()
            expected_meta = {k: v for k, v in item.items() if k not in ("points", "missing_dates")}
            assert json.loads(meta) == expected_meta
            assert "points" not in json.loads(meta)
            rows = dict(
                db.execute("SELECT date,value FROM observations WHERE series_id=?", (item["id"],))
            )
            assert all(rows[d] == v for d, v in item["points"])
            assert all(rows[d] is None for d in item["missing_dates"])
            assert (first_day, last_day) == (
                min((d for d in rows if rows[d] is not None), default=None),
                max((d for d in rows if rows[d] is not None), default=None),
            )
        for table in ("releases", "events", "model_links"):
            assert [
                json.loads(x[0])
                for x in db.execute(f"SELECT payload_json FROM {table} ORDER BY ordinal")
            ] == catalog[table]
        assert db.execute(
            "SELECT date,value FROM observations WHERE series_id='us_10y' ORDER BY date LIMIT 1"
        ).fetchone() == ("2021-01-04", 0.93)
        assert db.execute(
            "SELECT COUNT(*) FROM observations WHERE series_id='us_1y' AND date<'2023-11-28'"
        ).fetchone() == (727,)
        assert db.execute(
            "SELECT COUNT(*) FROM observations WHERE series_id='kospi' AND date<'2025-11-24'"
        ).fetchone() == (0,)


def test_catalog_archives_only_rolled_off_same_definition_and_retains_verification(tmp_path):
    def write(name, payload):
        (tmp_path / name).write_text(json.dumps(payload))

    meta = dict(
        label="일본 기준금리",
        unit="percent",
        source="BOJ",
        date_basis="effective_date",
        verified_through="2026-11-18",
        release_dates={"2026-11-16": "2026-11-17"},
    )

    def collect(points, source="BOJ"):
        write(
            "market-chart-history.json",
            dict(
                as_of_date="2026-11-19",
                series={"jp_policy_rate": dict(meta, source=source, points=points)},
            ),
        )
        out = build_catalog(tmp_path)
        write("market-indicator-catalog.json", out)
        return out

    collect([["2026-11-16", 0.5], ["2026-11-17", None], ["2026-11-18", 0.75]])
    new = collect([["2026-11-18", 1.0], ["2026-11-19", 1.0]])
    assert new["as_of_date"] == "2026-11-19"
    assert new["series"][0]["verified_through"] == "2026-11-18"
    assert new["series"][0]["country"] == "JP"
    assert new["observation_archive"]["jp_policy_rate"]["points"] == [
        ["2026-11-16", 0.5],
        ["2026-11-17", None],
    ]
    same = collect([["2026-11-19", 1]])
    assert same["observation_archive"]["jp_policy_rate"]["points"][-1] == ["2026-11-18", 1]
    assert not collect([["2026-11-19", 1]], "changed")["observation_archive"]


def test_pages_generation_failure_only_uses_validated_previous_pair(tmp_path):

    live, output = tmp_path / "previous", tmp_path / "output"
    run = export(live)
    assert run.returncode == 0, run.stderr
    # A file URL keeps the test offline and exercises the same urllib requests as Pages.
    stage_database(tmp_path / "broken", output, live.as_uri())
    assert (output / DB).read_bytes() == (live / DB).read_bytes()
    assert (output / MANIFEST).read_bytes() == (live / MANIFEST).read_bytes()
    (live / DB).write_bytes(b"broken snapshot")

    with pytest.raises(ValueError):
        stage_database(tmp_path / "broken", tmp_path / "rejected", live.as_uri())
    assert not (tmp_path / "rejected" / MANIFEST).exists()


def test_local_qa_uses_read_only_index_closes_and_cannot_be_staged(tmp_path):
    source = tmp_path / "private.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE index_eod(index_code,session_date,close,source)")
        db.execute(
            "INSERT INTO index_eod VALUES ('KOSPI','2019-01-02',2010,'duckdb_benchmark_daily')"
        )
        db.execute("INSERT INTO index_eod VALUES ('STOCK','2019-01-02',123,'private')")
    before = source.read_bytes()
    output = tmp_path / "qa"
    run = export(output, "--local-qa-sqlite", source)
    assert run.returncode == 0, run.stderr
    assert source.read_bytes() == before
    with sqlite3.connect(output / DB) as db:
        assert db.execute(
            "SELECT value FROM observations WHERE series_id='kospi' AND date='2019-01-02'"
        ).fetchone() == (2010,)
        assert db.execute(
            "SELECT COUNT(*) FROM observations WHERE series_id='STOCK'"
        ).fetchone() == (0,)

    with pytest.raises(ValueError):
        stage_database(tmp_path / "broken", tmp_path / "stage", output.as_uri())


def test_pages_rebuild_advances_conflicting_timestamp_and_reuses_identical_release(tmp_path):

    old, first, second = (tmp_path / name for name in ("old", "first", "second"))
    assert export(old).returncode == 0
    data = tmp_path / "data"
    data.mkdir()
    catalog = json.loads((DATA / "market-indicator-catalog.json").read_text())
    catalog["series"][0]["note"] = "A verified source correction"
    (data / "market-indicator-catalog.json").write_text(json.dumps(catalog))
    stage_database(data, first, old.as_uri())
    manifest = json.loads((first / MANIFEST).read_text())

    assert datetime.fromisoformat(manifest["generated_at"]) > datetime.fromisoformat(
        catalog["generated_at"]
    )
    stage_database(data, second, first.as_uri())
    assert (first / MANIFEST).read_bytes() == (second / MANIFEST).read_bytes()
    assert (first / DB).read_bytes() == (second / DB).read_bytes()


@pytest.mark.parametrize("fault", ["unavailable", "malformed", "missing_db", "wrong_digest"])
def test_pages_keeps_live_site_if_previous_release_cannot_be_verified(tmp_path, fault):
    prior = tmp_path / "previous"
    assert export(prior).returncode == 0
    manifest = json.loads((prior / MANIFEST).read_text())
    if fault == "unavailable":
        (prior / MANIFEST).unlink()  # file transport raises URLError, not an HTTP 404
    elif fault == "malformed":
        (prior / MANIFEST).write_text('{"generated_at":"2025-01-01T00:00:00Z"}')
    elif fault == "missing_db":
        (prior / DB).unlink()
    else:
        manifest["sha256"] = "0" * 64
        (prior / MANIFEST).write_text(json.dumps(manifest))
    target = tmp_path / "stage"
    with pytest.raises((ValueError, OSError)):
        stage_database(DATA, target, prior.as_uri())
    assert not target.exists()

#!/usr/bin/env python3
"""Build the native chart database from the reviewed indicator catalog and public seed."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_market_indicator_catalog import number, valid_date, validate_output

DATABASE = "market-indicators-v1.sqlite"
MANIFEST = "market-indicators-v1.manifest.json"
SEED = ROOT / "data/chart_seeds/us_treasury_2021_2023.csv"
MAX_SIZE = 64 * 1024 * 1024
DETAILS = ("events", "releases", "model_links")
COLUMNS = {
    "meta": ("key", "value"),
    "series": ("id", "metadata_json", "first_date", "last_date"),
    "observations": ("series_id", "date", "value"),
    **{table: ("ordinal", "payload_json") for table in DETAILS},
}


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def read_only(path):
    return closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True))


def treasury_seed():
    with SEED.open(newline="") as stream:
        reader = csv.DictReader(stream)
        ids = ["us_1y", "us_3y", "us_5y", "us_10y"]
        if reader.fieldnames != ["date", *ids]:
            raise ValueError("unknown Treasury seed format")
        result = {sid: {} for sid in ids}
        for row in reader:
            for sid in ids:
                result[sid][row["date"]] = float(row[sid])
        return result


def qa_closes(path):
    with read_only(path) as db:
        return {
            sid: dict(
                db.execute(
                    "SELECT session_date,close FROM index_eod WHERE index_code=? AND close>0",
                    (sid.upper(),),
                )
            )
            for sid in ("kospi", "kosdaq")
        }


def write_database(path, catalog, local_qa, generated_at):
    if catalog["schema_version"] != "market_indicator_catalog_v1":
        raise ValueError("unknown catalog schema")
    validate_output(catalog)
    meta = {
        "schema_version": "1",
        "generated_at": catalog["generated_at"],
        "as_of_date": catalog["as_of_date"],
        "scope": "local_qa" if local_qa else "public",
    }
    if (
        not valid_date(meta["as_of_date"])
        or datetime.fromisoformat(meta["generated_at"]).tzinfo is None
    ):
        raise ValueError("invalid catalog timestamps")
    if generated_at and generated_at != meta["generated_at"]:
        meta["catalog_generated_at"] = meta["generated_at"]
        meta["generated_at"] = generated_at
    seed = treasury_seed()
    private = qa_closes(local_qa) if local_qa else {}
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            PRAGMA user_version=1;
            PRAGMA foreign_keys=ON;
            CREATE TABLE meta(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL);
            CREATE TABLE series(id TEXT PRIMARY KEY NOT NULL,metadata_json TEXT NOT NULL,
                                first_date TEXT,last_date TEXT);
            CREATE TABLE observations(series_id TEXT NOT NULL REFERENCES series(id),date TEXT NOT NULL,
                                      value REAL,PRIMARY KEY(series_id,date)) WITHOUT ROWID;
        """)
        db.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
        for series in catalog["series"]:
            sid = series["id"]
            metadata = {k: v for k, v in series.items() if k not in ("points", "missing_dates")}
            archive = catalog.get("observation_archive", {}).get(sid, {})
            rows = {}
            if (
                sid in seed
                and series["source"] == "미국 재무부"
                and series["unit"] == "percent"
                and series["date_basis"] == "observation_date"
            ):
                rows.update(seed[sid])
            rows.update(archive.get("points", []))
            rows.update(series["points"])
            rows.update(dict.fromkeys(series["missing_dates"]))
            for key in ("release_dates", "release_urls"):
                if key in archive:
                    metadata[key] = {**archive[key], **metadata.get(key, {})}
            if sid in private and rows:
                start = min(rows)
                historical = {day: value for day, value in private[sid].items() if day < start}
                if historical:
                    rows.update(historical)
                    metadata["local_qa_before"] = start
                    metadata["note"] = (
                        (metadata.get("note") or "")
                        + f" {start} 이전은 배포 권한과 출처가 미확인된 로컬 검토용 종가입니다."
                    ).strip()
            if any(not valid_date(d) or (v is not None and not number(v)) for d, v in rows.items()):
                raise ValueError(f"invalid archived observation for {sid}")
            dates = [d for d, v in rows.items() if v is not None]
            db.execute(
                "INSERT INTO series VALUES (?,?,?,?)",
                (sid, compact(metadata), min(dates, default=None), max(dates, default=None)),
            )
            db.executemany(
                "INSERT INTO observations VALUES (?,?,?)", ((sid, d, rows[d]) for d in sorted(rows))
            )
        for table in DETAILS:
            db.execute(
                f"CREATE TABLE {table}(ordinal INTEGER PRIMARY KEY,payload_json TEXT NOT NULL)"
            )
            db.executemany(
                f"INSERT INTO {table} VALUES (?,?)",
                ((i, compact(row)) for i, row in enumerate(catalog[table])),
            )


def verify_pair(directory, scope="public"):
    manifest = json.loads((directory / MANIFEST).read_bytes())
    path = directory / DATABASE
    size = path.stat().st_size
    if (
        type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
        or type(manifest.get("size_bytes")) is not int
        or not 0 < size <= MAX_SIZE
        or size != manifest.get("size_bytes")
        or hashlib.sha256(path.read_bytes()).hexdigest() != manifest.get("sha256")
    ):
        raise ValueError("chart database size, version or checksum mismatch")
    with read_only(path) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        objects = db.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")
        if set(objects) != {("table", name) for name in COLUMNS}:
            raise ValueError("unexpected chart database objects")
        for table, columns in COLUMNS.items():
            if tuple(row[1] for row in db.execute(f"PRAGMA table_info({table})")) != columns:
                raise ValueError(f"wrong columns in chart table {table}")
        meta = dict(db.execute("SELECT key,value FROM meta"))
        if (
            db.execute("PRAGMA user_version").fetchone() != (1,)
            or meta.get("schema_version") != "1"
            or meta.get("scope") != scope
            or not valid_date(meta.get("as_of_date"))
            or meta.get("generated_at") != manifest.get("generated_at")
            or datetime.fromisoformat(meta["generated_at"]).tzinfo is None
            or db.execute("PRAGMA integrity_check").fetchone() != ("ok",)
            or db.execute("PRAGMA foreign_key_check").fetchone() is not None
            or db.execute("SELECT count(*) FROM series").fetchone()[0] == 0
            or db.execute("SELECT 1 FROM observations WHERE value IS NOT NULL LIMIT 1").fetchone()
            is None
        ):
            raise ValueError("invalid chart database contents")
    return manifest


def export(data_dir, output_dir, local_qa=None, generated_at=None):
    data_dir, output_dir = data_dir.resolve(), output_dir.resolve()
    if output_dir == data_dir or data_dir in output_dir.parents:
        raise ValueError("chart database must be built outside its source data directory")
    if local_qa and any((parent / ".git").exists() for parent in (output_dir, *output_dir.parents)):
        raise ValueError("local QA output must be outside a checkout")
    catalog = json.loads((data_dir / "market-indicator-catalog.json").read_bytes())
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output_dir) as scratch:
        stage = Path(scratch)
        database = stage / DATABASE
        write_database(database, catalog, local_qa, generated_at)
        raw = database.read_bytes()
        manifest = dict(
            schema_version=1,
            generated_at=generated_at or catalog["generated_at"],
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
        (stage / MANIFEST).write_text(compact(manifest), encoding="utf-8")
        verify_pair(stage, scope="local_qa" if local_qa else "public")
        for filename in (DATABASE, MANIFEST):
            (stage / filename).replace(output_dir / filename)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "docs/data")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--local-qa-sqlite",
        type=Path,
        help="Read older KOSPI/KOSDAQ closes for private local QA only",
    )
    args = parser.parse_args()
    print(compact(export(args.data_dir, args.output_dir, args.local_qa_sqlite)))

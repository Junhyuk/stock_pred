#!/usr/bin/env python3
"""Stage tracked Pages files with a freshly built or verified last-good native chart database."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_market_indicator_sqlite import (
    DATABASE,
    MANIFEST,
    MAX_SIZE,
    export,
    read_only,
    verify_pair,
)


def download(base_url, filename, destination, limit):
    with urlopen(f"{base_url.rstrip('/')}/{filename}", timeout=15) as response:
        data = response.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError(f"invalid response length for {filename}")
    (destination / filename).write_bytes(data)


def source_dates(directory):
    with read_only(directory / DATABASE) as db:
        meta = dict(db.execute("SELECT key,value FROM meta"))
    generated = datetime.fromisoformat(meta.get("catalog_generated_at", meta["generated_at"]))
    return meta["as_of_date"], generated


def prepare_publication(data_dir, stage, previous, base_url):
    try:
        candidate = export(data_dir, stage)
    except Exception:
        logging.exception("Chart generation failed, checking the previously deployed public pair")
        download(base_url, MANIFEST, previous, 4096)
        download(base_url, DATABASE, previous, MAX_SIZE)
        verify_pair(previous)
        return previous

    # A code-only rebuild (or a different SQLite runtime) can change bytes without
    # changing the source catalog's timestamp. The native client requires a new
    # timestamp for a different digest, and rejects replay of an older release.
    try:
        download(base_url, MANIFEST, previous, 4096)
    except HTTPError as error:
        if error.code == 404:
            return stage  # No prior manifest on first release.
        raise
    download(base_url, DATABASE, previous, MAX_SIZE)
    manifest = verify_pair(previous)
    as_of, source_time = source_dates(stage)
    previous_as_of, previous_source = source_dates(previous)
    if as_of < previous_as_of or source_time < previous_source:
        return previous
    last_time = datetime.fromisoformat(manifest["generated_at"])
    if manifest == candidate or last_time < source_time:
        return stage
    if last_time > source_time:
        replay = export(data_dir, stage, generated_at=manifest["generated_at"])
        if replay == manifest:
            return previous
    publication_time = max(
        datetime.now(UTC),
        source_time + timedelta(microseconds=1),
        last_time + timedelta(microseconds=1),
    )
    export(data_dir, stage, generated_at=publication_time.isoformat())
    return stage


def stage_database(data_dir, target, previous_base_url):
    with tempfile.TemporaryDirectory() as scratch:
        stage, previous = Path(scratch) / "candidate", Path(scratch) / "previous"
        previous.mkdir()
        pair = prepare_publication(data_dir, stage, previous, previous_base_url)
        target.mkdir(parents=True, exist_ok=True)
        for name in (DATABASE, MANIFEST):
            shutil.copyfile(pair / name, target / name)


def stage_pages(target, previous_base_url):
    # A fresh staging directory prevents a retry from picking up private/untracked files.
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Pages staging directory must be empty")
    names = subprocess.check_output(["git", "ls-files", "-z", "docs"], cwd=ROOT)
    for name in names.decode().split("\0"):
        if not name:
            continue
        source = ROOT / name
        if source.is_symlink() or source.suffix in {".sqlite", ".db", ".duckdb"}:
            raise ValueError(f"unexpected file in tracked Pages content: {name}")
        staged = target / source.relative_to(ROOT / "docs")
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, staged)
    stage_database(target / "data", target / "data", previous_base_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--previous-base-url", required=True, help="Previously deployed data/ URL")
    args = parser.parse_args()
    stage_pages(args.output_dir, args.previous_base_url)

import hashlib
import json

import pandas as pd
import pytest

from scripts.run_robo_stock_accuracy import audit_sessions, load_verified_archive, merge_closes


def test_archive_must_match_receipt_and_exist_at_audit_cutoff(tmp_path):
    archive = tmp_path / "archive.json"
    receipt = tmp_path / "receipt.json"
    run_id = "b" * 64
    archive.write_text(json.dumps({"run_id": run_id, "replay": False, "markets": []}))
    receipt.write_text(json.dumps({
        "schema_version": "reliable_publication_v1",
        "status": "research_only",
        "run_id": run_id,
        "replay": False,
        "publication_checked_at": "2026-11-19T09:20:00Z",
        "payload_checksums": {f"archive/{run_id}.json": hashlib.sha256(archive.read_bytes()).hexdigest()},
    }))
    cutoff = pd.Timestamp("2026-11-19T20:35:00+09:00")
    assert load_verified_archive(archive, receipt, cutoff)["run_id"] == run_id
    with pytest.raises(ValueError, match="cutoff"):
        load_verified_archive(archive, receipt, pd.Timestamp("2026-11-19T09:19:00Z"))
    archive.write_text(archive.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        load_verified_archive(archive, receipt, cutoff)


def test_audit_calendar_applies_official_csat_hours_without_adding_holidays():
    dates = audit_sessions(pd.Timestamp("2026-11-19T20:35:00+09:00")).set_index("date")
    assert dates.at["2026-11-19", "open_at"] == pd.Timestamp("2026-11-19T01:00:00Z")
    assert dates.at["2026-11-19", "close_at"] == pd.Timestamp("2026-11-19T07:30:00Z")
    assert dates.at["2024-11-14", "close_at"] == pd.Timestamp("2024-11-14T07:30:00Z")
    assert dates.at["2025-11-13", "close_at"] == pd.Timestamp("2025-11-13T07:30:00Z")
    assert dates.at["2026-11-18", "close_at"] == pd.Timestamp("2026-11-18T06:30:00Z")
    assert "2026-10-09" not in dates.index


def test_overlapping_price_conflicts_cannot_silently_replace_training_data():
    historical = pd.DataFrame([
        ("KOSPI", "2026-11-17", 4968.47998, "2026-11-17"),
        ("KOSPI", "2026-11-18", 5000.00, None),
    ], columns=["index_code", "session_date", "close", "published_at"])
    crosscheck = pd.DataFrame([
        ("KOSPI", "2026-11-17", 4968.48),
        ("KOSPI", "2026-11-18", 5067.44),
        ("KOSPI", "2026-11-19", 5137.89),
    ], columns=["index_code", "session_date", "close"])
    merged = merge_closes(historical, crosscheck, as_of=pd.Timestamp("2026-11-19T20:35:00+09:00")).set_index("session_date")
    assert merged.at["2026-11-17", "close"] == pytest.approx(4968.48)
    assert pd.isna(merged.at["2026-11-17", "published_at"])
    assert merged.at["2026-11-18", "status"] == "conflict"
    assert pd.isna(merged.at["2026-11-18", "close"])
    assert merged.at["2026-11-19", "close"] == pytest.approx(5137.89)


def test_crosscheck_rows_unavailable_at_cutoff_cannot_supply_or_dispute_prices():
    historical = pd.DataFrame([
        ("KOSPI", "2026-11-18", 5067.44),
    ], columns=["index_code", "session_date", "close"])
    crosscheck = pd.DataFrame([
        ("KOSPI", "2026-11-17", 4968.48, "2026-11-19T09:15:00Z", None),
        ("KOSPI", "2026-11-18", 5100.00, "2026-11-20T12:00:00Z", None),
        ("KOSPI", "2026-11-19", 5137.89, None, "2026-11-20T12:00:00Z"),
    ], columns=["index_code", "session_date", "close", "observed_at", "published_at"])
    merged = merge_closes(historical, crosscheck, as_of=pd.Timestamp("2026-11-19T10:00:00Z")).set_index("session_date")
    assert pd.Timestamp(merged.at["2026-11-17", "observed_at"]) == pd.Timestamp("2026-11-19T09:15:00Z")
    assert merged.at["2026-11-18", "close"] == 5067.44
    assert merged.at["2026-11-18", "status"] == "single_source"
    assert pd.isna(merged.at["2026-11-19", "close"])
    assert merged.at["2026-11-19", "status"] == "price_not_observed_as_of"

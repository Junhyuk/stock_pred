"""Integration contracts for actual-time, fail-closed research publication."""
import hashlib
import importlib.util
import json
import sqlite3
from datetime import UTC, datetime

import numpy as np
import pandas as pd

from roboquant.index_forecast.reliable import ReliableSettings


def pipeline():
    assert importlib.util.find_spec("roboquant.index_forecast.reliable_pipeline"), "reliable pipeline is missing"
    from roboquant.index_forecast import reliable_pipeline
    return reliable_pipeline


def config():
    return {"settings": {"initial_train_days": 25, "purge_days": 1, "retrain_step_days": 15,
            "calibration_window": 25, "min_calibration_rows": 8, "development_days": 20,
            "test_days": 20, "bootstrap_samples": 30, "bootstrap_block_days": 5,
            "min_oos_rows": 15, "model_families": ["linear"]}}


def database(tmp_path, n=140):
    dates = pd.bdate_range(end="2026-10-23", periods=n)
    rng = np.random.default_rng(23)
    prices = pd.DataFrame({"session_date": dates.strftime("%Y-%m-%d"),
                           "close": 2000 * np.exp(np.cumsum(rng.normal(0, .01, n))),
                           "source": "test-secondary", "published_at": None,
                           "ingested_at": "2026-10-23T13:00:00+00:00"})
    path = tmp_path / "research.sqlite"
    with sqlite3.connect(path) as conn:
        pd.concat([prices.assign(index_code=code) for code in ("KOSPI", "KOSDAQ")]).to_sql("index_eod", conn, index=False)
    return path, dates.append(pd.DatetimeIndex(["2026-10-26"]))


def test_missing_exchange_session_does_not_create_a_two_day_one_day_label():
    mod = pipeline()
    dates = pd.bdate_range("2026-07-01", periods=60)
    prices = pd.DataFrame({"session_date": dates, "close": np.arange(100., 160.)})
    missing = dates[40]
    frame = mod.build_reliable_price_frame(prices.loc[prices.session_date != missing], sessions=dates)
    prior = frame.loc[frame.asof_date == dates[39].date().isoformat()].iloc[0]
    assert pd.isna(prior.label_return)
    assert prior.target_date == missing.date().isoformat()
    assert pd.isna(frame.loc[frame.asof_date == dates[41].date().isoformat(), "return_1d"].iloc[0])


def test_stale_run_does_not_persist_current_forecast(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    report = mod.run_reliable_pipeline(path, tmp_path / "report", config=config(), sessions=sessions,
                now=datetime(2026, 10, 26, 14, tzinfo=UTC))
    assert report["status"] == "blocked_stale_input"
    assert not list((tmp_path / "report" / "archive").glob("*.json"))


def test_post_deadline_run_does_not_archive_an_0800_forecast(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    report = mod.run_reliable_pipeline(path, tmp_path / "late", config=config(), sessions=sessions,
                now=datetime(2026, 10, 26, 0, tzinfo=UTC))
    assert report["status"] == "blocked_issuance_deadline"
    assert not list((tmp_path / "late" / "archive").glob("*.json"))


def test_actual_run_crossing_issuance_deadline_does_not_archive(tmp_path, monkeypatch):
    mod = pipeline()
    path, sessions = database(tmp_path)
    ticks = [datetime(2026, 10, 25, 22, 59, tzinfo=UTC), datetime(2026, 10, 26, 0, tzinfo=UTC)]
    class Clock:
        @staticmethod
        def now(tz):
            return (ticks.pop(0) if len(ticks) > 1 else ticks[0]).astimezone(tz)
    monkeypatch.setattr(mod, "datetime", Clock)
    report = mod.run_reliable_pipeline(path, tmp_path / "crossed", config=config(), sessions=sessions)
    assert report["status"] == "blocked_issuance_deadline"
    assert report["publication_checked_at"] == "2026-10-26T00:00:00+00:00"
    assert report["artifact_written_at"] is None
    assert not list((tmp_path / "crossed" / "archive").glob("*.json"))


def test_preliminary_snapshot_completed_after_0800_keeps_honest_publication(tmp_path, monkeypatch):
    mod = pipeline()
    path, sessions = database(tmp_path)
    ticks = [datetime(2026, 10, 25, 22, 59, tzinfo=UTC), datetime(2026, 10, 25, 23, 1, tzinfo=UTC)]
    class Clock:
        @staticmethod
        def now(tz):
            return (ticks.pop(0) if len(ticks) > 1 else ticks[0]).astimezone(tz)
    monkeypatch.setattr(mod, "datetime", Clock)
    report = mod.run_reliable_pipeline(path, tmp_path / "preliminary", config=config(), sessions=sessions)
    assert report["status"] == "research_only"
    assert report["run_started_at"] == "2026-10-25T22:59:00+00:00"
    assert report["issued_at"] == report["artifact_written_at"] == "2026-10-25T23:01:00+00:00"
    for market in report["markets"]:
        assert market["context"]["stage"] == "preliminary"
        assert market["forecast"]["issued_at"] == report["issued_at"]
        assert market["context"]["prediction_cutoff"] == report["run_started_at"]


def test_open_crossing_during_large_serialization_cannot_publish(tmp_path, monkeypatch):
    mod = pipeline()
    path, sessions = database(tmp_path)
    state = {"first": True, "late": False}
    class Clock:
        @staticmethod
        def now(tz):
            if state["first"]:
                state["first"] = False
                return datetime(2026, 10, 25, 22, 59, tzinfo=UTC).astimezone(tz)
            return datetime(2026, 10, 26, 0, tzinfo=UTC).astimezone(tz) if state["late"] else datetime(2026, 10, 25, 23, 59, tzinfo=UTC).astimezone(tz)
    original = mod._json_text
    def slow_large_serialization(value):
        encoded = original(value)
        if isinstance(value, dict) and value.get("schema_version") == "reliable_index_forecast_v1" and value.get("run_id"):
            state["late"] = True
        return encoded
    monkeypatch.setattr(mod, "datetime", Clock)
    monkeypatch.setattr(mod, "_json_text", slow_large_serialization)
    output = tmp_path / "slow-serialization"
    report = mod.run_reliable_pipeline(path, output, config=config(), sessions=sessions)
    assert state["late"]
    assert report["status"] == "blocked_issuance_deadline"
    assert not list((output / "archive").glob("*.json"))
    assert not (output / "reliable_publication.json").exists()
    assert json.loads((output / "reliable_index_forecast.json").read_text())["status"] == "blocked_issuance_deadline"


def test_publication_manifest_is_authoritative_and_missing_manifest_is_unpublished(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    output = tmp_path / "manifest"
    report = mod.run_reliable_pipeline(path, output, config=config(), sessions=sessions,
                now=datetime(2026, 10, 23, 14, tzinfo=UTC))
    manifest = json.loads((output / "reliable_publication.json").read_text())
    assert manifest["run_id"] == report["run_id"]
    assert manifest["publication_checked_at"] == report["issued_at"]
    raw = json.loads((output / "reliable_index_forecast.json").read_text())
    assert raw["status"] == "unpublished" and raw["issued_at"] is None
    assert mod.resolve_reliable_publication(output)["status"] == "research_only"
    (output / "reliable_publication.json").unlink()
    unresolved = mod.resolve_reliable_publication(output)
    assert unresolved["status"] == "unpublished"
    assert unresolved["markets"] == []


def test_final_snapshot_uses_stage_matched_historical_0800_cutoffs(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    report = mod.run_reliable_pipeline(path, tmp_path / "final", config=config(), sessions=sessions,
                now=datetime(2026, 10, 25, 23, 30, tzinfo=UTC))
    for market in report["markets"]:
        assert market["context"]["stage"] == "premarket_final"
        assert market["context"]["prediction_cutoff"] == "2026-10-25T23:00:00+00:00"
        for row in market["evaluation"]["calibration_observations"]:
            cutoff = pd.Timestamp(row["prediction_cutoff"]).tz_convert("Asia/Seoul")
            assert cutoff.date().isoformat() == row["target_date"]
            assert cutoff.hour == 8 and cutoff.minute == 0


def test_saturday_preliminary_history_preserves_after_close_day_offset(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    report = mod.run_reliable_pipeline(path, tmp_path / "saturday", config=config(), sessions=sessions,
                now=datetime(2026, 10, 23, 15, 30, tzinfo=UTC))
    assert report["status"] == "research_only"
    for market in report["markets"]:
        assert market["context"]["asof_date"] == "2026-10-23"
        assert market["context"]["target_date"] == "2026-10-26"
        for row in market["evaluation"]["calibration_observations"]:
            cutoff = pd.Timestamp(row["prediction_cutoff"]).tz_convert("Asia/Seoul")
            assert cutoff.date() > pd.Timestamp(row["asof_date"]).date()
            assert cutoff.hour == 0 and cutoff.minute == 30


def test_verified_foreign_bridge_enters_forecast_candidates_without_pension(tmp_path):
    from roboquant.index_forecast.participant_impact import write_participant_bridge
    mod = pipeline()
    path, sessions = database(tmp_path)
    rows = [{"record_type": "flow", "scope": "market_total", "market": "KOSPI", "symbol": None,
             "investor_type": "FOREIGN", "observation_date": day.date().isoformat(),
             "net_value": float(10 + i), "turnover": 1000., "unit": "KRW", "source": "KRX_CSV",
             "source_url": "https://data.krx.co.kr/", "available_at": day.date().isoformat() + "T12:00:00+00:00",
             "ingested_at": day.date().isoformat() + "T12:00:00+00:00", "revision": "1", "is_final": True}
            for i, day in enumerate(sessions[:-1])]
    bridge = tmp_path / "participants.json"
    manifest = write_participant_bridge(bridge, rows)
    report = mod.run_reliable_pipeline(path, tmp_path / "impact", config=config(), sessions=sessions,
                now=datetime(2026, 10, 23, 14, tzinfo=UTC), participant_bridge=bridge)
    kospi = next(item for item in report["markets"] if item["market"] == "KOSPI")
    group = "price_participant_market_foreign_raw"
    assert group in kospi["active_feature_groups"]
    assert any(row["group"] == group for row in kospi["evaluation"]["candidate_reports"])
    assert kospi["participant_impact"]["missing_participants"] == ["institution", "pension"]
    assert kospi["participant_bridge_manifest"]["observation_sha256"] == manifest["observation_sha256"]
    assert kospi["participant_impact"]["forecast_uplift"] == "unmeasured"
    assert kospi["forecast"]["production_weight"] == 0


def test_empty_actual_subproject_source_is_insufficient_not_forced_feature(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    subproject = tmp_path / "empty-subproject.sqlite"
    with sqlite3.connect(subproject) as conn:
        conn.execute("CREATE TABLE kr_investor_flows(symbol TEXT, trade_date TEXT)")
    report = mod.run_reliable_pipeline(path, tmp_path / "empty-impact", config=config(), sessions=sessions,
                now=datetime(2026, 10, 23, 14, tzinfo=UTC), participant_sqlite=subproject)
    assert all(item["participant_impact"]["status"] == "insufficient_data" for item in report["markets"])
    assert all(set(item["active_feature_groups"]) == {"price"} for item in report["markets"])
    assert all(item["participant_bridge_manifest"]["input_kind"] == "read_only_sub_sqlite" for item in report["markets"])


def test_incomplete_or_unavailable_current_close_blocks_before_archive(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    kwargs = {"config": config(), "sessions": sessions, "now": datetime(2026, 10, 23, 14, tzinfo=UTC)}
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE index_eod SET close = NULL WHERE session_date = '2026-10-23' AND index_code = 'KOSPI'")
    first = mod.run_reliable_pipeline(path, tmp_path / "missing", **kwargs)
    assert first["status"] == "blocked_incomplete_input"
    assert not list((tmp_path / "missing" / "archive").glob("*.json"))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE index_eod SET close = 2500, published_at = '2026-10-24T14:00:00+00:00' WHERE session_date = '2026-10-23'")
    second = mod.run_reliable_pipeline(path, tmp_path / "unavailable", **kwargs)
    assert second["status"] == "blocked_unavailable_input"
    assert not list((tmp_path / "unavailable" / "archive").glob("*.json"))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE index_eod SET published_at = NULL, ingested_at = NULL WHERE session_date = '2026-10-23'")
    third = mod.run_reliable_pipeline(path, tmp_path / "unknown_retrieval", **kwargs)
    assert third["status"] == "blocked_unavailable_input"


def test_current_research_archive_is_immutable_and_missing_groups_are_explicit(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    arguments = {"config": config(), "sessions": sessions, "now": datetime(2026, 10, 23, 14, tzinfo=UTC)}
    first = mod.run_reliable_pipeline(path, tmp_path / "report", **arguments)
    second = mod.run_reliable_pipeline(path, tmp_path / "report", **arguments)
    assert first["run_id"] == second["run_id"]
    assert len(list((tmp_path / "report" / "archive").glob("*.json"))) == 1
    for market in first["markets"]:
        forecast = market["forecast"]
        assert forecast["forecast_id"] == market["forecast_id"]
        assert forecast["status"] in {"rejected", "not_ready", "research_candidate"}
        assert forecast["production_weight"] == 0
        assert market["context"]["target_date"] == "2026-10-26"
        assert market["participant_pressure"]["inferred"] == "inconclusive"
        assert set(market["missing_feature_groups"]) == {"breadth", "participants"}
        assert market["data_provenance"]["original_publication_verified"] is False
        assert market["forecast"]["p_up"] + market["forecast"]["p_down"] == 1
    changed = {**arguments, "config": {**config(), "experiment_note": "changed artifact"}}
    third = mod.run_reliable_pipeline(path, tmp_path / "report", **changed)
    assert third["run_id"] != first["run_id"]
    stored = json.loads((tmp_path / "report" / "archive" / (first["run_id"] + ".json")).read_text())
    assert stored["run_id"] == first["run_id"]
    assert stored["markets"][0]["forecast"]["issued_at"] is None
    assert mod.resolve_reliable_publication(tmp_path / "report", run_id=first["run_id"])["markets"][0]["forecast"] == first["markets"][0]["forecast"]
    assert ReliableSettings().test_days == 252


def _pages_rows_absent(value):
    if isinstance(value, dict):
        assert {"candidate_observations", "observations", "calibration_observations"}.isdisjoint(value)
        for item in value.values():
            _pages_rows_absent(item)
    elif isinstance(value, list):
        for item in value:
            _pages_rows_absent(item)


def test_pages_file_omits_oof_rows_while_archive_keeps_them(tmp_path):
    mod = pipeline()
    path, sessions = database(tmp_path)
    output = tmp_path / "pages"
    report = mod.run_reliable_pipeline(path, output, config=config(), sessions=sessions,
                now=datetime(2026, 10, 23, 14, tzinfo=UTC))
    assert report["status"] == "research_only"
    assert report["production_weight"] == 0
    assert report["markets"][0]["evaluation"]["candidate_observations"]
    assert report["markets"][0]["evaluation"]["calibration_observations"]
    assert "pages_omitted" not in report
    written = json.loads((output / "reliable_index_forecast.json").read_text())
    validation = json.loads((output / "reliable_validation_report.json").read_text())
    archive = json.loads((output / "archive" / f"{report['run_id']}.json").read_text())
    _pages_rows_absent(written)
    _pages_rows_absent(validation)
    assert "candidate_observations" in written["pages_omitted"]
    assert archive["markets"][0]["evaluation"]["candidate_observations"]
    assert archive["markets"][0]["evaluation"]["calibration_observations"]
    assert "pages_omitted" not in archive
    assert mod.resolve_reliable_publication(output)["status"] == "research_only"
    manifest = json.loads((output / "reliable_publication.json").read_text())
    for name in ("reliable_index_forecast.json", "reliable_validation_report.json"):
        digest = hashlib.sha256((output / name).read_bytes()).hexdigest()
        assert manifest["payload_checksums"][name] == digest
    archive_name = f"archive/{report['run_id']}.json"
    assert manifest["payload_checksums"][archive_name] == hashlib.sha256(
        (output / "archive" / f"{report['run_id']}.json").read_bytes()).hexdigest()
    assert manifest["payload_checksums"][archive_name] != manifest["payload_checksums"]["reliable_index_forecast.json"]

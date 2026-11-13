"""Actual-time orchestration and immutable artifacts for calibrated 1D research.

This track never updates the legacy 5D pipeline or any production model weights.
Historical close-only reconstruction is explicitly different from an observed
point-in-time publication archive.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.economic_forecast import (
    begin_forward,
    calendar_frame,
    finish_forward,
    load_calendar,
    numeric_shadow,
)
from roboquant.index_forecast.event_context import build_event_risk_context
from roboquant.index_forecast.international_news import build_international_news_report
from roboquant.index_forecast.participant_impact import build_participant_impact_inputs
from roboquant.index_forecast.reliable import (
    ReliableSettings,
    evaluate_reliable_forecasts,
    forecast_reliable_challenger,
    forecast_reliable_latest,
    resolve_live_fit,
)
from roboquant.index_forecast.reliable_inputs import (
    BREADTH_FEATURES,
    KST,
    PARTICIPANT_FEATURES,
    build_participant_features,
    build_stock_breadth,
    resolve_forecast_context,
    session_schedule,
    summarize_participant_pressure,
    utc_timestamp,
)
from roboquant.index_forecast.us_market_inputs import (
    US_FEATURES,
    build_us_features,
    us_evidence_card,
)


def build_reliable_price_frame(prices: pd.DataFrame, *, sessions: Any = None,
                               forecast_context: dict | None = None) -> pd.DataFrame:
    """Reindex sessions and honor known availability without inventing vintages.

    Unknown original publication clocks retain the declared close reconstruction.
    Local receipts only bound this run's knowledge; they are never backdated to
    historical row cutoffs. Explicit publication clocks also bound every rolling
    feature window at its own stage-matched prediction cutoff.
    """
    if prices.empty:
        return pd.DataFrame()
    data = prices.copy()
    data["asof_date"] = pd.to_datetime(data["session_date"], errors="coerce").dt.strftime("%Y-%m-%d")
    data = data.dropna(subset=["asof_date"]).sort_values("asof_date").drop_duplicates("asof_date", keep="last")
    schedule, _ = session_schedule(pd.Timestamp(data["asof_date"].min()),
                                    pd.Timestamp(data["asof_date"].max()) + timedelta(days=14), sessions)
    calendar = schedule.loc[(schedule["date"] >= data["asof_date"].min()) &
                            (schedule["date"] <= data["asof_date"].max())].copy()
    # Keep the next scheduled target even for the last, currently unlabeled row.
    next_dates = dict(zip(schedule["date"], schedule["date"].shift(-1), strict=True))
    close_times = dict(zip(schedule["date"], schedule["close_at"], strict=True))
    data = calendar[["date"]].rename(columns={"date": "asof_date"}).merge(data, on="asof_date", how="left")
    close = pd.to_numeric(data["close"], errors="coerce").where(lambda values: values.gt(0))
    if forecast_context is not None and "ingested_at" in data:
        limit = utc_timestamp(forecast_context["prediction_cutoff"])
        received = data["ingested_at"].map(utc_timestamp)
        # Missing historical receipts remain explicitly retrospective. A known
        # malformed/future receipt cannot enter this run, even via an old close.
        allowed = data["ingested_at"].isna() | received.map(lambda stamp: pd.notna(stamp) and stamp <= limit)
        close = close.where(allowed)
    data["close"] = close
    for width in (1, 5, 20):
        data[f"return_{width}d"] = close.pct_change(width, fill_method=None)
    log_return = np.log(close / close.shift(1))
    for width in (5, 20):
        data[f"volatility_{width}d"] = log_return.rolling(width, min_periods=width).std()
        data[f"ma_gap_{width}d"] = close / close.rolling(width, min_periods=width).mean() - 1
    data["target_date"] = data["asof_date"].map(next_dates)
    data["asof_close_at"] = data["asof_date"].map(close_times).map(lambda x: x.isoformat() if pd.notna(x) else None)
    data["target_close_at"] = data["target_date"].map(close_times).map(lambda x: x.isoformat() if pd.notna(x) else None)
    data["label_return"] = close.shift(-1) / close - 1
    data["prediction_cutoff"] = data["asof_date"].map(close_times).map(lambda x: x.isoformat())
    if forecast_context is not None:
        current_close = utc_timestamp(close_times[forecast_context["asof_date"]])
        elapsed = utc_timestamp(forecast_context["prediction_cutoff"]) - current_close
        historical_cutoffs = []
        for row in data.itertuples():
            target_snapshot = utc_timestamp(str(row.target_date) + "T08:00:00+09:00")
            candidate = (target_snapshot if forecast_context["stage"] == "premarket_final"
                         else min(utc_timestamp(row.prediction_cutoff) + elapsed, target_snapshot))
            historical_cutoffs.append(candidate.isoformat())
        data["prediction_cutoff"] = historical_cutoffs
        data.loc[data["asof_date"] == forecast_context["asof_date"], "prediction_cutoff"] = forecast_context["prediction_cutoff"]
    # These are market close maturity times, never fabricated publication times.
    # The report explicitly labels this as retrospective close reconstruction.
    data["label_available_at"] = data["target_date"].map(close_times).map(
        lambda x: x.isoformat() if not pd.isna(x) else None)
    if "published_at" in data:
        published = dict(zip(data["asof_date"], data["published_at"], strict=True))
        def maturity(day: Any, assumed: Any) -> Any:
            original = published.get(day)
            if original is None or pd.isna(original):
                return assumed
            stamp = utc_timestamp(original)
            return None if pd.isna(stamp) else stamp.isoformat()
        data["label_available_at"] = [maturity(day, assumed) for day, assumed in
                                      zip(data["target_date"], data["label_available_at"], strict=True)]
        cutoffs = data["prediction_cutoff"].map(utc_timestamp).tolist()
        unavailable = np.zeros(len(data), dtype=bool)
        for position, original in enumerate(data["published_at"]):
            if original is None or pd.isna(original):
                continue
            stamp = utc_timestamp(original)
            # return/volatility_20d require the current plus previous 20 closes.
            for affected in range(position, min(position + 21, len(data))):
                if pd.isna(stamp) or pd.isna(cutoffs[affected]) or stamp > cutoffs[affected]:
                    unavailable[affected] = True
        data.loc[unavailable, PRICE_FEATURES] = np.nan
    return data


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json_text(value).encode()).hexdigest()


def _frame_hash(frame: pd.DataFrame) -> str:
    return hashlib.sha256(frame.to_json(orient="split", date_format="iso", double_precision=15).encode()).hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _json_text(value) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_optional(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return pd.read_sql_query(f'SELECT * FROM "{table}"', conn) if exists else pd.DataFrame()


def _load_news_inputs(config: dict, manual_events: dict) -> dict:
    source = config.get("international_news", {}).get("snapshot_path")
    snapshot = json.loads(Path(source).read_text()) if source and Path(source).is_file() else {}
    return {"events": [*manual_events.get("events", []), *snapshot.get("events", [])],
            "coverage": snapshot.get("coverage", {}), "snapshot": snapshot}


def _apply_international_news(evaluation: dict, forecast: dict, *, market: str, cutoff,
                               target_close_at, news_inputs: dict, minimum_oos: int) -> dict:
    selected = evaluation.get("selected") or {}
    key = f"{selected.get('family')}:{selected.get('group')}"
    records = evaluation.get("candidate_observations", {}).get(key, [])
    protocol = {field: evaluation.get(field) for field in ("development_start", "selection_frozen_before", "status", "gate_reasons")}
    stamp, oof = utc_timestamp(cutoff), []
    for original in records:
        available, close = utc_timestamp(original.get("label_available_at")), utc_timestamp(original.get("target_close_at"))
        if pd.isna(available) or pd.isna(close) or max(available, close) > stamp:
            continue
        day = original["asof_date"]
        split = ("terminal" if protocol["selection_frozen_before"] and day >= protocol["selection_frozen_before"]
                 else "development" if protocol["development_start"] and day >= protocol["development_start"] else "calibration_history")
        oof.append({**original, "market": market, "split": split})
    report = build_international_news_report(news_inputs["events"], news_inputs["coverage"], oof, forecast,
        market, cutoff, target_close_at, minimum_oos=minimum_oos, evaluation_protocol=protocol)
    if report["alpha"] == 0 and report["blended_forecast"] != forecast:
        raise ValueError("alpha-zero news fusion must preserve the exact base forecast")
    if report["alpha"] > 0 and not report["news_model_ready"]:
        raise ValueError("positive news fusion requires independent model readiness")
    return report


def _stage_json(path: Path, value: dict) -> str:
    payload = (_json_text(value) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(payload).hexdigest()


def _resolved_run(payload: dict, manifest: dict) -> dict:
    result = deepcopy(payload)
    checked = manifest["publication_checked_at"]
    result.update(status="research_only", issued_at=checked, artifact_written_at=checked, publication=manifest)
    for record in result["markets"]:
        record["context"]["issued_at"] = checked
        record["forecast"].update(issued_at=checked, artifact_written_at=checked)
    return result


_PAGES_ROW_KEYS = frozenset({"candidate_observations", "observations", "calibration_observations", "fit_artifacts"})
PAGES_OMITTED_NOTE = (
    "candidate_observations, observations, and calibration_observations are omitted from this Pages file; "
    "the returned report and archive JSON keep the full rows"
)


def _without_pages_rows(value: Any) -> Any:
    """Copy a document tree without the embedded out-of-fold row payloads."""
    if isinstance(value, dict):
        return {key: _without_pages_rows(item) for key, item in value.items() if key not in _PAGES_ROW_KEYS}
    if isinstance(value, list):
        return [_without_pages_rows(item) for item in value]
    return value


def pages_public_document(document: dict) -> dict:
    """Pages copy used by both forecast pipelines.

    The in-memory report and ``archive/{run_id}.json`` stay complete. Checksums
    hash the bytes actually written, including this shorter Pages body.
    """
    compact = _without_pages_rows(document)
    compact["pages_omitted"] = PAGES_OMITTED_NOTE
    return compact


def resolve_reliable_publication(output_dir: str | Path, *, run_id: str | None = None) -> dict:
    """Resolve only a checksummed payload with an authoritative commit manifest."""
    output = Path(output_dir)
    relative = f"archive/{run_id}.json" if run_id else "reliable_index_forecast.json"
    raw = (output / relative).read_bytes()
    payload = json.loads(raw)
    if str(payload.get("status", "")).startswith("blocked_"):
        return payload
    manifest_path = output / (f"publications/{run_id}.json" if run_id else "reliable_publication.json")
    unpublished = {"status": "unpublished", "run_id": payload.get("run_id"), "markets": [],
                   "issued_at": None, "reasons": ["missing_or_mismatched_publication_manifest"]}
    if not manifest_path.is_file():
        return unpublished
    manifest = json.loads(manifest_path.read_bytes())
    if manifest.get("run_id") != payload.get("run_id") or manifest.get("schema_version") != "reliable_publication_v1":
        return unpublished
    checksums = manifest.get("payload_checksums", {})
    required = {relative} if run_id else set(checksums)
    if relative not in checksums or not required:
        return unpublished
    for name in required:
        file_path = output / name
        if not file_path.is_file() or hashlib.sha256(file_path.read_bytes()).hexdigest() != checksums[name]:
            return unpublished
    if not manifest.get("publication_checked_at"):
        return unpublished
    return _resolved_run(payload, manifest)


def _publish_staged_run(run: dict, output: Path, documents: dict[str, dict], *,
                         contexts: dict, replay: bool) -> dict:
    """Stage large outputs before a small, authoritative atomic commit record.

    The recorded issue clock is the publication-decision check after payload I/O.
    A second clock guard immediately precedes rename/link commit. This is not a
    hard-real-time physical-visibility or nanosecond timestamp guarantee.
    """
    output.mkdir(parents=True, exist_ok=True)
    deadline = min(utc_timestamp(context["issuance_deadline"]) for context in contexts.values())
    with tempfile.TemporaryDirectory(prefix=".reliable-stage-", dir=output) as temporary:
        stage = Path(temporary)
        checksums = {name: _stage_json(stage / name, value) for name, value in documents.items()}
        publication_relative = f"publications/{run['run_id']}.json"
        publication_path = output / publication_relative
        # Verify immutable conflicts before the time-critical commit section.
        for name in (f"archive/{run['run_id']}.json", publication_relative):
            (output / name).parent.mkdir(parents=True, exist_ok=True)
        archive_name = f"archive/{run['run_id']}.json"
        if (output / archive_name).exists() and hashlib.sha256((output / archive_name).read_bytes()).hexdigest() != checksums[archive_name]:
            raise ValueError("immutable archive content conflict")
        existing = json.loads(publication_path.read_bytes()) if publication_path.exists() else None
        if existing and existing.get("payload_checksums") != checksums:
            raise ValueError("immutable publication manifest conflict")
        checked = utc_timestamp(datetime.now(KST))
        manifest = existing or {"schema_version": "reliable_publication_v1", "run_id": run["run_id"],
                                "status": "research_only", "replay": replay,
                                "publication_checked_at": checked.isoformat(),
                                "run_started_at": run["run_started_at"], "prepared_at": run["prepared_at"],
                                "payload_checksums": checksums,
                                "timestamp_basis": "publication decision after large payload staging; guarded immediately before atomic commit; no hard-real-time guarantee"}
        # Only this small record remains to serialize/fsync; then check the clock
        # once more. No hashing, large serialization or payload I/O follows it.
        _stage_json(stage / "publication.json", manifest)
        guard = utc_timestamp(datetime.now(KST))
        if not replay and (checked >= deadline or guard >= deadline):
            blocked = {"status": "blocked_issuance_deadline", "issued_at": None,
                       "run_started_at": run["run_started_at"], "prepared_at": run["prepared_at"],
                       "publication_checked_at": guard.isoformat(), "artifact_written_at": None,
                       "production_weight": 0.0, "markets": [], "contexts": contexts,
                       "reasons": ["publication_at_or_after_target_session_open"]}
            _atomic_json(output / "reliable_index_forecast.json", blocked)
            return blocked
        if not (output / archive_name).exists():
            os.link(stage / archive_name, output / archive_name)
        for name in documents:
            if name != archive_name:
                os.replace(stage / name, output / name)
        if not publication_path.exists():
            os.link(stage / "publication.json", publication_path)
        # This tiny manifest is the authoritative visibility/acceptance point.
        os.replace(stage / "publication.json", output / "reliable_publication.json")
    return _resolved_run(run, manifest)


def _evidence_cards(missing: list[str]) -> list[dict]:
    hypotheses = {
        "price": "recent return/volatility may summarize short-horizon persistence and risk",
        "breadth": "large common-share participation may distinguish concentrated and broad index moves",
        "participants": "cash net flow normalized by turnover may measure incremental demand pressure",
    }
    return [{"group": group, "economic_hypothesis": hypothesis,
             "data_acquisition": "SQLite observed closes" if group == "price" else "dated, release-timestamped snapshots required",
             "actual_publication_timestamp": "unknown for reconstructed price history" if group == "price" else "required; absence excludes group",
             "cost": "existing local data; no paid source purchased",
             "missing_rate": "unavailable" if group in missing else "see eligible_n and date range",
             "history_length": "see validation candidate eligibility",
             "leakage_risk": "retrospective reconstruction; original revisions unverified" if group == "price" else "survivorship/revision/publication leakage requires dated availability",
             "baseline_gain": "unknown until statistical gates pass; see validation",
             "regime_performance": "see year/volatility/trend slices; unavailable groups untested",
             "ablation_result": "unavailable: no eligible PIT history" if group in missing else "see ablation_comparison"}
            for group, hypothesis in hypotheses.items()]


def run_reliable_pipeline(sqlite_path: str | Path, output_dir: str | Path, *, config: dict | None = None,
                          now: datetime | None = None, target_date: Any = None, sessions: Any = None,
                          membership: pd.DataFrame | None = None, stock_prices: pd.DataFrame | None = None,
                          flows: pd.DataFrame | None = None, position_evidence: dict | None = None,
                          participant_sqlite: str | Path | None = None,
                          participant_bridge: str | Path | None = None,
                          us_observations: pd.DataFrame | str | Path | None = None,
                          news_context: dict | str | Path | None = None) -> dict:
    """Build one immutable research run; stale inputs block before any forecast archive."""
    config = dict(config or {})
    issued = utc_timestamp(now if now is not None else datetime.now(KST))
    raw_settings = dict(config.get("settings", {}))
    if "model_families" in raw_settings:
        raw_settings["model_families"] = tuple(raw_settings["model_families"])
    settings = ReliableSettings(**raw_settings)
    us_config = config.get("us_market", {})
    us_requested = us_observations is not None or bool(us_config.get("enabled", False))
    us_source = us_observations if us_observations is not None else us_config.get("observations_path")
    us_data = (us_source.copy() if isinstance(us_source, pd.DataFrame) else pd.read_csv(us_source)
               if us_source and Path(us_source).is_file() else pd.DataFrame())
    event_source = news_context if news_context is not None else config.get("event_context", {}).get("path")
    event_payload = (event_source if isinstance(event_source, dict) else json.loads(Path(event_source).read_text())
                     if event_source and Path(event_source).is_file() else {})
    news_inputs = _load_news_inputs(config, event_payload)
    path = Path(sqlite_path)
    with sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True) as conn:
        prices = pd.read_sql_query("SELECT * FROM index_eod WHERE index_code IN ('KOSPI','KOSDAQ') ORDER BY index_code,session_date", conn)
        membership = membership if membership is not None else _read_optional(conn, "reliable_membership_snapshot")
        stock_prices = stock_prices if stock_prices is not None else _read_optional(conn, "reliable_stock_prices")
        flows = flows if flows is not None else _read_optional(conn, "reliable_participant_flow")
    contexts, prepared = {}, {}
    for market in ("KOSPI", "KOSDAQ"):
        subset = prices.loc[prices["index_code"] == market]
        if subset.empty:
            contexts[market] = {"status": "blocked_missing_index", "reasons": ["missing_index_close"]}
        else:
            contexts[market] = resolve_forecast_context(subset["session_date"].max(), now=issued,
                                                        sessions=sessions, target_date=target_date)
            context = contexts[market]
            if context["status"] != "ready":
                continue
            current = subset.loc[subset["session_date"] == context["asof_date"]].iloc[-1]
            close = pd.to_numeric(current["close"], errors="coerce")
            if not np.isfinite(close) or close <= 0:
                context.update(status="blocked_incomplete_input", reasons=["latest_index_close_missing_or_invalid"])
                continue
            if "is_final" in current and str(current["is_final"]).lower() not in {"true", "1", "yes"}:
                context.update(status="blocked_incomplete_input", reasons=["latest_index_close_not_final"])
                continue
            if current.get("ingested_at") is None or pd.isna(current.get("ingested_at")):
                context.update(status="blocked_unavailable_input", reasons=["latest_index_retrieval_timestamp_unknown"])
                continue
            for field in ("published_at", "ingested_at"):
                original = current.get(field)
                if original is not None and not pd.isna(original):
                    stamp = utc_timestamp(original)
                    # A later run cannot claim an 08:00 snapshot using a close
                    # first received after that snapshot, even if already final.
                    limit = context["prediction_cutoff"]
                    if pd.isna(stamp) or stamp > utc_timestamp(limit):
                        context.update(status="blocked_unavailable_input", reasons=[f"latest_index_{field}_unavailable_at_cutoff"])
                        break
            if context["status"] != "ready":
                continue
            prepared[market] = build_reliable_price_frame(subset, sessions=sessions, forecast_context=context)
            latest_features = prepared[market].loc[prepared[market]["asof_date"] == context["asof_date"], PRICE_FEATURES]
            if latest_features.empty or not np.isfinite(latest_features.to_numpy(dtype=float)).all():
                context.update(status="blocked_incomplete_input", reasons=["latest_index_price_features_incomplete"])
    blocked = [context for context in contexts.values() if context["status"] != "ready"]
    output = Path(output_dir)
    code_hash = _hash({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                       for name in ("reliable.py", "reliable_intervals.py", "fit_protocol.py", "market_calendar.py", "economic_calendar.py", "economic_forecast.py", "economic_sources.py", "forward_ledger.py", "reliable_inputs.py", "reliable_pipeline.py", "participant_impact.py", "us_market_inputs.py", "event_context.py", "international_news.py")})
    input_hashes = {"index": _frame_hash(prices), "membership": _frame_hash(membership),
                    "stock_prices": _frame_hash(stock_prices), "participant_flows": _frame_hash(flows),
                    "us_observations": _frame_hash(us_data), "current_event_context": _hash(event_payload),
                    "international_news_snapshot": _hash(news_inputs["snapshot"])}
    registry = Path(__file__).resolve().parents[3] / 'configs/economic_calendar.yaml'
    code_hash = _hash({'modules': code_hash, 'economic_registry': hashlib.sha256(registry.read_bytes()).hexdigest() if registry.is_file() else None})
    calendar_store = load_calendar(config)
    evidence = begin_forward(output, config=config, settings=settings, started_at=issued, code_hash=code_hash, replay=now is not None)
    if blocked:
        result = {"status": blocked[0]["status"], "issued_at": issued.isoformat(),
                  "production_weight": 0.0, "markets": [], "contexts": contexts,
                  "reasons": [reason for item in blocked for reason in item["reasons"]]}
        _atomic_json(output / "reliable_index_forecast.json", result)
        finish_forward(output, evidence=evidence, frames=prepared, observed_at=datetime.now(KST))
        return result
    run = {"schema_version": "reliable_index_forecast_v1", "status": "research_only",
           "issued_at": issued.isoformat(), "production_weight": 0.0, "horizon": "1D",
           "run_started_at": issued.isoformat(),
           "issuance_policy": "08:00 KST snapshot cutoff; actual publication strictly before verified XKRX session open",
           "replay": now is not None, "config_hash": _hash({**config, "settings": asdict(settings)}),
           "code_hash": code_hash, "data_hash": _hash(input_hashes), "data_components": input_hashes, "markets": [],
           "restrictions": ["no production promotion", "no orders", "retrospective history does not establish original publication availability",
                            "independent review and 60 actual premarket observations required"],
           "interval_note": "80% target interval from prior mature OOF errors; temporal dependence prevents a coverage guarantee"}
    for market, context in contexts.items():
        raw = prices.loc[prices["index_code"] == market].copy()
        frame = prepared[market].copy()
        frame = frame.loc[frame["asof_date"] <= context["asof_date"]].copy()
        frame, economic_contexts = calendar_frame(frame, calendar_store)
        cutoffs = dict(zip(frame["asof_date"], frame["prediction_cutoff"], strict=True))
        breadth = build_stock_breadth(stock_prices, membership, market=market, asof_cutoffs=cutoffs,
                                      target_count=int(config.get("universe", {}).get(market, 100 if market == "KOSPI" else 50)))
        participants = build_participant_features(flows, market=market, asof_cutoffs=cutoffs)
        groups = {"price": list(PRICE_FEATURES)}
        missing = []
        for name, optional, features in (("breadth", breadth, BREADTH_FEATURES),
                                         ("participants", participants, PARTICIPANT_FEATURES)):
            if optional.empty:
                missing.append(name)
                continue
            frame = frame.merge(optional, on="asof_date", how="left", validate="one_to_one")
            # A live-only snapshot does not activate a historically untrained head.
            complete = frame.dropna(subset=[*PRICE_FEATURES, *features, "label_return"])
            minimum = settings.initial_train_days + settings.purge_days + settings.min_calibration_rows + settings.development_days + settings.test_days
            if len(complete) < minimum or not frame.loc[frame["asof_date"] == context["asof_date"], features].notna().all(axis=None):
                missing.append(name)
            else:
                groups[f"price_{name}"] = [*PRICE_FEATURES, *features]
        if "price_breadth" in groups and "price_participants" in groups:
            groups["price_breadth_participants"] = [*PRICE_FEATURES, *BREADTH_FEATURES, *PARTICIPANT_FEATURES]
        impact_config = config.get("participant_impact", {})
        impact = build_participant_impact_inputs(
            market=market, asof_cutoffs=cutoffs,
            sqlite_path=participant_sqlite if participant_sqlite is not None else impact_config.get("sqlite"),
            bridge_path=participant_bridge if participant_bridge is not None else impact_config.get("bridge"),
            membership=membership, stock_prices=stock_prices, index_history=frame,
            target_count=int(config.get("universe", {}).get(market, 100 if market == "KOSPI" else 50)))
        impact_features = impact["features"]
        actor_eligibility = {}
        if not impact_features.empty:
            frame = frame.merge(impact_features, on="asof_date", how="left", validate="one_to_one")
            minimum = settings.initial_train_days + settings.purge_days + settings.min_calibration_rows + settings.development_days + settings.test_days
            for name, columns in sorted(impact["feature_groups"].items()):
                complete = frame.dropna(subset=[*PRICE_FEATURES, *columns, "label_return"])
                live_ready = bool(frame.loc[frame["asof_date"] == context["asof_date"], columns].notna().all(axis=None))
                eligible = len(complete) >= minimum and live_ready
                actor_eligibility[name] = {"eligible_n": len(complete), "required_n": minimum,
                                           "live_ready": live_ready, "eligible": eligible,
                                           "reasons": ([] if eligible else
                                                       ["insufficient_pit_history"] if len(complete) < minimum else ["current_actor_inputs_missing"])}
                if eligible:
                    groups[f"price_{name}"] = [*PRICE_FEATURES, *columns]
        if any(name.startswith("price_participant_") for name in groups) and "participants" in missing:
            missing.remove("participants")
        history = frame.dropna(subset=[*PRICE_FEATURES, "label_return", "target_date"])
        us_result = build_us_features(us_data, asof_cutoffs=cutoffs, live_asof_date=context["asof_date"],
                                      reporting_cutoff=issued, buffer_minutes=int(us_config.get("buffer_minutes", 15)),
                                      max_stale_sessions=int(us_config.get("max_stale_sessions", 0)))
        if us_requested and not us_result["features"].empty:
            frame = frame.merge(us_result["features"], on="asof_date", how="left", validate="one_to_one")
            common = frame.dropna(subset=[*PRICE_FEATURES, *US_FEATURES, "label_return", "target_date"])
            minimum = settings.initial_train_days + settings.purge_days + settings.min_calibration_rows + settings.development_days + settings.test_days
            current_ready = bool(frame.loc[frame["asof_date"] == context["asof_date"], US_FEATURES].notna().all(axis=None))
            if len(common) >= minimum and current_ready:
                groups["price_us"] = [*PRICE_FEATURES, *US_FEATURES]
                history = common
            else:
                missing.append("us_market")
        elif us_requested:
            missing.append("us_market")
        us_result["report"].update(requested=us_requested, group_used="price_us" in groups,
                                   trial_status="retrospective_terminal_previously_viewed",
                                   comparison={"common_eligible_rows": len(history),
                                               "same_training_and_oos_dates": "price_us" in groups,
                                               "date_start": history.asof_date.min() if not history.empty else None,
                                               "date_end": history.asof_date.max() if not history.empty else None})
        evaluation = evaluate_reliable_forecasts(history, feature_groups=groups, settings=settings,
                                                  reporting_cutoff=context["prediction_cutoff"], prediction_grid=frame)
        impact["report"]["forecast_group_eligibility"] = actor_eligibility
        impact["report"]["forecast_ablation_evidence"] = {
            "split": "development_oof_selection_exposed; terminal confirms frozen selection only",
            "association_used_for_selection": False,
            "candidate_reports": [report for report in evaluation.get("candidate_reports", [])
                                  if report["group"] == "price" or report["group"].startswith("price_participant_")],
            "selected": evaluation.get("selected"), "gate_status": evaluation["status"],
            "uplift_claim": "not_established_by_descriptive_association"}
        latest = frame.loc[frame["asof_date"] == context["asof_date"]].copy()
        latest["prediction_cutoff"] = context["prediction_cutoff"]
        incumbent = forecast_reliable_latest(history, latest, evaluation, settings=settings)
        fit_state = {"status": "retrospective_replay", "reason": "explicit_replay_not_live_capture"}
        if now is None:
            fit_state = resolve_live_fit(history, latest, evaluation.get("selected"), settings=settings,
                state_dir=config.get("fit_state_dir", output / "fit_models"), market=market,
                allow_capture=context["stage"] == "premarket_final")
            forecast = forecast_reliable_latest(history, latest, evaluation, settings=settings,
                fit_artifact=fit_state.get("artifact"), require_live_snapshot=True)
            if fit_state.get("reason"):
                forecast["gate_reasons"] = list(dict.fromkeys([*forecast["gate_reasons"], fit_state["reason"]]))
        else:
            forecast = incumbent
        us_challenger = forecast_reliable_challenger(history, latest, evaluation, family="linear", group="price_us", settings=settings)
        close = float(latest["close"].iloc[0])
        if us_challenger.get("expected_return") is not None:
            us_challenger.update(latest_close=close, target_date=context["target_date"], target_close_at=context["target_close_at"],
                                 expected_index=close * (1 + us_challenger["expected_return"]),
                                 index_range_low=None if us_challenger["range_low"] is None else close * (1 + us_challenger["range_low"]),
                                 index_range_high=None if us_challenger["range_high"] is None else close * (1 + us_challenger["range_high"]))
        forecast.update(target_date=context["target_date"], issued_at=context["issued_at"],
                        stage=context["stage"], latest_close=close, target_close_at=context["target_close_at"],
                        expected_index=None if forecast["expected_return"] is None else close * (1 + forecast["expected_return"]),
                        index_range_low=None if forecast["range_low"] is None else close * (1 + forecast["range_low"]),
                        index_range_high=None if forecast["range_high"] is None else close * (1 + forecast["range_high"]))
        news_report = _apply_international_news(evaluation, forecast, market=market,
            cutoff=context["prediction_cutoff"], target_close_at=context["target_close_at"],
            news_inputs=news_inputs, minimum_oos=settings.min_oos_rows)
        forecast = news_report["blended_forecast"]
        provenance = {"sources": sorted(raw.get("source", pd.Series(dtype=str)).dropna().astype(str).unique()),
                      "retrieved_at": str(raw.loc[raw["session_date"] == context["asof_date"], "ingested_at"].iloc[-1]),
                      "original_publication_verified": False,
                      "historical_mode": "retrospective_close_reconstruction",
                      "label_maturity_assumption": "target exchange close; not asserted original release timestamp",
                      "live_mode": "observed_local_close", "asof_date": context["asof_date"],
                      "membership_status": "unavailable_for_training" if "breadth" in missing else "dated_available_snapshots",
                      "participant_status": "unavailable_for_training" if "participants" in missing else "final_available_observations"}
        macro = numeric_shadow(frame, store=calendar_store, settings=settings, market=market,
            state_dir=config.get("fit_state_dir", output / "fit_models"), live=now is None,
            allow_capture=context["stage"] == "premarket_final")
        record = {"economic_calendar": economic_contexts.get(context["asof_date"], {"event_bucket": None, "calendar_complete": False}),
                  "numeric_shadow": macro, "market": market, "context": context, "forecast": forecast, "evaluation": evaluation,
                  "incumbent_reference": {**incumbent, "role": "retrospective_reference_not_live_model"},
                  "fit_state": {key: value for key, value in fit_state.items() if key != "artifact"},
                  "missing_feature_groups": missing, "active_feature_groups": groups,
                  "participant_pressure": summarize_participant_pressure(flows, market=market, cutoff=context["prediction_cutoff"]),
                  "position_subproject": position_evidence or {"status": "unavailable", "inferred": "inconclusive", "observed": {}, "unverified": ["no evidence artifact supplied"]},
                  "participant_impact": impact["report"], "participant_bridge_manifest": impact["bridge_manifest"],
                  "us_market": us_result["report"], "us_research_challenger": us_challenger,
                  "event_context": build_event_risk_context(event_payload, cutoff=context["prediction_cutoff"]),
                  "international_news": news_report,
                  "data_provenance": provenance, "evidence_cards": [*_evidence_cards(missing),
                                                                      *([us_evidence_card(us_result["report"])] if us_requested else [])],
                  "feature_hash": _frame_hash(latest[["asof_date", *groups[forecast.get("group") or "price"]]]),
                  "model_hash": _hash({"selected": evaluation.get("selected"), "model_params": evaluation.get("model_params"),
                                       "settings": asdict(settings), "training_data_hash": _frame_hash(history), "code_hash": code_hash,
                                       "fit_artifact_hash": forecast.get("fit_artifact_hash")})}
        run["markets"].append(record)
    run["data_hash"] = _hash({**input_hashes, "participant_bridge_manifests": {
        record["market"]: record["participant_bridge_manifest"] for record in run["markets"]}})
    prepared_clock = utc_timestamp(datetime.now(KST)) if now is None else None
    run.update(status="unpublished", issued_at=None, artifact_written_at=None,
               prepared_at=prepared_clock.isoformat() if prepared_clock is not None else None,
               publication_manifest="reliable_publication.json")
    for record in run["markets"]:
        record["context"].update(run_started_at=issued.isoformat(), issued_at=None)
        record["forecast"].update(run_started_at=issued.isoformat(), issued_at=None, artifact_written_at=None)
        record["forecast_id"] = _hash({"market": record["market"], "context": record["context"],
                                      "data_hash": run["data_hash"], "config_hash": run["config_hash"],
                                      "code_hash": code_hash, "feature_hash": record["feature_hash"],
                                      "model_hash": record["model_hash"]})
        record["forecast"]["forecast_id"] = record["forecast_id"]
    run["run_id"] = _hash({key: value for key, value in run.items() if key != "run_id"})
    validation = {"run_id": run["run_id"], "status": "unpublished", "markets": [
        {"market": item["market"], "forecast_id": item["forecast_id"], "evaluation": item["evaluation"],
         "evidence_cards": item["evidence_cards"]} for item in run["markets"]]}
    documents = {f"archive/{run['run_id']}.json": run,
                 "reliable_index_forecast.json": pages_public_document(run),
                 "reliable_validation_report.json": pages_public_document(validation),
                 "reliable_model_card.json": {"run_id": run["run_id"], "status": "unpublished", "production_weight": 0.0,
        "settings": asdict(settings), "restrictions": run["restrictions"], "interval_note": run["interval_note"],
        "markets": [{"market": item["market"], "forecast_id": item["forecast_id"],
                     "selected": item["evaluation"].get("selected"), "status": item["forecast"]["status"],
                     "data_provenance": item["data_provenance"]} for item in run["markets"]]}}
    published = _publish_staged_run(run, output, documents, contexts=contexts, replay=now is not None)
    finish_forward(output, evidence=evidence, frames=prepared, observed_at=datetime.now(KST))
    return published

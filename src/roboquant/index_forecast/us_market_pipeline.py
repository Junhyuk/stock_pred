"""Own-market US index forecasts with independent US session publication clocks."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.event_context import build_event_risk_context
from roboquant.index_forecast.reliable import (
    ReliableSettings,
    evaluate_reliable_forecasts,
    forecast_reliable_latest,
)
from roboquant.index_forecast.reliable_pipeline import (
    _apply_international_news,
    _atomic_json,
    _frame_hash,
    _hash,
    _load_news_inputs,
    _publish_staged_run,
)
from roboquant.index_forecast.us_market_inputs import (
    HISTORICAL_MODE,
    SCHEMA_VERSION,
    _utc,
    us_close_schedule,
    us_evidence_card,
)


def build_us_target_frame(observations: pd.DataFrame, *, symbol: str, reporting_cutoff) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build each feature row from the versions known at that row's own cutoff.

    Snapshot zero is the declared historical reconstruction. Explicit later
    revisions become available no earlier than their actual local first-seen.
    Labels use the first available target close; a target present only as a late
    correction retains that correction's actual maturity, never the old date.
    """
    data = observations.loc[(observations.symbol == symbol) & (observations.schema_version == SCHEMA_VERSION)].copy()
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    for column, original in (("_available", "available_at"), ("_seen", "first_seen_at")):
        data[column] = pd.to_datetime(data[original].map(_utc), utc=True)
    data["_revision"] = pd.to_numeric(data.revision, errors="coerce")
    data["close"] = pd.to_numeric(data.close, errors="coerce")
    data = data.loc[(data._seen <= _utc(reporting_cutoff)) & data._revision.ge(0) & data.close.gt(0) & np.isfinite(data.close) &
                    data.is_final.astype(str).str.lower().isin({"true", "1", "yes"})].copy()
    if data.empty:
        return pd.DataFrame(), pd.DataFrame()
    start = (pd.Timestamp(data.session_date.min()) - timedelta(days=10)).date().isoformat()
    end = (_utc(reporting_cutoff) + timedelta(days=14)).date().isoformat()
    schedule = us_close_schedule(start, end)
    schedule = schedule.loc[schedule.symbol == symbol].copy()
    closes = dict(zip(schedule.session_date, schedule.session_close_at, strict=True))
    minimum_availability = {day: _utc(close) + timedelta(minutes=15) for day, close in closes.items()}
    data["_available"] = [max(available, minimum_availability.get(day, available), seen if revision > 0 else available)
                           for day, available, seen, revision in zip(data.session_date, data._available, data._seen, data._revision, strict=True)]
    completed_days = set(schedule.loc[pd.to_datetime(schedule.session_close_at, utc=True) <= _utc(reporting_cutoff), "session_date"])
    data = data.loc[data.session_date.isin(completed_days) & (data._available <= _utc(reporting_cutoff))]
    if data.empty:
        return pd.DataFrame(), schedule
    versions = {day: rows.to_dict("records") for day, rows in data.groupby("session_date", sort=True)}
    def choose(day, cutoff=None, *, first=False):
        eligible = [r for r in versions.get(day, []) if not pd.isna(r["_available"]) and
                    (cutoff is None or r["_available"] <= cutoff)]
        if not eligible:
            return None
        if first:
            earliest = min(r["_available"] for r in eligible)
            eligible = [r for r in eligible if r["_available"] == earliest]
        def ordering(record):
            return record["_available"], record["_revision"], record["_seen"]
        best_key = max(map(ordering, eligible))
        latest = [r for r in eligible if ordering(r) == best_key]
        return latest[0] if len({r["close"] for r in latest}) == 1 else None
    finished = schedule.loc[pd.to_datetime(schedule.session_close_at, utc=True) <= _utc(reporting_cutoff)]
    latest_session = finished.iloc[-1].session_date if not finished.empty else None
    days, rows = schedule.session_date.tolist(), []
    for i, day in enumerate(days[:-1]):
        if day < data.session_date.min() or day > data.session_date.max() or day > str(latest_session):
            continue
        cutoff = _utc(reporting_cutoff) if day == latest_session else minimum_availability[day]
        current = choose(day, cutoff)
        target_day = days[i + 1]
        outcome = choose(target_day, first=True)
        row = {"asof_date": day, "target_date": target_day, "asof_close_at": closes[day],
               "target_close_at": closes[target_day], "prediction_cutoff": cutoff.isoformat(),
               "close": current["close"] if current else np.nan,
               "label_return": outcome["close"] / current["close"] - 1 if current and outcome else np.nan,
               "label_available_at": outcome["_available"].isoformat() if outcome else None,
               "label_revision_policy": "first_available_target_close; explicit later revisions not backfilled",
               **dict.fromkeys(PRICE_FEATURES, np.nan)}
        window = [choose(d, cutoff) for d in days[max(0, i - 20):i + 1]]
        if len(window) == 21 and all(value is not None for value in window):
            values = np.asarray([r["close"] for r in window], dtype=float)
            log_returns = np.log(values[1:] / values[:-1])
            row.update(return_1d=float(values[-1] / values[-2] - 1), return_5d=float(values[-1] / values[-6] - 1),
                       return_20d=float(values[-1] / values[-21] - 1), volatility_5d=float(log_returns[-5:].std(ddof=1)),
                       volatility_20d=float(log_returns.std(ddof=1)), ma_gap_5d=float(values[-1] / values[-5:].mean() - 1),
                       ma_gap_20d=float(values[-1] / values[-20:].mean() - 1))
        rows.append(row)
    return pd.DataFrame(rows), schedule


def run_us_reliable_pipeline(observations: pd.DataFrame | str | Path, output_dir: str | Path, *,
                             config: dict | None = None, now: datetime | None = None,
                             news_context: dict | str | Path | None = None) -> dict:
    config = config or {}
    issued = _utc(now if now is not None else datetime.now(UTC))
    data = observations.copy() if isinstance(observations, pd.DataFrame) else pd.read_csv(observations)
    raw_settings = dict(config.get("settings", {}))
    if "model_families" in raw_settings:
        raw_settings["model_families"] = tuple(raw_settings["model_families"])
    settings = ReliableSettings(**raw_settings)
    event_source = news_context if news_context is not None else config.get("event_context", {}).get("path")
    events = event_source if isinstance(event_source, dict) else json.loads(Path(event_source).read_text()) if event_source and Path(event_source).is_file() else {}
    news_inputs = _load_news_inputs(config, events)
    contexts, prepared, reasons = {}, {}, []
    for market, symbol in (("US_SPX", "^GSPC"), ("US_NASDAQ", "^IXIC")):
        frame, schedule = build_us_target_frame(data, symbol=symbol, reporting_cutoff=issued)
        if frame.empty:
            reasons.append(f"{market}:missing_source")
            continue
        finished = schedule.loc[pd.to_datetime(schedule.session_close_at, utc=True) <= issued]
        if finished.empty:
            reasons.append(f"{market}:no_completed_session")
            continue
        asof = finished.iloc[-1].session_date
        following = schedule.loc[schedule.session_date > asof].iloc[0]
        context = {"status": "ready", "asof_date": asof, "target_date": following.session_date,
                   "expected_asof_date": asof, "prediction_cutoff": issued.isoformat(),
                   "issued_at": None, "run_started_at": issued.isoformat(), "stage": "us_after_close_preopen_research",
                   "issuance_deadline": following.session_open_at, "target_close_at": following.session_close_at,
                   "calendar_source": "exchange_calendars:XNYS", "timezone": "America/New_York", "reasons": []}
        latest = frame.loc[frame.asof_date == asof]
        if latest.empty or latest[PRICE_FEATURES].isna().any(axis=None):
            reasons.append(f"{market}:expected_completed_bar_or_price_history_missing")
        elif issued >= _utc(following.session_open_at):
            reasons.append(f"{market}:target_session_already_open")
        contexts[market], prepared[market] = context, frame
    output = Path(output_dir)
    if reasons:
        blocked = {"status": "blocked_us_input_or_deadline", "issued_at": None, "run_started_at": issued.isoformat(),
                   "markets": [], "production_weight": 0.0, "reasons": reasons, "contexts": contexts}
        _atomic_json(output / "reliable_index_forecast.json", blocked)
        return blocked
    code_hash = _hash({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                      for name in ("us_market_pipeline.py", "us_market_inputs.py", "reliable.py", "reliable_pipeline.py", "event_context.py", "international_news.py")})
    run = {"schema_version": "reliable_index_forecast_v1", "status": "unpublished", "issued_at": None,
           "artifact_written_at": None, "run_started_at": issued.isoformat(), "prepared_at": None,
           "publication_manifest": "reliable_publication.json", "replay": now is not None,
           "production_weight": 0.0, "horizon": "next_US_session", "code_hash": code_hash,
           "config_hash": _hash({**config, "settings": asdict(settings)}), "data_hash": _hash({"observations": _frame_hash(data), "news": events, "news_snapshot": news_inputs["snapshot"]}),
           "restrictions": ["retrospective end-of-session reconstruction", "no production approval", "news not a trained alpha feature"],
           "interval_note": "80% target coverage, not a guarantee; own-market mature OOF only", "markets": []}
    for market, context in contexts.items():
        frame = prepared[market]
        history = frame.dropna(subset=[*PRICE_FEATURES, "label_return", "target_date"])
        evaluation = evaluate_reliable_forecasts(history, settings=settings, reporting_cutoff=issued)
        latest = frame.loc[frame.asof_date == context["asof_date"]].copy()
        latest["prediction_cutoff"] = issued.isoformat()
        forecast = forecast_reliable_latest(history, latest, evaluation, settings=settings)
        close = float(latest.close.iloc[-1])
        forecast.update(target_date=context["target_date"], target_close_at=context["target_close_at"], latest_close=close,
                        stage=context["stage"], issued_at=None, run_started_at=issued.isoformat(), artifact_written_at=None,
                        expected_index=None if forecast["expected_return"] is None else close * (1 + forecast["expected_return"]),
                        index_range_low=None if forecast["range_low"] is None else close * (1 + forecast["range_low"]),
                        index_range_high=None if forecast["range_high"] is None else close * (1 + forecast["range_high"]))
        news_report = _apply_international_news(evaluation, forecast, market=market, cutoff=issued,
            target_close_at=context["target_close_at"], news_inputs=news_inputs, minimum_oos=settings.min_oos_rows)
        forecast = news_report["blended_forecast"]
        feature_hash = _frame_hash(latest[["asof_date", *PRICE_FEATURES]])
        model_hash = _hash({"selected": evaluation.get("selected"), "model_params": evaluation.get("model_params"),
                            "settings": asdict(settings), "training_data_hash": _frame_hash(history), "code_hash": code_hash})
        forecast_id = _hash({"market": market, "context": context, "data_hash": run["data_hash"], "code_hash": code_hash,
                             "config_hash": run["config_hash"], "feature_hash": feature_hash, "model_hash": model_hash})
        forecast["forecast_id"] = forecast_id
        run["markets"].append({"market": market, "forecast_id": forecast_id, "context": context, "forecast": forecast,
            "feature_hash": feature_hash, "model_hash": model_hash,
            "evaluation": evaluation, "active_feature_groups": {"price": PRICE_FEATURES}, "missing_feature_groups": [],
            "event_context": build_event_risk_context(events, cutoff=issued),
            "international_news": news_report,
            "data_provenance": {"historical_mode": HISTORICAL_MODE, "original_publication_verified": False,
                                "sources": sorted(data.source.dropna().unique()), "retrieved_at": str(data.fetched_at.max()),
                                "target_timezone": "America/New_York"},
            "evidence_cards": [us_evidence_card({"eligible_rows": len(history)}, own_market=True)],
            "participant_pressure": {"status": "not_used", "observed": {}, "inferred": "inconclusive", "confidence": "unavailable", "unverified": [], "missing": []},
            "position_subproject": {"status": "not_used"}})
    run["prepared_at"] = _utc(datetime.now(UTC)).isoformat() if now is None else None
    run["run_id"] = _hash(run)
    documents = {f"archive/{run['run_id']}.json": run, "reliable_index_forecast.json": run,
                 "reliable_validation_report.json": {"run_id": run["run_id"], "status": "unpublished", "markets": [
                     {"market": item["market"], "forecast_id": item["forecast_id"], "evaluation": item["evaluation"]} for item in run["markets"]]},
                 "reliable_model_card.json": {"run_id": run["run_id"], "status": "unpublished", "settings": asdict(settings),
                                             "candidate_matrix": {"US_SPX": ["price"], "US_NASDAQ": ["price"]},
                                             "markets": [{"market": m["market"], "forecast_id": m["forecast_id"], "model_hash": m["model_hash"],
                                                          "feature_hash": m["feature_hash"], "target_close_at": m["context"]["target_close_at"]} for m in run["markets"]],
                                             "historical_mode": HISTORICAL_MODE, "production_weight": 0.0}}
    return _publish_staged_run(run, output, documents, contexts=contexts, replay=now is not None)

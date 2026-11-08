"""Timestamped current-event reporting, deliberately separate from ML features."""
from __future__ import annotations

from datetime import timedelta

import pandas as pd

from roboquant.index_forecast.us_market_inputs import _utc


def _event_clocks(event: dict):
    published, seen, updated = (_utc(event.get(key)) for key in ("published_at", "first_seen_at", "updated_at"))
    version_seen = _utc(event.get("version_first_seen_at") or event.get("version_received_at"))
    receipt = max((t for t in (seen, version_seen) if not pd.isna(t)), default=pd.NaT)
    available = max((t for t in (published, receipt, updated) if not pd.isna(t)), default=pd.NaT)
    return published, receipt, updated, available


def build_event_risk_context(payload: dict | None, *, cutoff, lookback_hours: int = 72) -> dict:
    stamp = _utc(cutoff)
    result = {"status": "missing", "cutoff": stamp.isoformat(), "event_count": 0,
              "news_ml_status": "not_validated_not_used", "probability_adjustment": 0,
              "event_time_inferred_from_publication": False, "observed": [], "unverified": [],
              "conditional_scenarios": [], "channels": [], "excluded": [],
              "interpretation": "Verified publication is not independent verification of each attributed incident. No calibrated news effect or causal magnitude is estimated."}
    if not payload or payload.get("schema_version") != "verified_event_context_v1" or pd.isna(stamp):
        return result
    seen_ids, seen_urls = set(), set()
    # Prefer the newest eligible version; an unavailable newer version must not
    # consume the dedup key and hide a previously known statement.
    ordered = sorted(payload.get("events", []), key=lambda e: (
        _event_clocks(e)[3].value if not pd.isna(_event_clocks(e)[3]) else -1,
        _event_clocks(e)[0].value if not pd.isna(_event_clocks(e)[0]) else -1), reverse=True)
    for event in ordered:
        identifier, url = event.get("event_id"), event.get("source_url")
        published, first_seen, updated, available = _event_clocks(event)
        date_only = pd.to_datetime(event.get("published_date"), errors="coerce")
        old = (published < stamp - timedelta(hours=lookback_hours) if not pd.isna(published)
               else pd.isna(date_only) or date_only.date() < (stamp - timedelta(hours=lookback_hours)).date())
        reason = ("missing_receipt_time" if pd.isna(first_seen)
                  else "invalid_update_timestamp" if event.get("updated_at") and pd.isna(updated)
                  else "not_available_at_cutoff" if available > stamp
                  else "outside_current_context_window" if old
                  else "duplicate" if identifier in seen_ids or url in seen_urls else None)
        if reason or not identifier or not url:
            result["excluded"].append({"event_id": identifier, "reason": reason or "missing_source_identity"})
            continue
        seen_ids.add(identifier)
        seen_urls.add(url)
        observed = {key: event.get(key) for key in ("event_id", "title", "source", "source_url", "published_at", "published_date", "first_seen_at", "updated_at", "version_first_seen_at", "verification_status", "claims")}
        observed["available_at"] = available.isoformat()
        observed["availability_basis"] = "actual_receipt_publication_clock_unknown" if pd.isna(published) else "publication_and_actual_receipt"
        result["observed"].append(observed)
        for claim in event.get("claims", []):
            if isinstance(claim, dict) and any(word in str(claim.get("verification_status", "")).lower() for word in ("unconfirmed", "unverified", "attributed", "anonymous")):
                result["unverified"].append({"event_id": identifier, **claim})
        scenarios = event.get("scenario_conditions", [])
        if isinstance(scenarios, dict):
            scenarios = [f"{key}: {condition}" for key, conditions in scenarios.items() for condition in conditions]
        result["conditional_scenarios"].extend(scenarios)
        result["channels"].extend(event.get("channels", []))
    result.update(status="current_context_only" if result["observed"] else "unavailable",
                  event_count=len(result["observed"]), channels=sorted(set(result["channels"])),
                  bounded_search_result=payload.get("bounded_search_result"))
    return result

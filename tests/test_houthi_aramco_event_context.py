"""User-receipt Houthi context stays unverified and out of the excluded headlines."""
from __future__ import annotations

import json
from pathlib import Path

from roboquant.index_forecast.event_context import build_event_risk_context
from roboquant.index_forecast.international_news import ALPHA_GRID

ROOT = Path(__file__).resolve().parents[1]
RECEIPT = ROOT / "docs" / "data" / "event_context" / "houthi_aramco_20261108.json"
IRAN = ROOT / "data" / "processed" / "verified_us_iran_context_20261108.json"
HOUTHI_IDS = {
    "financialjuice_houthi_riyadh_claim_20261108",
    "financialjuice_houthi_abqaiq_claim_20261108",
}
EXCLUDED_URLS = {
    "https://x.com/FirstSquawk/status/2119416299809812677",
    "https://x.com/FirstSquawk/status/2119416435906912292",
    "https://x.com/FirstSquawk/status/2119416473370480898",
    "https://x.com/FirstSquawk/status/2119416512549359978",
}


def test_houthi_claims_are_observed_unverified_and_excluded_receipts_stay_out():
    text = RECEIPT.read_text(encoding="utf-8")
    assert "confirmed_event" not in text
    assert "probability_adjustment" not in text
    payload = json.loads(text)
    assert payload["schema_version"] == "verified_event_context_v1"
    assert ALPHA_GRID == (0.0, 0.05, 0.10, 0.20)
    event_urls = {event["source_url"] for event in payload["events"]}
    assert EXCLUDED_URLS.isdisjoint(event_urls)
    excluded = payload["excluded_receipts"]
    assert {item["source_url"] for item in excluded} == EXCLUDED_URLS
    assert {item["reason"] for item in excluded} == {"not_index_relevant"}
    if IRAN.is_file():
        original = json.loads(IRAN.read_text(encoding="utf-8"))["events"]
        stored = {event["event_id"]: event for event in payload["events"]}
        for event in original:
            assert stored[event["event_id"]] == event
    stored = {event["event_id"]: event for event in payload["events"]}
    for event_id in HOUTHI_IDS:
        assert stored[event_id]["verification_status"] == "reported_unverified"
        assert stored[event_id]["claim_status"] == "reported_unverified"
        assert set(stored[event_id]["channels"]) == {"shipping_energy", "escalation"}
    result = build_event_risk_context(payload, cutoff="2026-11-09T07:50:00+09:00")
    observed = {event["event_id"]: event for event in result["observed"]}
    unverified = {claim["event_id"] for claim in result["unverified"]}
    assert HOUTHI_IDS <= set(observed)
    assert HOUTHI_IDS <= unverified
    assert result["probability_adjustment"] == 0
    assert {"shipping_energy", "escalation"} <= set(result["channels"])
    rendered = json.dumps(result)
    for url in EXCLUDED_URLS:
        assert url not in rendered
    for event_id in HOUTHI_IDS:
        assert observed[event_id]["verification_status"] == "reported_unverified"

import json
import sqlite3
from pathlib import Path

from roboquant.index_forecast.automation_state import public_payload_paths
from roboquant.index_forecast.official_credit import CreditSourceError
from scripts import collect_market_indicators as tracker

ROOT = Path(__file__).resolve().parents[1]
RECEIVED = "2026-11-19T12:45:00+00:00"


def observation(series, value, source, *, market="GLOBAL", day="2026-11-18", unit="percent"):
    return {
        "series": series,
        "market": market,
        "value": value,
        "unit": unit,
        "source": source,
        "observation_date": day,
        "received_at": RECEIVED,
        "published_at": None,
        "is_final": None,
        "availability_policy": "observed",
    }


def test_credit_failure_does_not_discard_successful_yield_collection(monkeypatch, tmp_path):
    def unavailable(**_):
        raise CreditSourceError("upstream unavailable")

    monkeypatch.setattr(tracker, "collect_credit_balances", unavailable)
    monkeypatch.setattr(
        tracker,
        "collect_us_yields",
        lambda **_: [observation("US_YIELD_3Y", 4.84, "us_treasury_daily_par_yield_curve")],
    )
    output, db = tmp_path / "public", tmp_path / "private.sqlite"
    report = tracker.collect_indicators(
        sqlite=db, output=output, seed=output, ecos_key="", fsc_key=""
    )
    assert report["sources"]["credit"] == {"status": "unavailable", "reason": "source_error"}
    assert report["observations"][0]["value"] == 4.84


def test_partial_run_keeps_market_chart_and_appends_keyless_source(monkeypatch, tmp_path):
    seed, output, db = ROOT / "docs/data", tmp_path / "public", tmp_path / "private.sqlite"
    monkeypatch.setattr(
        tracker,
        "collect_credit_balances",
        lambda **_: [
            observation(
                "MARGIN_CREDIT_BALANCE",
                26_266_366_000_000,
                "kofia_freesis_margin_credit",
                unit="KRW",
            )
        ],
    )
    monkeypatch.setattr(
        tracker,
        "collect_us_yields",
        lambda **_: [observation("US_YIELD_1Y", 4.62, "us_treasury_daily_par_yield_curve")],
    )
    report = tracker.collect_indicators(
        sqlite=db, output=output, seed=seed, ecos_key="", fsc_key=""
    )

    assert report["sources"]["kr_yields"]["reason"] == "missing_credentials"
    assert report["sources"]["credit"]["status"] == "partial"
    assert (output / "margin_credit_chart.json").read_bytes() == (
        seed / "margin_credit_chart.json"
    ).read_bytes()
    latest = {(row["market"], row["series"]): row for row in report["observations"]}
    assert latest["KOSPI", "MARGIN_CREDIT_BALANCE"]["tracking_status"] == "retained"
    assert latest["GLOBAL", "US_YIELD_1Y"]["value"] == 4.62
    assert latest["GLOBAL", "MARGIN_CREDIT_BALANCE"]["value"] == 26_266_366_000_000
    assert {path.name for path in public_payload_paths(output)} == {
        "margin_credit_chart.json",
        "market_indicators_status.json",
    }
    before = db.read_bytes()
    report = tracker.collect_indicators(
        sqlite=db, output=output, seed=output, ecos_key="", fsc_key=""
    )
    assert report["inserted"] == 0
    assert db.read_bytes() == before


def test_validated_market_split_extends_graph_and_source_failure_is_redacted(monkeypatch, tmp_path):
    seed, output, db = ROOT / "docs/data", tmp_path / "public", tmp_path / "private.sqlite"
    monkeypatch.setattr(
        tracker,
        "collect_credit_balances",
        lambda **_: [
            observation(
                "MARGIN_CREDIT_BALANCE",
                value,
                "fsc_kofia_margin_credit",
                market=market,
                day="2026-11-19",
                unit="KRW",
            )
            for market, value in (("KOSPI", 20_000_000_000_000), ("KOSDAQ", 5_000_000_000_000))
        ],
    )

    def unavailable(**_):
        raise OSError("credential-in-url")

    monkeypatch.setattr(tracker, "collect_us_yields", unavailable)
    report = tracker.collect_indicators(
        sqlite=db, output=output, seed=seed, ecos_key="", fsc_key="configured"
    )
    assert report["sources"]["us_yields"]["status"] == "unavailable"
    assert "credential-in-url" not in (output / "market_indicators_status.json").read_text()
    chart = json.loads((output / "margin_credit_chart.json").read_bytes())
    assert chart["markets"]["KOSPI"][-1] == {
        "date": "2026-11-19",
        "balance_krw": 20_000_000_000_000,
    }
    assert all(len(rows) == 90 for rows in chart["markets"].values())
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

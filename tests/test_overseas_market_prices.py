from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from roboquant.index_forecast.automation_state import public_payload_paths
from scripts import export_overseas_market_prices as exporter

CHECK = datetime(2026, 11, 25, 12, tzinfo=UTC)


def _timestamp(day):
    return int(datetime.fromisoformat(f"{day}T14:30:00+00:00").timestamp())


def _provider(symbol, dated_prices, *, share_type=None, splits=None):
    days, prices = zip(*dated_prices, strict=True)
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": "USD",
                        "exchangeTimezoneName": "America/New_York",
                        "exchangeName": "NMS" if symbol == "SKHY" else "PCX",
                        "instrumentType": share_type or ("EQUITY" if symbol == "SKHY" else "ETF"),
                    },
                    "timestamp": [_timestamp(day) for day in days],
                    "indicators": {"quote": [{"close": prices}]},
                    "events": {"splits": splits or {}},
                }
            ],
        }
    }


def test_only_completed_us_market_sessions_are_eligible():
    snapshot = _provider(
        "SKHY",
        [
            ("2026-11-20", 120),
            ("2026-11-21", 900),
            ("2026-11-23", 116),
            ("2026-11-24", 111.98),
            ("2026-11-25", 500),
            ("2026-11-26", 700),
        ],
    )
    rows = exporter.market_points("SKHY", snapshot, CHECK)
    assert rows == [
        {"date": "2026-11-20", "close": 120},
        {"date": "2026-11-23", "close": 116},
        {"date": "2026-11-24", "close": 111.98},
    ]


def test_early_close_includes_settlement_delay():
    day = date(2026, 11, 27)
    assert exporter.latest_session(datetime(2026, 11, 27, 18, 10, tzinfo=UTC)) == date(2026, 11, 25)
    assert exporter.latest_session(datetime(2026, 11, 27, 18, 16, tzinfo=UTC)) == day


def test_wrong_instrument_and_unadjusted_split_are_rejected():
    with pytest.raises(ValueError, match="identity"):
        exporter.market_points("SKHY", _provider("HXSCL", [("2026-11-24", 20)]), CHECK)
    split = {"seen": {"date": _timestamp("2026-07-15"), "numerator": 20, "denominator": 1}}
    rows = [("2026-07-14", 200), ("2026-07-15", 10)]
    with pytest.raises(ValueError, match="split"):
        exporter.market_points("KORU", _provider("KORU", rows, splits=split), CHECK)
    rows[0] = ("2026-07-14", 10)
    assert len(exporter.market_points("KORU", _provider("KORU", rows, splits=split), CHECK)) == 2


def test_collector_retains_only_the_failed_series(tmp_path):
    output = tmp_path / "overseas-market-prices.json"

    def good(symbol):
        return _provider(symbol, [("2026-11-23", 12), ("2026-11-24", 11)]), CHECK

    first = exporter.export_chart(output, as_of=CHECK, fetch=good)
    assert first["status"] == "ready"

    def partial(symbol):
        if symbol == "KORU":
            raise OSError("provider unavailable")
        return _provider(symbol, [("2026-11-23", 13), ("2026-11-24", 12)]), CHECK

    second = exporter.export_chart(output, as_of=CHECK, fetch=partial)
    assert second["status"] == "partial"
    assert second["instruments"]["KORU"]["status"] == "stale"
    assert second["instruments"]["KORU"]["points"] == first["instruments"]["KORU"]["points"]
    assert second["instruments"]["KORU"]["last_session"] == "2026-11-24"
    assert second["instruments"]["SKHY"]["points"][-1]["close"] == 12


def test_chart_files_are_reviewed_public_payloads(tmp_path):
    names = ("overseas-market-prices.json", "sk-hynix-investor-flow.json")
    for name in (*names, "private-raw.json"):
        (tmp_path / name).write_text("{}")
    assert {p.name for p in public_payload_paths(tmp_path)} == set(names)

    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/premarket-research-forecast.yml").read_text()
    for name in names:
        assert f"--output docs/data/{name}" in workflow

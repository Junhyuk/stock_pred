import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_unofficial_numerics", ROOT / "scripts" / "collect_unofficial_numerics.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_naver_trend_keeps_signed_flow_and_session_date():
    rows = MODULE.parse_naver_trend(
        {"bizdate": "20261120", "personalValue": "+15,381", "foreignValue": "-10,956",
         "institutionalValue": "-9,330"},
        "KOSPI",
    )
    foreign = next(row for row in rows if row["id"] == "kospi_foreignValue")
    assert foreign["value"] == -10956
    assert foreign["display"] == "-10,956억"
    assert foreign["date"] == "2026-11-20"
    assert foreign["unit"] == "억원"
    assert len(rows) == 3


def test_basis_requires_the_same_session():
    future = MODULE.parse_naver_basic(
        {"closePrice": "752.75", "fluctuationsRatio": "-5.43", "localTradedAt": "2026-11-20T15:34:59+09:00"},
        series_id="kospi200_futures", label="KOSPI200 선물")
    spot = MODULE.parse_naver_basic(
        {"closePrice": "755.34", "localTradedAt": "2026-11-20T20:15:00+09:00"},
        series_id="kospi200", label="KOSPI200")
    basis = MODULE.derive_basis(future, spot)
    assert basis["value"] == -2.59
    assert basis["date"] == "2026-11-20"
    spot["date"] = "2026-11-19"
    assert MODULE.derive_basis(future, spot) is None


def test_yahoo_chart_uses_exchange_date_and_percent_display():
    payload = {"chart": {"result": [{"meta": {
        "regularMarketPrice": 4.912,
        "regularMarketTime": 1795204795,
        "exchangeTimezoneName": "America/New_York",
        "regularMarketChangePercent": -2.771,
    }}]}}
    row = MODULE.parse_yahoo_chart(payload, series_id="us10y", label="미국 10년 금리", unit="percent")
    assert row["display"] == "4.912%"
    assert row["change_pct"] == -2.771
    assert row["date"] == MODULE.datetime.fromtimestamp(1795204795, MODULE.ZoneInfo("America/New_York")).date().isoformat()
    assert MODULE.parse_yahoo_chart({}, series_id="x", label="x", unit="index") is None


def test_payload_does_not_assign_a_probability_weight():
    payload = MODULE.build_payload([{"id": "vix"}], received_at="2026-11-21T00:00:00+00:00")
    assert payload["schema_version"] == "unofficial_numerics_v1"
    assert payload["production_weight"] == 0.0
    assert "p_up" not in payload

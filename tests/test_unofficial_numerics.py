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


def test_requested_market_prices_include_crypto_brent_and_yield_curve():
    symbols = {item[2] for item in MODULE.YAHOO}
    assert {"KRW=X", "DX-Y.NYB", "BTC-USD", "ETH-USD", "CL=F", "BZ=F"} <= symbols
    treasury = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
      <entry><content><m:properties>
        <d:NEW_DATE>2026-11-19T00:00:00</d:NEW_DATE>
        <d:BC_1YEAR>3.50</d:BC_1YEAR><d:BC_3YEAR>3.70</d:BC_3YEAR><d:BC_5YEAR>3.90</d:BC_5YEAR><d:BC_10YEAR>4.10</d:BC_10YEAR>
      </m:properties></content></entry>
      <entry><content><m:properties>
        <d:NEW_DATE>2026-11-20T00:00:00</d:NEW_DATE>
        <d:BC_1YEAR>3.60</d:BC_1YEAR><d:BC_3YEAR>3.80</d:BC_3YEAR><d:BC_5YEAR>4.00</d:BC_5YEAR><d:BC_10YEAR>4.20</d:BC_10YEAR>
      </m:properties></content></entry>
    </feed>"""
    us = {row["id"]: row for row in MODULE.parse_treasury_latest(treasury.encode())}
    assert us["us_10y"]["display"] == "4.200%"
    assert us["us_5y"]["date"] == "2026-11-20"
    assert us["us_1y"]["change_display"] == "+0.100%p"
    assert us["us_1y"]["change_pct"] is None
    japan = "title\nDate,1Y,2Y,3Y,5Y,10Y\n2026/10/29,0.50,0.60,0.70,0.90,1.10\n2026/10/30,0.55,0.60,0.77,0.99,1.21\n"
    jp = {row["id"]: row for row in MODULE.parse_japan_latest(japan.encode())}
    assert jp["jp_10y"]["value"] == 1.21
    assert jp["jp_3y"]["date"] == "2026-10-30"
    assert jp["jp_1y"]["change_display"] == "+0.050%p"
    total, points = MODULE.parse_korea_page({"StatisticSearch": {"list_total_count": 1, "row": {"TIME": "20261120", "DATA_VALUE": "3.100"}}})
    assert total == 1 and points == [("2026-11-20", 3.1)]
    page = (ROOT / "docs/reliable-index-forecast.html").read_text()
    script = (ROOT / "docs/assets/cross-assets.js").read_text()
    assert 'id="cross-assets"' in page and "assets/cross-assets.js" in page
    for label in ("원/달러", "달러 인덱스", "비트코인", "이더리움", "서부 텍사스 원유", "브렌트유", "미국 국채 5년", "한국 국고채 5년", "일본 국채 10년"):
        assert label in script


def test_payload_does_not_assign_a_probability_weight():
    payload = MODULE.build_payload([{"id": "vix"}], received_at="2026-11-21T00:00:00+00:00")
    assert payload["schema_version"] == "unofficial_numerics_v1"
    assert payload["production_weight"] == 0.0
    assert "p_up" not in payload

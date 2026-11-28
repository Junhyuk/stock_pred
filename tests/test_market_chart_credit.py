import json
from datetime import UTC, date, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts import market_chart_credit as credit

FIRST = date(2023, 11, 28)
LAST = date(2026, 11, 28)
CUTOFF = datetime(2026, 11, 28, 12, tzinfo=UTC)


def workbook_rows():
    return [
        ("신용공여 잔고 추이",),
        ("[단위 :백만원]",),
        (None, "신용거래융자"),
        (None, "전체", "유가증권", "코스닥"),
        ("2023/11/27", "17,000,000", 11_000_000, 6_000_000),
        ("2023/11/28", "17,204,508", 11_204_508, 6_000_000),
        ("2026/11/18", "26,266,366", 20_323_672, 5_942_694, 10_000),
    ]


def test_workbook_extracts_only_official_total_and_normalizes_units():
    assert credit.workbook_points(iter(workbook_rows()), FIRST, LAST) == {
        "2023-11-28": 17_204_508_000_000,
        "2026-11-18": 26_266_366_000_000,
    }
    rows = workbook_rows()
    rows[2] = (None, "예탁증권담보융자")
    with pytest.raises(ValueError):
        credit.workbook_points(iter(rows), FIRST, LAST)


def test_api_requires_matching_official_unit_and_includes_last_day():
    day = "2026-11-25"
    item = {"basDt": "20261125", "crdTrFingWhl": 25_412_277,
            "crdTrFingScrs": 20_000_000, "crdTrFingKosdaq": 5_412_277}

    def fetch(url):
        params = parse_qs(urlsplit(url).query)
        assert params["beginBasDt"] == ["20231128"]
        assert params["endBasDt"] == ["20261129"]
        assert params["serviceKey"] == ["key/with+chars"]
        return json.dumps({"response": {"header": {"resultCode": "00"},
                           "body": {"totalCount": 1, "items": {"item": item}}}})

    assert credit.fsc_points(FIRST, LAST, fetch, "key%2Fwith%2Bchars", {day: 25_412_277_000_000}) == {
        day: 25_412_277_000_000,
    }
    with pytest.raises(ValueError, match="단위"):
        credit.fsc_points(FIRST, LAST, fetch, "key%2Fwith%2Bchars", {day: 50_000})


def test_missing_or_failed_api_keeps_only_dated_official_total(tmp_path, monkeypatch):
    monkeypatch.delenv("KOFIA_CREDIT_XLSX", raising=False)
    monkeypatch.delenv("DATA_GO_KR_SERVICE_KEY", raising=False)
    row = dict(market="GLOBAL", series="MARGIN_CREDIT_BALANCE", unit="KRW",
               source="kofia_freesis_margin_credit", observation_date="2026-11-25",
               received_at="2026-11-26T23:04:00Z", value=25_412_277_000_000.0)
    (tmp_path / "market_indicators_status.json").write_text(json.dumps({
        "schema_version": "market_indicators_status_v1",
        "observations": [row, {**row, "observation_date": "2026-11-27", "received_at": "2026-11-30T00:00:00Z"},
                         {**row, "market": "KOSPI"}, {**row, "observation_date": "2026-11-22", "is_final": False}],
    }))
    (tmp_path / "market-indicator-catalog.json").write_text(json.dumps({
        "schema_version": "market_indicator_catalog_v1",
        "series": [{"id": "official_margin_credit_balance_global", "unit": "KRW", "source": row["source"],
                    "updated_at": row["received_at"], "points": [["2026-11-24", 25_877_746_000_000]]}],
    }))
    calls = []

    def offline(url):
        calls.append(url)
        raise OSError("official source temporarily unavailable")

    expected = {credit.SERIES: {"2026-11-24": 25_877_746_000_000, "2026-11-25": 25_412_277_000_000}}
    assert credit.credit_history(FIRST, LAST, {}, offline, public_dir=tmp_path, cutoff=CUTOFF) == expected
    assert calls == []
    monkeypatch.setenv("DATA_GO_KR_SERVICE_KEY", "configured")
    assert credit.credit_history(FIRST, LAST, {}, offline, public_dir=tmp_path, cutoff=CUTOFF) == expected
    assert len(calls) == 1

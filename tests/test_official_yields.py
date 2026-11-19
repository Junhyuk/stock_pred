import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from roboquant.index_forecast import official_yields as oy

RECEIPT = "2026-11-19T12:35:00+00:00"
ASOF = datetime(2026, 11, 19, 12, 30, tzinfo=UTC)


def xml(days):
    rows = "".join(
        "<entry><content><m:properties><d:NEW_DATE>"
        + day
        + "T00:00:00</d:NEW_DATE><d:BC_1YEAR>4.62</d:BC_1YEAR><d:BC_3YEAR>4.84</d:BC_3YEAR><d:BC_10YEAR>5.07</d:BC_10YEAR></m:properties></content></entry>"
        for day in days
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices" xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">'
        + rows
        + "</feed>"
    ).encode()


def ecos(code, name, days):
    return json.dumps(
        {
            "StatisticSearch": {
                "list_total_count": len(days),
                "row": [
                    {
                        "STAT_CODE": "817Y002",
                        "ITEM_CODE1": code,
                        "ITEM_NAME1": name,
                        "UNIT_NAME": "연%",
                        "TIME": d,
                        "DATA_VALUE": "3.6",
                    }
                    for d in days
                ],
            }
        }
    ).encode()


def test_treasury_yields_are_bounded_mapped_and_received_at_download():
    with patch.object(
        oy, "_download", return_value=(xml(["2026-11-17", "2026-11-18", "2026-11-20"]), RECEIPT)
    ):
        rows = oy.collect_us_yields(as_of=ASOF)
    assert {r["series"] for r in rows} == {"US_YIELD_1Y", "US_YIELD_3Y", "US_YIELD_10Y"}
    assert {r["observation_date"] for r in rows} == {"2026-11-17", "2026-11-18"}
    assert all(
        r["received_at"] == RECEIPT
        and r["published_at"] is None
        and r["unit"] == "percent"
        and r["market"] == "GLOBAL"
        for r in rows
    )


def test_treasury_january_uses_preceding_year_only_when_needed():
    def download(url):
        return (
            xml(["2025-12-30", "2025-12-31"]) if url.endswith("=2025") else xml(["2026-01-02"])
        ), RECEIPT

    with patch.object(oy, "_download", side_effect=download):
        rows = oy.collect_us_yields(as_of=datetime(2026, 1, 4, tzinfo=UTC))
    assert {r["observation_date"] for r in rows} == {"2025-12-30", "2025-12-31", "2026-01-02"}


def test_ecos_maturity_identity_matching_and_credential_redaction():
    names = {"010190000": "국고채(1년)", "010200000": "국고채(3년)", "010210000": "국고채(10년)"}

    def download(url):
        code = url.rsplit("/", 1)[1]
        return ecos(code, names[code], ["20261117", "20261118", "20261120"]), RECEIPT

    with patch.object(oy, "_download", side_effect=download):
        rows = oy.collect_kr_yields(as_of=ASOF, api_key="secretvalue")
    assert len(rows) == 6
    assert {r["series"] for r in rows} == {"KR_YIELD_1Y", "KR_YIELD_3Y", "KR_YIELD_10Y"}
    assert {r["observation_date"] for r in rows} == {"2026-11-17", "2026-11-18"}
    assert all(
        r["source"] == "bok_ecos_817Y002"
        and r["published_at"] is None
        and r["received_at"] == RECEIPT
        for r in rows
    )
    with patch.object(oy, "_download", side_effect=RuntimeError("secretvalue")):
        with pytest.raises(ValueError) as err:
            oy.collect_kr_yields(as_of=ASOF, api_key="secretvalue")
    assert "secretvalue" not in str(err.value)


def test_ecos_rejects_incomplete_tenor_mix_and_sample_key():
    names = {"010190000": "국고채(1년)", "010200000": "국고채(3년)", "010210000": "국고채(10년)"}

    def download(url):
        code = url.rsplit("/", 1)[1]
        return ecos(
            code, names[code], ["20261117"] if code == "010210000" else ["20261118"]
        ), RECEIPT

    with patch.object(oy, "_download", side_effect=download):
        with pytest.raises(ValueError):
            oy.collect_kr_yields(as_of=ASOF, api_key="secretvalue")
    for key in ("", "sample"):
        with pytest.raises(ValueError):
            oy.collect_kr_yields(as_of=ASOF, api_key=key)


def test_both_collectors_reject_naive_asof():
    for fn in (
        lambda: oy.collect_us_yields(as_of=datetime(2026, 1, 1)),
        lambda: oy.collect_kr_yields(as_of=datetime(2026, 1, 1), api_key="abc123"),
    ):
        with pytest.raises(ValueError):
            fn()

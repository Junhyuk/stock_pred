import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo


DATA_PATH = Path(__file__).resolve().parents[1] / "docs/data/overseas-institutional-holdings.json"


def test_selected_sec_filings_have_valid_identifiers_and_dates():
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "status", "as_of", "coverage", "securities"}
    assert data["schema_version"] == 1
    assert data["status"] == "ready"
    assert data["coverage"] == "selected_filings"
    as_of = date.fromisoformat(data["as_of"])

    expected_cusips = {"SKHY": "78392B206", "KORU": "25461A387"}
    assert set(data["securities"]) == set(expected_cusips)
    for symbol, security in data["securities"].items():
        assert set(security) == {"cusip", "filings"}
        assert security["cusip"] == expected_cusips[symbol]
        assert 1 <= len(security["filings"]) <= 3
        accessions = set()
        for filing in security["filings"]:
            assert set(filing) == {
                "manager", "shares", "market_value_usd", "period_end",
                "filed_at", "accession", "table_url",
            }
            period_end = date.fromisoformat(filing["period_end"])
            filed_at = datetime.fromisoformat(filing["filed_at"])
            assert (period_end.month, period_end.day) in {(3, 31), (6, 30), (9, 30), (12, 31)}
            if symbol == "SKHY":
                assert period_end >= date(2026, 7, 10)
            assert period_end < filed_at.date() <= as_of
            assert filed_at.utcoffset() is not None
            assert filed_at.astimezone(ZoneInfo("America/New_York")).utcoffset() == filed_at.utcoffset()

            accession = filing["accession"]
            assert re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession)
            assert accession[11:13] == str(filed_at.year)[-2:]
            assert accession not in accessions
            accessions.add(accession)
            source = urlsplit(filing["table_url"])
            assert (source.scheme, source.netloc) == ("https", "www.sec.gov")
            assert re.fullmatch(
                rf"/Archives/edgar/data/\d+/{accession.replace('-', '')}/xslForm13F_X02/[^/]+\.xml",
                source.path,
            )

            assert isinstance(filing["manager"], str) and filing["manager"].strip()
            for field in ("shares", "market_value_usd"):
                assert type(filing[field]) is int and filing[field] > 0

import importlib
import io
import json
from datetime import datetime
from email.message import Message

import pytest


@pytest.fixture
def collector():
    return importlib.import_module("scripts.collect_frees_is_cfd")


def row(day, amounts=(100, 200, 0, 400, 0, 10, 20, 0, 40, 0, 770)):
    return {"TMPV1": day, **{f"TMPV{index}": amount for index, amount in enumerate(amounts, 2)}}


def serve(monkeypatch, collector, *datasets):
    requests = []
    replies = iter(datasets)

    def open_request(request, *, timeout):
        requests.append((request, timeout))
        data = json.dumps({"unit": "", "ds1": next(replies), "dsmHeader": ""}).encode()
        response = io.BytesIO(data)
        response.headers = Message()
        response.headers["Content-Type"] = "application/json;charset=UTF-8"
        response.geturl = lambda: request.full_url
        return response

    monkeypatch.setattr(collector, "urlopen", open_request)
    return requests


def test_fetch_validated_metric_scopes_and_krw_balances(collector, monkeypatch):
    with_margin = [row("20261124"), row("20261123")]
    without_margin = [
        row(day, (50, 90, 0, 210, 0, 5, 2, 0, 11, 0, 368))
        for day in ("20261123", "20261124")
    ]
    requests = serve(monkeypatch, collector, with_margin, without_margin)

    result = collector.fetch_totals("2026-11-23", "2026-11-24")

    included = {
        "total_krw": 770, "kospi_krw": 110, "kosdaq_krw": 220, "buy_krw": 700,
        "sell_krw": 70, "kospi_buy_krw": 100, "kospi_sell_krw": 10,
        "kosdaq_buy_krw": 200, "kosdaq_sell_krw": 20,
    }
    excluded = {
        "total_krw": 368, "kospi_krw": 55, "kosdaq_krw": 92, "buy_krw": 350,
        "sell_krw": 18, "kospi_buy_krw": 50, "kospi_sell_krw": 5,
        "kosdaq_buy_krw": 90, "kosdaq_sell_krw": 2,
    }
    dates = ("2026-11-23", "2026-11-24")
    assert result == {
        "with_margin": [dict(included, date=day) for day in dates],
        "without_margin": [dict(excluded, date=day) for day in dates],
    }
    assert len(requests) == 2
    for (request, timeout), object_name in zip(
        requests, ("STATDRV0100000320BO", "STATDRV0100000310BO"), strict=True
    ):
        assert request.full_url == "https://freesis.kofia.or.kr/meta/getMetaDataList.do"
        assert request.get_method() == "POST"
        assert 0 < timeout <= 12
        assert json.loads(request.data) == {
            "dmSearch": {
                "tmpV40": "1",
                "tmpV41": "1",
                "tmpV1": "D",
                "tmpV45": "20261123",
                "tmpV46": "20261124",
                "OBJ_NM": object_name,
            }
        }


@pytest.mark.parametrize("first,last", [("20261120", "2026-11-24"), ("2026-11-25", "2026-11-24")])
def test_rejects_invalid_interval_before_contacting_source(collector, monkeypatch, first, last):
    requests = serve(monkeypatch, collector)

    with pytest.raises(ValueError):
        collector.fetch_totals(first, last)

    assert not requests


@pytest.mark.parametrize(
    "malformed",
    [
        [{**row("20261124"), "TMPV12": 771}],
        [{**row("20261124"), "TMPV4": False}],
        [{**row("20261124"), "TMPV4": None}],
        [{**row("20261124"), "TMPV4": -1, "TMPV12": 769}],
        [row("2026112")],
        [row("20261125")],
        [row("20261124"), row("20261124")],
        {"TMPV1": "20261124"},
    ],
)
def test_rejects_malformed_source_rows(collector, monkeypatch, malformed):
    serve(monkeypatch, collector, malformed, [row("20261124")])

    with pytest.raises(ValueError):
        collector.fetch_totals("2026-11-23", "2026-11-24")


def test_rejects_metrics_with_mismatched_dates(collector, monkeypatch):
    serve(monkeypatch, collector, [row("20261123")], [row("20261124")])

    with pytest.raises(ValueError, match="same dates"):
        collector.fetch_totals("2026-11-23", "2026-11-24")


def test_empty_holiday_interval_is_valid(collector, monkeypatch):
    serve(monkeypatch, collector, [], [])

    assert collector.fetch_totals("2026-11-21", "2026-11-21") == {
        "with_margin": [],
        "without_margin": [],
    }


def test_cli_writes_private_snapshot_with_actual_fetch_timestamp(collector, monkeypatch, tmp_path):
    serve(monkeypatch, collector, [row("20261124")], [row("20261124")])
    destination = tmp_path / "frees_is_market.json"

    assert collector.main(["--first", "2026-11-24", "--last", "2026-11-24", "--output", str(destination)]) == 0

    result = json.loads(destination.read_text())
    assert datetime.fromisoformat(result["generated_at"]).utcoffset().total_seconds() == 0
    assert result["series"]["with_margin"][0]["date"] == "2026-11-24"
    assert result["series"]["without_margin"][0]["total_krw"] == 770

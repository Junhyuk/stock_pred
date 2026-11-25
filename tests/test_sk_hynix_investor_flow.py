import importlib.util
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "export_sk_hynix_investor_flow", ROOT / "scripts/export_sk_hynix_investor_flow.py"
)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


NOW = datetime(2026, 11, 24, 16, tzinfo=UTC)


def trend_row(day, foreign="-118,990", institution="-844", individual="-233,523"):
    return {
        "itemCode": "000660",
        "bizdate": day,
        "foreignerPureBuyQuant": foreign,
        "foreignerHoldRatio": "49.32%",
        "organPureBuyQuant": institution,
        "individualPureBuyQuant": individual,
        "closePrice": "1,113,000",
        "compareToPreviousClosePrice": "28,000",
        "compareToPreviousPrice": {"code": "2", "text": "상승", "name": "RISING"},
        "accumulatedTradingVolume": "3,611,127",
    }


def test_refresh_exports_explicit_share_counts_and_keeps_missing_classes_null(tmp_path):
    output = tmp_path / "data" / "investor-flow.json"

    def fetch_page(before):
        assert before == date(2026, 11, 25)
        return [
            trend_row("20261124"),
            trend_row("20261123", foreign="+1,023", institution="0", individual="-"),
        ]

    assert exporter.refresh(output, NOW, fetch_page) == "partial"
    snapshot = json.loads(output.read_text(encoding="utf-8"))
    assert snapshot["schema_version"] == 1
    assert snapshot["status"] == "partial"
    assert snapshot["generated_at"] == "2026-11-24T16:00:00Z"
    assert snapshot["issuer"] == "SK하이닉스"
    assert snapshot["symbol"] == "000660"
    assert snapshot["market"] == "KRX"
    assert snapshot["unit"] == "shares"
    assert snapshot["source"] == {
        "label": "네이버페이 증권",
        "url": "https://m.stock.naver.com/domestic/stock/000660/total",
    }
    assert snapshot["series"] == [
        {"date": "2026-11-23", "foreign": 1023, "institution": 0, "individual": None},
        {"date": "2026-11-24", "foreign": -118990, "institution": -844, "individual": -233523},
    ]


def test_refresh_never_publishes_same_day_future_invalid_or_wrong_symbol(tmp_path):
    output = tmp_path / "flow.json"

    def fetch_page(_before):
        return [
            trend_row("20261201"),
            trend_row("20261125"),
            {**trend_row("20261124"), "itemCode": "005930"},
            trend_row("20261123", individual="+46,433"),
            trend_row("20261122"),
            trend_row("20261131"),
        ]

    assert exporter.refresh(output, NOW, fetch_page) == "ready"
    assert json.loads(output.read_text())["series"] == [
        {"date": "2026-11-23", "foreign": -118990, "institution": -844, "individual": 46433},
    ]


def test_collect_paginates_exclusively_and_caps_history():
    seen = []
    day = date(2026, 11, 24)
    days = []
    while len(days) < 320:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)

    def fetch_page(before):
        seen.append(before)
        return [trend_row(day.strftime("%Y%m%d")) for day in days if day < before][:60]

    rows = exporter.collect_rows(date(2026, 11, 25), fetch_page)
    assert seen == [date(2026, 11, 25), days[59], days[119], days[179], days[239]]
    assert len(rows) == 250
    assert rows[0]["date"] == days[249].isoformat()
    assert rows[-1]["date"] == "2026-11-24"


@pytest.mark.parametrize("failure", ["network", "schema", "empty", "all_fields_missing", "stale"])
def test_refresh_preserves_previous_snapshot_on_failed_or_stale_update(tmp_path, failure):
    output = tmp_path / "flow.json"
    assert exporter.refresh(output, NOW, lambda _: [trend_row("20261124")]) == "ready"
    previous = output.read_bytes()

    def fetch_page(_before):
        if failure == "network":
            raise TimeoutError("provider timed out")
        if failure == "schema":
            return {"unexpected": []}
        if failure == "all_fields_missing":
            return [trend_row("20261124", None, "--", "123.4")]
        if failure == "stale":
            return [trend_row("20261123")]
        return []

    assert exporter.refresh(output, NOW, fetch_page) == "preserved"
    assert output.read_bytes() == previous


def test_refresh_retains_previous_dates_and_directly_reported_values(tmp_path):
    output = tmp_path / "flow.json"
    assert exporter.refresh(
        output, NOW, lambda _: [trend_row("20261124"), trend_row("20261123")]
    ) == "ready"

    def fetch_page(_before):
        return [
            trend_row("20261125", individual=None),
            trend_row("20261124", foreign="-120,000", individual=None),
        ]

    assert exporter.refresh(output, NOW + timedelta(days=1), fetch_page) == "partial"
    rows = json.loads(output.read_text())["series"]
    assert rows == [
        {"date": "2026-11-23", "foreign": -118990, "institution": -844, "individual": -233523},
        {"date": "2026-11-24", "foreign": -120000, "institution": -844, "individual": -233523},
        {"date": "2026-11-25", "foreign": -118990, "institution": -844, "individual": None},
    ]


def test_refresh_writes_unavailable_without_previous_snapshot(tmp_path):
    output = tmp_path / "flow.json"
    assert exporter.refresh(output, NOW, lambda _: []) == "unavailable"
    snapshot = json.loads(output.read_text())
    assert snapshot["status"] == "unavailable"
    assert snapshot["series"] == []


def test_cli_uses_output_path(tmp_path, monkeypatch):
    output = tmp_path / "nested" / "stock-flow.json"
    monkeypatch.setattr(exporter, "fetch_naver_page", lambda _: [trend_row("20230102")])

    assert exporter.main(["--output", str(output)]) == 0
    assert json.loads(output.read_text())["series"][0]["date"] == "2023-01-02"


def test_fetch_naver_page_requests_krx_cursor_and_retries_transient_error(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError

    requests = []

    def open_url(request, *, timeout):
        assert timeout > 0
        requests.append(request.full_url)
        if len(requests) == 1:
            raise HTTPError(request.full_url, 503, "unavailable", {}, None)
        return BytesIO(json.dumps([trend_row("20261124")]).encode())

    monkeypatch.setattr(exporter, "urlopen", open_url)
    monkeypatch.setattr(exporter, "sleep", lambda _: None)
    assert exporter.fetch_naver_page(date(2026, 11, 25)) == [trend_row("20261124")]
    assert requests == [
        "https://m.stock.naver.com/api/stock/000660/trend?pageSize=60&bizdate=20261125",
    ] * 2

from datetime import UTC, datetime
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts import refresh_cfd_dashboard as daily

CHECK = datetime(2026, 11, 25, 9, tzinfo=UTC)
DOC = SimpleNamespace(
    document_ref="788",
    observation_date="2026-11-24",
    posted_date="2026-11-25",
    source_url="https://www.kofia.or.kr/brd/m_198/view.do?seq=788",
    attachment_url="https://www.kofia.or.kr/brd/m_198/down.do?seq=788",
)


def test_without_reuse_confirmation_only_official_post_metadata_is_exported(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return b"board"

    monkeypatch.setattr(daily, "discover_disclosures", lambda html, limit: [DOC])
    status = daily.refresh(tmp_path, as_of=CHECK, fetch=fetch)
    assert status["status"] == "source_current"
    assert status["data_status"] == "source_permission_unconfirmed"
    assert status["latest"]["observation_date"] == "2026-11-24"
    assert calls == [daily.KOFIA_INDEX_URL]
    assert not (tmp_path / "cfd_market_history.json").exists()


def test_verified_refresh_publishes_aggregates_and_retains_them_on_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "discover_disclosures", lambda html, limit: [DOC])
    rows = [
        SimpleNamespace(market="KOSPI", value_krw=10_000, ticker="000660", quantity=12),
        SimpleNamespace(market="KOSPI", value_krw=50_000, ticker="005930", quantity=55),
        SimpleNamespace(market="KOSDAQ", value_krw=20_000, ticker="099111", quantity=2),
    ]
    monkeypatch.setattr(daily, "parse_pdf", lambda data, observation_date: rows)
    monkeypatch.setattr(daily, "which", lambda name: "/usr/bin/pdftotext")
    kwargs = {"as_of": CHECK, "permit_aggregates": True}
    status = daily.refresh(tmp_path, fetch=lambda url: b"official", **kwargs)
    assert status["data_status"] == "ready"
    p = tmp_path / "cfd_market_history.json"
    successful = p.read_bytes()
    assert (
        b"000660" not in successful and b"005930" not in successful and b"099111" not in successful
    )
    assert b"60000" in successful and b"20000" in successful

    def failed(url):
        if url == daily.KOFIA_INDEX_URL:
            return b"board"
        raise OSError("provider unavailable")

    status = daily.refresh(tmp_path, fetch=failed, **kwargs)
    assert status["data_status"] == "stale"
    assert p.read_bytes() == successful


def test_frees_is_still_refreshes_when_pdf_source_is_unavailable(tmp_path):
    def failed(url):
        raise OSError("board unavailable")

    def free(first, last):
        item = {"date": last, "total_krw": 250_000, "kospi_krw": 50_000, "kosdaq_krw": 20_000}
        return {"with_margin": [item], "without_margin": [dict(item, total_krw=100_000)]}

    status = daily.refresh(
        tmp_path, as_of=CHECK, fetch=failed, permit_aggregates=True, fetch_totals=free
    )
    assert status["status"] == "source_unavailable"
    assert status["frees_is_status"] == "ready"
    assert (tmp_path / "cfd_frees_is_history.json").is_file()
    assert not (tmp_path / "cfd_market_history.json").exists()


def test_frees_is_not_ready_when_one_definition_is_stale(tmp_path):
    def totals(first, last):
        return {
            "with_margin": [{"date": last, "total_krw": 250_000}],
            "without_margin": [{"date": "2026-11-23", "total_krw": 100_000}],
        }

    result = daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"bad board", permit_aggregates=True, fetch_totals=totals)
    assert result["frees_is_status"] == "stale"


def test_ui_shows_stale_frees_is_status():
    script = Path(__file__).resolve().parents[1] / "docs/assets/cfd-balance.js"
    program = r"""
const vm = require('node:vm');
const fs = require('node:fs');
const status = {textContent: ''}, charts = {append() {}};
const info = {schema_version: 'cfd_status_v1', status: 'source_current', data_status: 'ready', frees_is_status: 'stale'};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: id => id === 'cfd-status' ? status : charts},
  fetch: async path => ({ok: path.includes('cfd_status'), json: async () => info}),
});
setImmediate(() => console.log(status.textContent));
"""
    output = subprocess.check_output(["node", "-e", program, str(script)], text=True)
    assert "FreeSIS" in output and "지난 자료" in output


@pytest.mark.parametrize("error", [
    subprocess.TimeoutExpired("pdftotext", 30),
    subprocess.CalledProcessError(1, "pdftotext"),
])
def test_pdf_process_failure_keeps_prior_balance_and_refreshes_frees_is(tmp_path, monkeypatch, error):
    monkeypatch.setattr(daily, "discover_disclosures", lambda html, limit: [DOC])
    monkeypatch.setattr(daily, "which", lambda name: "/usr/bin/pdftotext")
    rows = [SimpleNamespace(market=market, value_krw=100) for market in daily.MARKETS]
    monkeypatch.setattr(daily, "parse_pdf", lambda *_: rows)
    daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"pdf", permit_aggregates=True)
    history = tmp_path / daily.HISTORY
    original = history.read_bytes()

    def failed(*_):
        raise error

    def totals(first, last):
        return {key: [{"date": last, "total_krw": 100}] for key in ("with_margin", "without_margin")}

    monkeypatch.setattr(daily, "parse_pdf", failed)
    result = daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"pdf", permit_aggregates=True, fetch_totals=totals)
    assert result["pdf_failures"] == 1
    assert result["data_status"] == "stale"
    assert result["frees_is_status"] == "ready"
    assert history.read_bytes() == original
    assert (tmp_path / "cfd_frees_is_history.json").exists()

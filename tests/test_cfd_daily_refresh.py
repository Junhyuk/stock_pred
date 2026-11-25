import json
from datetime import UTC, date, datetime
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
    def free(*_):
        pytest.fail("FreeSIS balance fetch requires source approval")

    status = daily.refresh(tmp_path, as_of=CHECK, fetch=fetch, fetch_totals=free)
    assert status["status"] == "source_current"
    assert status["data_status"] == "source_permission_unconfirmed"
    assert status["latest"]["observation_date"] == "2026-11-24"
    assert calls == [daily.KOFIA_INDEX_URL]
    assert not (tmp_path / "cfd_market_history.json").exists()
    assert not (tmp_path / "cfd_frees_is_history.json").exists()


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


@pytest.mark.parametrize("missing", ["none", "all", "side", "session"])
def test_frees_is_backfills_only_if_prior_side_history_is_incomplete(tmp_path, missing):
    def source_rows(first, last):
        _, sessions = daily.exchange_schedule("XKRX", date.fromisoformat(first), date.fromisoformat(last))
        return [{
            "date": day.date().isoformat(), "total_krw": 109, "kospi_krw": 22, "kosdaq_krw": 33,
            "buy_krw": 100, "sell_krw": 9, "kospi_buy_krw": 20, "kospi_sell_krw": 2,
            "kosdaq_buy_krw": 30, "kosdaq_sell_krw": 3,
        } for day in sessions]

    target = tmp_path / "cfd_frees_is_history.json"
    rows = source_rows("2024-11-22", "2026-11-23")
    if missing == "side":
        rows[100].pop("sell_krw")
    elif missing == "session":
        del rows[100]
    if missing != "all":
        target.write_text(json.dumps({
            "schema_version": "cfd_frees_is_history_v1",
            "series": {"with_margin": rows, "without_margin": rows},
        }))

    calls = []

    def free(first, last):
        calls.append((first, last))
        return {key: source_rows(first, last) for key in ("with_margin", "without_margin")}

    assert daily._refresh_frees_is(tmp_path, date(2026, 11, 25), "2026-11-24", free) == "ready"
    [(first, last)] = calls
    assert last == "2026-11-24"
    if missing == "none":
        assert first == "2026-11-04"
    else:
        assert first <= "2024-11-22"
    series = json.loads(target.read_text())["series"]
    assert series["with_margin"][-1]["sell_krw"] == 9
    assert next(row for row in series["with_margin"] if row["date"] == "2024-11-22")["buy_krw"] == 100
    assert len([row for row in series["with_margin"] if row["date"] >= "2024-11-22"]) == 487


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


def test_ui_shows_signed_two_year_side_changes_with_legacy_and_permission_fallback():
    script = Path(__file__).resolve().parents[1] / "docs/assets/cfd-balance.js"
    program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const JS = fs.readFileSync(process.argv[1], 'utf8');
const el = () => ({
  textContent: '', children: [], append(...nodes) { this.children.push(...nodes); },
  replaceChildren(...nodes) { this.children = nodes; }, setAttribute() {},
  addEventListener(name, callback) { this[name] = callback; },
  createTHead() { const heading = el(); this.append(heading); return heading; },
  createTBody() { const body = el(); this.append(body); return body; },
  insertRow() { const row = el(); this.append(row); return row; },
  getContext() { return null; },
});
const all = node => [node, ...node.children.flatMap(all)];
const u = 1e8;
function row(date, buy, sell, kospiBuy) {
  return {date, total_krw: (buy + sell) * u, buy_krw: buy * u, sell_krw: sell * u,
    kospi_krw: kospiBuy * u, kospi_buy_krw: kospiBuy * u, kospi_sell_krw: 0,
    kosdaq_krw: 0, kosdaq_buy_krw: 0, kosdaq_sell_krw: 0};
}
const dates = [];
for (let d = Date.UTC(2024, 10, 22); d < Date.UTC(2026, 10, 24); d += 7 * 86400000) {
  dates.push(new Date(d).toISOString().slice(0, 10));
}
const points = [{date: '2024-11-19', total_krw: 3 * u},
  ...dates.map(date => row(date, 10, 15, 5)), row('2026-11-24', 16, 11, 2)];

async function render(mode) {
  const status = el(), charts = el(), calls = [];
  const info = {schema_version: 'cfd_status_v1', status: 'source_current',
    data_status: 'source_permission_unconfirmed',
    frees_is_status: mode === 'gated' ? 'source_permission_unconfirmed' : 'ready'};
  const legacy = points.map(({date, total_krw}) => ({date, total_krw}));
  const history = {schema_version: 'cfd_frees_is_history_v1',
    series: {with_margin: mode === 'legacy' ? legacy : points, without_margin: legacy}};
  vm.runInNewContext(JS, {
    document: {getElementById: id => id === 'cfd-status' ? status : charts, createElement: el},
    fetch: async path => {
      calls.push(path);
      return {ok: true, json: async () => path.includes('cfd_status') ? info : history};
    },
  });
  await new Promise(resolve => setImmediate(resolve));
  return {charts, calls, body: () => all(charts).map(node => node.textContent).join(' ')};
}

(async () => {
  const actual = await render('sides');
  assert.match(actual.body(), /2년 매수·매도 명목잔고 누적 변화/);
  assert.match(actual.body(), /2024-11-22.*2026-11-24/);
  assert.match(actual.body(), /\+6\.00억 원/);
  assert.match(actual.body(), /-4\.00억 원/);
  const select = all(actual.charts).find(node => node.change);
  assert.ok(select);
  select.value = 'KOSPI';
  select.change();
  assert.match(actual.body(), /-3\.00억 원/);

  const legacy = await render('legacy');
  assert.match(legacy.body(), /FreeSIS 전체 명목잔고/);
  assert.doesNotMatch(legacy.body(), /2년 매수·매도 명목잔고 누적 변화/);

  const gated = await render('gated');
  assert.deepEqual(gated.calls, ['data/cfd_status.json']);
  assert.equal(gated.charts.children.length, 0);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    subprocess.run(["node", "-e", program, str(script)], check=True)


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

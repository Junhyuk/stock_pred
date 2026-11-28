import json
import subprocess
from datetime import UTC, date, datetime
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


def frees_is_row(day):
    return {
        "date": day, "total_krw": 109, "kospi_krw": 22, "kosdaq_krw": 33,
        "buy_krw": 100, "sell_krw": 9, "kospi_buy_krw": 20, "kospi_sell_krw": 2,
        "kosdaq_buy_krw": 30, "kosdaq_sell_krw": 3,
    }


def test_without_reuse_confirmation_only_official_post_metadata_is_exported(tmp_path, monkeypatch):
    calls = []

    def fetch(url):
        calls.append(url)
        return b"board"

    monkeypatch.setattr(daily, "discover_disclosures", lambda html, limit: [DOC])
    def free(*_):
        pytest.fail("FreeSIS balance fetch requires source approval")

    status = daily.refresh(tmp_path, as_of=CHECK, fetch=fetch, fetch_total=free)
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
        return {definition: [frees_is_row(last)] for definition in daily.FREESIS_DEFINITIONS}

    status = daily.refresh(
        tmp_path, as_of=CHECK, fetch=failed, permit_aggregates=True, fetch_totals=free
    )
    assert status["status"] == "source_unavailable"
    assert status["frees_is_status"] == "ready"
    assert (tmp_path / "cfd_frees_is_history.json").is_file()
    assert not (tmp_path / "cfd_market_history.json").exists()


def test_bad_prior_document_url_does_not_interrupt_frees_is(tmp_path):
    row = {
        "date": "2026-11-24", "posted_date": "2026-11-25", "received_at": CHECK.isoformat(),
        "source_url": "https://[", "document_sha256": "0" * 64,
        "value_krw": 20_000, "valid_count": 2,
    }
    (tmp_path / daily.HISTORY).write_text(json.dumps({
        "schema_version": "cfd_market_history_v1", "unit": "KRW",
        "markets": {market: [row] for market in daily.MARKETS},
    }))
    result = daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"bad board",
                           permit_aggregates=True, fetch_total=lambda *args: [frees_is_row(args[-1])])
    assert result["data_status"] == "unavailable"
    assert result["frees_is_status"] == "ready"


def test_frees_is_not_ready_when_one_definition_is_stale(tmp_path):
    def totals(definition, first, last):
        return [frees_is_row(last if definition == "with_margin" else "2026-11-23")]

    result = daily.refresh(
        tmp_path, as_of=CHECK, fetch=lambda _: b"bad board", permit_aggregates=True, fetch_total=totals
    )
    assert result["frees_is_status"] == "stale"
    assert result["frees_is_series_status"] == {"with_margin": "ready", "without_margin": "stale"}
    assert result["frees_is_latest_observation_dates"] == {
        "with_margin": "2026-11-24", "without_margin": "2026-11-23"
    }


@pytest.mark.parametrize("bad_rows", [
    None,
    [frees_is_row("2026-11-24"), frees_is_row("2026-11-24")],
    [frees_is_row("2026-11-24"), frees_is_row("2026-11-23")],
    [frees_is_row("2026-11-25")],
    [dict(frees_is_row("2026-11-24"), sell_krw=8)],
    [dict(frees_is_row("2026-11-24"), sell_krw=-1)],
    [dict(frees_is_row("2026-11-24"), sell_krw=float("inf"))],
    [dict(frees_is_row("2026-11-24"), buy_krw=2**53, total_krw=2**53 + 9)],
])
def test_bad_frees_is_definition_keeps_its_own_last_good_row(tmp_path, bad_rows):
    kwargs = {"as_of": CHECK, "fetch": lambda _: b"bad board", "permit_aggregates": True}
    daily.refresh(tmp_path, fetch_total=lambda *_: [frees_is_row("2026-11-21")], **kwargs)

    def totals(definition, first, last):
        if definition == "with_margin":
            if bad_rows is None:
                raise OSError("FreeSIS margin query unavailable")
            return bad_rows
        return [dict(frees_is_row(last), ticker="PRIVATE")]

    result = daily.refresh(tmp_path, fetch_total=totals, **kwargs)
    assert result["frees_is_series_status"] == {"with_margin": "stale", "without_margin": "ready"}
    target = tmp_path / daily.FREESIS_HISTORY
    assert "PRIVATE" not in target.read_text()
    series = json.loads(target.read_text())["series"]
    assert series["with_margin"] == [frees_is_row("2026-11-21")]
    assert series["without_margin"][-1] == frees_is_row("2026-11-24")


def test_bad_domestic_market_keeps_the_previous_document_for_both_markets(tmp_path, monkeypatch):
    monkeypatch.setattr(daily, "discover_disclosures", lambda html, limit: [DOC])
    monkeypatch.setattr(daily, "which", lambda _: "/usr/bin/pdftotext")
    valid = [
        SimpleNamespace(market="KOSPI", value_krw=50_000),
        SimpleNamespace(market="KOSDAQ", value_krw=20_000),
    ]
    monkeypatch.setattr(daily, "parse_pdf", lambda *_: valid)
    kwargs = {"as_of": CHECK, "permit_aggregates": True}
    daily.refresh(tmp_path, fetch=lambda _: b"first", **kwargs)
    history = tmp_path / daily.HISTORY
    original = history.read_bytes()

    # The source PDF expresses amounts in ten-thousands of KRW.
    valid[1].value_krw = 20_001
    result = daily.refresh(tmp_path, fetch=lambda _: b"revision", **kwargs)
    assert result["pdf_failures"] == 1
    assert result["data_status"] == "stale"
    assert result["domestic_latest_observation_date"] == "2026-11-24"
    assert history.read_bytes() == original

    monkeypatch.setattr(daily, "which", lambda _: None)
    result = daily.refresh(tmp_path, fetch=lambda _: b"second revision", **kwargs)
    assert result["data_status"] == "stale"
    assert history.read_bytes() == original


@pytest.mark.parametrize("missing", ["none", "all", "side", "session"])
def test_frees_is_backfills_only_if_prior_side_history_is_incomplete(tmp_path, missing):
    def source_rows(first, last):
        _, sessions = daily.exchange_schedule("XKRX", date.fromisoformat(first), date.fromisoformat(last))
        return [frees_is_row(day.date().isoformat()) for day in sessions]

    target = tmp_path / "cfd_frees_is_history.json"
    rows = source_rows("2024-11-22", "2026-11-23")
    if missing == "side":
        rows[100].pop("sell_krw")
    elif missing == "session":
        del rows[100]
    if missing != "all":
        target.write_text(json.dumps({
            "schema_version": "cfd_frees_is_history_v1",
            "unit": "KRW",
            "series": {"with_margin": rows, "without_margin": rows},
        }))

    calls = []

    def free(definition, first, last):
        calls.append((first, last))
        return source_rows(first, last)

    result = daily.refresh(
        tmp_path, as_of=CHECK, fetch=lambda _: b"bad board", permit_aggregates=True, fetch_total=free
    )
    assert result["frees_is_status"] == "ready"
    (first, last), second = calls
    assert (first, last) == second
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
  document: {getElementById: id => id === 'cfd-status' ? status : charts, createElement: () => ({})},
  fetch: async path => ({ok: path.includes('cfd_status'), json: async () => info}),
});
setImmediate(() => console.log(status.textContent));
"""
    output = subprocess.check_output(["node", "-e", program, str(script)], text=True)
    assert "FreeSIS" in output and "지난 자료" in output


def test_ui_shows_signed_two_year_side_changes_with_legacy_and_outage_fallback():
    script = Path(__file__).resolve().parents[1] / "docs/assets/cfd-balance.js"
    program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const JS = fs.readFileSync(process.argv[1], 'utf8');
const el = () => ({
  textContent: '', children: [], append(...nodes) { this.children.push(...nodes); },
  replaceChildren(...nodes) { this.children = nodes; }, setAttribute() {},
  addEventListener(name, callback) { const previous = this[name]; this[name] = () => { if (previous) previous(); callback(); }; },
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
  const select = all(actual.charts).filter(node => node.change).at(-1);
  assert.ok(select);
  select.value = 'KOSPI';
  select.change();
  assert.match(actual.body(), /-3\.00억 원/);

  const legacy = await render('legacy');
  assert.match(legacy.body(), /FreeSIS 전체 명목잔고/);
  assert.doesNotMatch(legacy.body(), /2년 매수·매도 명목잔고 누적 변화/);

  const gated = await render('gated');
  assert.ok(gated.calls.includes('data/cfd_frees_is_history.json'));
  assert.match(gated.body(), /2년 매수·매도 명목잔고 누적 변화/);
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
    rows = [SimpleNamespace(market=market, value_krw=100_000) for market in daily.MARKETS]
    monkeypatch.setattr(daily, "parse_pdf", lambda *_: rows)
    daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"pdf", permit_aggregates=True)
    history = tmp_path / daily.HISTORY
    original = history.read_bytes()

    def failed(*_):
        raise error

    def totals(definition, first, last):
        return [frees_is_row(last)]

    monkeypatch.setattr(daily, "parse_pdf", failed)
    result = daily.refresh(tmp_path, as_of=CHECK, fetch=lambda _: b"pdf", permit_aggregates=True, fetch_total=totals)
    assert result["pdf_failures"] == 1
    assert result["data_status"] == "stale"
    assert result["frees_is_status"] == "ready"
    assert history.read_bytes() == original
    assert (tmp_path / "cfd_frees_is_history.json").exists()


def test_ui_loads_valid_history_even_when_status_lookup_fails():
    script = Path(__file__).resolve().parents[1] / "docs/assets/cfd-balance.js"
    program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const el = () => ({textContent: '', children: [], append(...x) {this.children.push(...x)},
  replaceChildren(...x) {this.children = x}, setAttribute() {}, addEventListener() {},
  createTHead() {const e = el(); this.append(e); return e},
  createTBody() {const e = el(); this.append(e); return e},
  insertRow() {const e = el(); this.append(e); return e}, getContext() {return null}});
const status = el(), charts = el();
const row = {date: '2026-11-24', total_krw: 268359918, buy_krw: 200000000, sell_krw: 68359918,
  kospi_krw: 200000000, kospi_buy_krw: 200000000, kospi_sell_krw: 0,
  kosdaq_krw: 0, kosdaq_buy_krw: 0, kosdaq_sell_krw: 0};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: id => id === 'cfd-status' ? status : charts, createElement: el},
  fetch: async path => {
    if (path.includes('status') || path.includes('market_history')) throw Error('unavailable');
    return {ok: true, json: async () => ({schema_version: 'cfd_frees_is_history_v1', series: {with_margin: [row, {...row, date: '2026-11-25', sell_krw: -1}]}})};
  },
});
const flatten = n => [n, ...n.children.flatMap(flatten)];
setImmediate(() => {
  const body = flatten(charts).map(n => n.textContent).join(' ');
  assert.match(body, /2026-11-24/);
  assert.match(body, /매수/);
  assert.match(body, /매도/);
  assert.match(body, /1개 관측/);
  assert.match(body, /0\.68359918억 원/);
  assert.doesNotMatch(body, /2026-11-25/);
  assert.doesNotMatch(body, /2년 매수·매도 명목잔고 누적 변화/);
});
"""
    subprocess.run(["node", "-e", program, str(script)], check=True)


def test_ui_explains_when_no_valid_balance_history_exists():
    script = Path(__file__).resolve().parents[1] / "docs/assets/cfd-balance.js"
    program = r"""
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const status = {textContent: ''}, charts = {children: [], append(x) {this.children.push(x)}};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: {getElementById: id => id === 'cfd-status' ? status : charts,
    createElement: () => ({textContent: '', className: ''})},
  fetch: async () => ({ok: false}),
});
setImmediate(() => assert.equal(charts.children[0]?.textContent, '확인된 잔고 자료가 없습니다.'));
"""
    subprocess.run(["node", "-e", program, str(script)], check=True)

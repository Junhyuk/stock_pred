import importlib.util
import json
import re
import sqlite3
import subprocess
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "collect_kospi_flow", ROOT / "scripts" / "collect_kospi_investor_flow_chart.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
SCRIPT = ROOT / "docs/assets/kospi-flow.js"
NODE_PROGRAM = r'''
const vm = require('vm');
const fs = require('fs');
const payload = JSON.parse(process.argv[2]);
const nodes = {'kospi-flow-status': {textContent: ''}, 'kospi-flow-content': {innerHTML: ''}};
const context = {document: {getElementById: id => nodes[id]}, fetch: async () => ({ok: true, json: async () => payload}),
  Intl, Date, Number, Math, Array, Object, String, Promise, console};
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
setTimeout(() => console.log(JSON.stringify(nodes)), 20);
'''


def render(payload):
    result = subprocess.run(["node", "-e", NODE_PROGRAM, str(SCRIPT), json.dumps(payload)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def chart_payload(kospi_rows, kosdaq_rows=None):
    return {
        "schema_version": "kospi_investor_flow_chart_v1",
        "unit": "억원",
        "production_weight": 0,
        "average_sessions": 20,
        "snapshot_received_at": "2026-11-23T12:00:00+00:00",
        "markets": {
            "KOSPI": {"sessions": kospi_rows},
            "KOSDAQ": {"sessions": kosdaq_rows if kosdaq_rows is not None else kospi_rows},
        },
    }


def dated_rows(count, start="2024-01-01"):
    first = date.fromisoformat(start)
    return [{"date": (first + timedelta(days=index)).isoformat(), "foreign_eok": 1.0,
             "institution_eok": -1.0, "personal_eok": 0.5} for index in range(count)]


def test_investor_groups_match_naver_net_buying_and_exclude_other_corporations():
    row = MODULE.parse_day({
        "bizdate": "20261123",
        "netAmounts": [
            {"investorGubun": "9000", "diffValue": "-246055000000"},
            {"investorGubun": "9001", "diffValue": "-9969000000"},
            {"investorGubun": "8000", "diffValue": "-353236000000"},
            {"investorGubun": "1000", "diffValue": "100000000000"},
            {"investorGubun": "7100", "diffValue": "500000000000"},
        ],
    })
    assert row["date"] == "2026-11-23"
    assert row["foreign_eok"] == -2560.2
    assert row["personal_eok"] == -3532.4
    assert row["institution_eok"] == 1000.0


def test_kosdaq_uses_the_same_trend_endpoint():
    kosdaq = MODULE.trend_url("KOSDAQ", "20261123", 0, 1)
    kospi = MODULE.trend_url("KOSPI", "20261123", 0, 1)
    assert kosdaq == (
        "https://stock.naver.com/api/domestic/market/trend/daily"
        "?tradeType=KRX&marketType=KOSDAQ&bizdate=20261123&startIdx=0&pageSize=1"
    )
    assert kospi.replace("marketType=KOSPI", "marketType=KOSDAQ") == kosdaq
    assert not hasattr(MODULE, "WINDOW_DAYS")


def test_window_is_one_year_and_one_month_by_calendar():
    assert MODULE.window_start("2026-11-23") == "2025-10-23"
    assert MODULE.window_start("2025-03-31") == "2024-02-29"
    assert MODULE.window_start("2024-03-31") == "2023-02-28"
    today = "2026-11-23"
    rows = [
        {"date": "2025-10-22", "foreign_eok": 1, "institution_eok": 1, "personal_eok": 1},
        {"date": "2025-10-23", "foreign_eok": 4, "institution_eok": 4, "personal_eok": 4},
        {"date": "2025-11-23", "foreign_eok": 2, "institution_eok": 2, "personal_eok": 2},
        {"date": "2026-11-23", "foreign_eok": 3, "institution_eok": 3, "personal_eok": 3},
        {"date": "2026-11-23", "foreign_eok": 9, "institution_eok": 9, "personal_eok": 9},
        {"date": "2026-11-24", "foreign_eok": 8, "institution_eok": 8, "personal_eok": 8},
    ]
    selected = MODULE.select_window(rows, today)
    assert [row["date"] for row in selected] == ["2025-10-23", "2025-11-23", "2026-11-23"]
    assert selected[-1]["foreign_eok"] == 9


def test_collect_rebuilds_the_window_from_stored_rows(tmp_path):
    database = tmp_path / "index_forecast.sqlite"
    conn = sqlite3.connect(database)
    conn.execute(
        """
        CREATE TABLE investor_flow (
            market TEXT NOT NULL,
            session_date TEXT NOT NULL,
            investor_type TEXT NOT NULL,
            net_value REAL,
            source TEXT NOT NULL,
            ingested_at TEXT NOT NULL
        )
        """
    )
    amounts = {"기관": 175844000000, "개인": -353236000000, "외국인": -256024000000}
    start = date(2025, 10, 23)
    for market in ("KOSPI", "KOSDAQ"):
        for offset, day in enumerate([start - timedelta(days=1), *[start + timedelta(days=index) for index in range(20)], date(2026, 11, 24)]):
            sign = -1 if market == "KOSDAQ" else 1
            for investor, won in amounts.items():
                conn.execute(
                    "INSERT INTO investor_flow VALUES (?, ?, ?, ?, ?, ?)",
                    (market, day.isoformat(), investor, sign * won * (1 if offset else 1), "stored", "2026-11-23T00:00:00+00:00"),
                )
    conn.commit()
    conn.close()
    payload = MODULE.collect(received_at="2026-11-23T00:00:00+00:00", today="2026-11-23", db_path=database)
    assert payload["window_start"] == "2025-10-23"
    assert payload["window_label"] == "1년 1개월"
    assert payload["average_sessions"] == 20
    assert payload["production_weight"] == 0.0
    kospi = payload["markets"]["KOSPI"]["sessions"]
    kosdaq = payload["markets"]["KOSDAQ"]["sessions"]
    assert [row["date"] for row in kospi] == [(start + timedelta(days=index)).isoformat() for index in range(20)]
    assert kospi[0]["institution_eok"] == 1758.4
    assert kospi[0]["personal_eok"] == -3532.4
    assert kospi[0]["foreign_eok"] == -2560.2
    assert kosdaq[0]["institution_eok"] == -1758.4
    assert len(kosdaq) == 20


def test_flow_chart_draws_cumulative_lines_for_both_markets():
    rows = [{"date": f"2026-01-{(index % 28) + 1:02d}", "foreign_eok": float(index - 10),
             "institution_eok": float(5 - index), "personal_eok": float(index)} for index in range(20)]
    rows.sort(key=lambda row: row["date"])
    kosdaq = [{**row, "foreign_eok": -row["foreign_eok"], "institution_eok": -row["institution_eok"],
               "personal_eok": -row["personal_eok"]} for row in rows]
    nodes = render(chart_payload(rows, kosdaq))
    html = nodes["kospi-flow-content"]["innerHTML"]
    assert html.count("<path") == 12
    assert html.count("flow-zero") == 6
    assert html.count('data-market="KOSPI"') == 1
    assert html.count('data-market="KOSDAQ"') == 1
    assert html.count('data-investor="institution"') == 2
    assert html.count('data-investor="personal"') == 2
    assert html.count('data-investor="foreign"') == 2
    assert html.count("flow-marker") == 120
    assert '<div class="credit-grid">' in html
    assert html.count('<span class="flow-institution">기관순매수누적</span>') == 2
    assert html.count('<span class="flow-personal">개인순매수누적</span>') == 2
    assert html.count('<span class="flow-foreign">외국인순매수누적</span>') == 2
    assert html.count('<span class="flow-ma">4주 이평</span>') == 6
    assert ">26-01</text>" in html
    assert "누적 순매수 (억원)" in html
    assert "누적 / " in html and "오늘 / " in html and "전일 / " in html and "조원" in html
    assert "외국인 +9억" not in html
    assert "<tr><td>2026-01-20</td><td>-90억</td><td>+190억</td><td>-10억</td>" in html
    assert "<tr><td>2026-01-01</td><td>+5억</td><td>0억</td><td>-10억</td>" in html
    assert "2026-11-23" in nodes["kospi-flow-status"]["textContent"]
    page = (ROOT / "docs/reliable-index-forecast.html").read_text()
    assert '<h2 id="kospi-flow-title">코스피·코스닥 투자자 누적 순매수 · 1년 1개월</h2>' in page
    assert "2년" not in page.split('id="kospi-flow"', 1)[1].split("</section>", 1)[0]
    assert '<a href="#kospi-flow">누적 순매수</a>' in page
    assert 'id="kospi-flow"' in page and "assets/kospi-flow.js" in page
    assert "https://stock.naver.com/market/stock/kr/trend/trader" in page
    workflow = (ROOT / ".github/workflows/premarket-research-forecast.yml").read_text()
    assert "collect_kospi_investor_flow_chart.py" in workflow
    assert "run_series_token_accuracy.py" not in workflow


def test_flow_chart_accepts_two_year_rows_and_rejects_short_or_oversized():
    long_nodes = render(chart_payload(dated_rows(500)))
    assert long_nodes["kospi-flow-content"]["innerHTML"].count("<path") == 12
    assert "자료 수신" in long_nodes["kospi-flow-status"]["textContent"]
    for count in (19, 641):
        nodes = render(chart_payload(dated_rows(count)))
        assert nodes["kospi-flow-content"]["innerHTML"] == ""
        assert "불러오지 못했습니다" in nodes["kospi-flow-status"]["textContent"]


def test_four_week_average_is_20_stored_sessions_of_the_cumulative_line():
    assert MODULE.AVERAGE_SESSIONS == 20
    cumulative = [float(index + 1) for index in range(20)]
    averaged = MODULE.trailing_average(cumulative)
    assert averaged[:19] == [None] * 19
    assert averaged[19] == sum(cumulative) / 20
    assert not hasattr(MODULE, "WINDOW_DAYS")


def test_latest_box_uses_signed_trillion_won_and_20_session_average():
    rows = []
    for index in range(20):
        foreign = 0.0
        if index == 17:
            foreign = -10000.0
        elif index == 18:
            foreign = -11000.0
        elif index == 19:
            foreign = -2000.0
        rows.append({
            "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
            "foreign_eok": foreign,
            "institution_eok": 10000.0,
            "personal_eok": -5000.0,
        })
    html = render(chart_payload(rows))["kospi-flow-content"]["innerHTML"]
    assert html.count("누적 / -2.3조원") == 2
    assert html.count("오늘 / -0.2조원") == 2
    assert html.count("전일 / -1.1조원") == 2
    assert "전일 / -2.1조원" not in html
    assert html.count("누적 / +20조원") == 2
    assert html.count("오늘 / +1조원") == 2
    assert html.count("전일 / +1조원") == 2
    assert html.count("누적 / -10조원") == 2
    assert html.count("오늘 / -0.5조원") == 2
    assert html.count("전일 / -0.5조원") == 2
    assert html.count('data-average-sessions="20"') == 6
    assert html.count('data-ma-eok="-2700"') == 2
    assert html.count('data-ma-eok="105000"') == 2
    assert html.count('data-ma-eok="-52500"') == 2
    short = re.findall(r'class="credit-line flow-ma" d="([^"]+)"', html)
    assert len(short) == 6
    assert all("L" not in path for path in short)
    longer = render(chart_payload(dated_rows(40)))["kospi-flow-content"]["innerHTML"]
    long_paths = re.findall(r'class="credit-line flow-ma" d="([^"]+)"', longer)
    assert len(long_paths) == 6
    assert all(path.count("L") == 20 for path in long_paths)


def test_investor_colors_differ_and_the_average_line_is_thinner():
    css = (ROOT / "docs/assets/kospi-flow.css").read_text()
    colors = {}
    for name in ("institution", "personal", "foreign"):
        match = re.search(rf"\.flow-{name} \{{ color: (#[0-9a-fA-F]{{6}});", css)
        assert match, name
        colors[name] = match.group(1).lower()
    assert len(set(colors.values())) == 3
    ma = re.search(r"path\.flow-ma \{ stroke: (#[0-9a-fA-F]{6}); stroke-width: 1\.15; \}", css)
    assert ma
    assert ma.group(1).lower() not in colors.values()
    assert "stroke-width: 2.2" in css
    assert "stroke-width: 1.15" in css


def test_small_nonzero_jo_keeps_its_sign():
    rows = [{
        "date": (date(2026, 1, 1) + timedelta(days=index)).isoformat(),
        "foreign_eok": -375.7 if index == 19 else 0.0,
        "institution_eok": 0.0,
        "personal_eok": 0.0,
    } for index in range(20)]
    html = render(chart_payload(rows))["kospi-flow-content"]["innerHTML"]
    assert "오늘 / -0.04조원" in html
    assert "누적 / -0.04조원" in html
    assert "전일 / 0조원" in html


def test_chart_rejects_a_missing_average_window():
    payload = chart_payload(dated_rows(20))
    del payload["average_sessions"]
    nodes = render(payload)
    assert nodes["kospi-flow-content"]["innerHTML"] == ""
    assert "불러오지 못했습니다" in nodes["kospi-flow-status"]["textContent"]


# --- runner path: committed JSON + Naver, merged, validated, written only when valid ---

FINAL_NOW = "2026-11-24T22:00:00+09:00"      # after the 20:00 KST after-market close
EARLY_NOW = "2026-11-24T18:10:00+09:00"      # the scheduled 18:00 forecast run
WORKFLOW = ROOT / ".github/workflows/premarket-research-forecast.yml"


def weekdays(start, end):
    day, days = date.fromisoformat(start), []
    while day <= date.fromisoformat(end):
        if day.weekday() < 5:
            days.append(day.isoformat())
        day += timedelta(days=1)
    return days


def won(day, offset=0):
    n = date.fromisoformat(day).toordinal() % 17 - 8 + offset
    return {"foreign": n * 123_400_000_000.0, "personal": -n * 77_700_000_000.0, "institution": n * 5_000_000_000.0}


def naver_item(day, offset=0):
    amount = won(day, offset)
    # foreign = 9000 + 9001, institution = 1000 + 6000; 7100 (other corporations) is outside the chart.
    parts = [("9000", amount["foreign"] - 1_000_000_000), ("9001", 1_000_000_000.0), ("8000", amount["personal"]),
             ("1000", amount["institution"] - 2_000_000_000), ("6000", 2_000_000_000.0), ("7100", 9_999_000_000_000.0)]
    return {"bizdate": day.replace("-", ""), "netAmounts": [
        {"investorGubun": code, "diffValue": str(int(value))} for code, value in parts]}


class FakeNaver:
    """Serves the daily trend endpoint: newest first, startIdx is the page number."""

    def __init__(self, days, offsets=None, broken=()):
        self.days, self.offsets, self.broken, self.calls = list(days), offsets or {}, set(broken), []

    def __call__(self, url):
        query = parse_qs(urlparse(url).query)
        self.calls.append(url)
        assert query["tradeType"] == ["KRX"]
        market, size, page = query["marketType"][0], int(query["pageSize"][0]), int(query["startIdx"][0])
        assert size <= 200
        newest = sorted((d for d in self.days if d.replace("-", "") <= query["bizdate"][0]), reverse=True)
        content = []
        for day in newest[page * size:(page + 1) * size]:
            item = naver_item(day, self.offsets.get((market, day), 0))
            if day in self.broken:
                item["netAmounts"] = [row for row in item["netAmounts"] if row["investorGubun"] != "9000"]
            content.append(item)
        return json.dumps({"content": content}).encode()


def seed_payload(last="2026-11-23", first=None, provisional=(), offsets=None):
    first = first or MODULE.window_start(last)
    rows = {market: [MODULE.to_eok_row({"date": day, **won(day, (offsets or {}).get((market, day), 0))})
                     for day in weekdays(first, last)] for market in MODULE.MARKETS}
    return MODULE.build_payload(rows, start=MODULE.window_start(last), received="2026-11-23T12:00:00+00:00",
                                source=MODULE.SOURCE_TAG, provisional=list(provisional), revisions=[])


@pytest.fixture
def weekday_calendar(monkeypatch):
    monkeypatch.setattr(MODULE, "xkrx_sessions", weekdays)
    return weekdays


def run_cli(tmp_path, naver, seed=None, now=FINAL_NOW, name="out.json"):
    seed_path = tmp_path / "seed.json"
    if seed is not None:
        seed_path.write_text(json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    output = tmp_path / name
    code = MODULE.main(["--seed", str(seed_path), "--output", str(output), "--now", now, "--delay", "0"], get=naver)
    return code, output


def sessions_of(path, market="KOSPI"):
    return json.loads(Path(path).read_text())["markets"][market]["sessions"]


def test_runner_seeds_from_committed_json_and_adds_the_new_session(tmp_path, weekday_calendar, capsys):
    naver = FakeNaver(weekdays("2025-01-01", "2026-11-24"))
    code, output = run_cli(tmp_path, naver, seed_payload())
    assert code == 0
    payload = json.loads(output.read_text())
    kospi = payload["markets"]["KOSPI"]["sessions"]
    assert kospi[0]["date"] == "2025-10-24" and kospi[-1]["date"] == "2026-11-24"
    assert payload["window_start"] == "2025-10-24" and payload["production_weight"] == 0
    assert payload["source"] == "naver_investor_trend_unofficial" and payload["provisional_dates"] == []
    # 9000 + 9001 are one foreign group, institution is 1000 + 6000, 7100 is left out.
    assert kospi[-1] == MODULE.to_eok_row({"date": "2026-11-24", **won("2026-11-24")})
    assert payload["snapshot_received_at"] == "2026-11-24T13:00:00+00:00"
    assert MODULE.validate_payload(payload, weekdays) == []
    # Only the newest page is asked for: the last session plus the 5 re-fetched ones.
    assert len(naver.calls) == 2 and "pageSize=6" in naver.calls[0] and "startIdx=0" in naver.calls[0]
    assert json.loads(capsys.readouterr().out)["changed"] is True


def test_second_run_is_idempotent_and_leaves_the_file_byte_identical(tmp_path, weekday_calendar):
    naver = FakeNaver(weekdays("2025-01-01", "2026-11-24"))
    code, output = run_cli(tmp_path, naver, seed_payload())
    assert code == 0
    first = output.read_bytes()
    seed = json.loads(first)
    code, again = run_cli(tmp_path, naver, seed, name="again.json")
    assert code == 0
    assert again.read_bytes() == first
    # Same path as the seed: unchanged means the file is not rewritten at all.
    stamp = output.stat().st_mtime_ns
    assert MODULE.main(["--seed", str(output), "--output", str(output), "--now", FINAL_NOW, "--delay", "0"], get=naver) == 0
    assert output.read_bytes() == first and output.stat().st_mtime_ns == stamp


def test_bootstrap_without_a_seed_fetches_the_whole_window_in_pages(tmp_path, weekday_calendar):
    naver = FakeNaver(weekdays("2024-01-01", "2026-11-24"))
    code, output = run_cli(tmp_path, naver, None)
    assert code == 0
    rows = sessions_of(output)
    assert rows[0]["date"] == "2025-10-24" and rows[-1]["date"] == "2026-11-24"
    assert [row["date"] for row in rows] == weekdays("2025-10-24", "2026-11-24")
    # 397 calendar days -> two pages of at most 200 per market; startIdx is the page number.
    assert len(naver.calls) == 4
    assert [("startIdx=0" in url, "startIdx=1" in url) for url in naver.calls[:2]] == [(True, False), (False, True)]


def test_stored_value_that_naver_later_changes_is_replaced_and_recorded(tmp_path, weekday_calendar):
    changed_day = "2026-11-20"
    naver = FakeNaver(weekdays("2025-01-01", "2026-11-24"), offsets={("KOSPI", changed_day): 3})
    code, output = run_cli(tmp_path, naver, seed_payload())
    assert code == 0
    payload = json.loads(output.read_text())
    row = next(item for item in payload["markets"]["KOSPI"]["sessions"] if item["date"] == changed_day)
    assert row == MODULE.to_eok_row({"date": changed_day, **won(changed_day, 3)})
    [revision] = payload["revisions"]
    assert revision["market"] == "KOSPI" and revision["date"] == changed_day
    assert revision["previous"]["foreign_eok"] == MODULE.to_eok_row({"date": changed_day, **won(changed_day)})["foreign_eok"]
    assert revision["new"]["foreign_eok"] == row["foreign_eok"]
    kosdaq = next(item for item in payload["markets"]["KOSDAQ"]["sessions"] if item["date"] == changed_day)
    assert kosdaq == MODULE.to_eok_row({"date": changed_day, **won(changed_day)})


def test_bulk_rewrite_of_stored_history_is_refused_and_the_file_is_untouched(tmp_path, weekday_calendar, capsys):
    offsets = {("KOSPI", day): 5 for day in ("2026-11-17", "2026-11-18", "2026-11-19", "2026-11-20")}
    seed = seed_payload()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    before = seed_path.read_bytes()
    code = MODULE.main(["--seed", str(seed_path), "--output", str(seed_path), "--now", FINAL_NOW, "--delay", "0"],
                       get=FakeNaver(weekdays("2025-01-01", "2026-11-24"), offsets=offsets))
    assert code == 1 and seed_path.read_bytes() == before
    assert "unchanged" in capsys.readouterr().err


def test_latest_session_before_2030_kst_is_provisional_and_corrected_next_run(tmp_path, weekday_calendar):
    early = FakeNaver(weekdays("2025-01-01", "2026-11-24"), offsets={("KOSPI", "2026-11-24"): 1})
    code, output = run_cli(tmp_path, early, seed_payload(), now=EARLY_NOW)
    assert code == 0
    payload = json.loads(output.read_text())
    assert payload["provisional_dates"] == ["2026-11-24"] and payload["revisions"] == []
    assert payload["snapshot_received_at"] == "2026-11-24T09:10:00+00:00"
    # Finalised later the same evening: the provisional value is replaced without a revision record.
    final = FakeNaver(weekdays("2025-01-01", "2026-11-24"))
    code, corrected = run_cli(tmp_path, final, payload, now=FINAL_NOW, name="final.json")
    assert code == 0
    fixed = json.loads(corrected.read_text())
    assert fixed["provisional_dates"] == [] and fixed["revisions"] == []
    assert sessions_of(corrected)[-1] == MODULE.to_eok_row({"date": "2026-11-24", **won("2026-11-24")})
    assert sessions_of(corrected)[-1] != payload["markets"]["KOSPI"]["sessions"][-1]


def test_provisional_session_stays_provisional_when_rechecked_before_the_cutoff(tmp_path, weekday_calendar):
    naver = FakeNaver(weekdays("2025-01-01", "2026-11-24"))
    code, output = run_cli(tmp_path, naver, seed_payload(), now=EARLY_NOW)
    assert code == 0
    first = output.read_bytes()
    code, again = run_cli(tmp_path, naver, json.loads(first), now="2026-11-24T19:00:00+09:00", name="again.json")
    assert code == 0 and json.loads(again.read_text())["provisional_dates"] == ["2026-11-24"]
    assert again.read_bytes() == first


def test_missing_naver_session_is_an_error_not_a_zero(tmp_path, weekday_calendar, capsys):
    days = [day for day in weekdays("2025-01-01", "2026-11-24") if day != "2026-11-23"]
    naver = FakeNaver(days)
    seed = seed_payload(last="2026-11-20")
    code, output = run_cli(tmp_path, naver, seed)
    assert code == 1 and not output.exists()
    assert "missing XKRX sessions" in capsys.readouterr().err
    # A day without the required investor codes is skipped, never turned into zeros:
    # in the middle of the window it is a gap and blocks the write ...
    middle = FakeNaver(weekdays("2025-01-01", "2026-11-24"), broken={"2026-11-23"})
    code, output = run_cli(tmp_path, middle, seed_payload(last="2026-11-20"), name="middle.json")
    assert code == 1 and not output.exists()
    # ... and as the newest day it is simply not added yet.
    newest = FakeNaver(weekdays("2025-01-01", "2026-11-24"), broken={"2026-11-24"})
    code, output = run_cli(tmp_path, newest, seed_payload(), name="newest.json")
    assert code == 0 and sessions_of(output)[-1]["date"] == "2026-11-23"
    assert json.loads(output.read_text())["window_start"] == "2025-10-23"


def test_network_failure_leaves_the_previous_file_untouched(tmp_path, weekday_calendar, capsys):
    seed = seed_payload()
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed), encoding="utf-8")
    before = seed_path.read_bytes()

    def offline(url):
        raise RuntimeError("Naver request failed after 3 attempts")

    assert MODULE.main(["--seed", str(seed_path), "--output", str(seed_path), "--now", FINAL_NOW, "--delay", "0"], get=offline) == 1
    assert seed_path.read_bytes() == before and not list(tmp_path.glob("*.tmp"))
    assert "Naver request failed" in capsys.readouterr().err

    def garbage(url):
        return b'{"unexpected": true}'

    assert MODULE.main(["--seed", str(seed_path), "--output", str(seed_path), "--now", FINAL_NOW, "--delay", "0"], get=garbage) == 1
    assert seed_path.read_bytes() == before


def test_guard_blocks_dropped_rows_stale_newest_day_and_unrecorded_edits():
    previous = seed_payload()
    ok = json.loads(json.dumps(previous))
    assert MODULE.guard_against_previous(previous, ok) == []
    dropped = json.loads(json.dumps(previous))
    del dropped["markets"]["KOSPI"]["sessions"][100]
    assert any("was dropped" in error for error in MODULE.guard_against_previous(previous, dropped))
    stale = json.loads(json.dumps(previous))
    stale["markets"]["KOSDAQ"]["sessions"] = stale["markets"]["KOSDAQ"]["sessions"][:-1]
    assert any("moved backwards" in error for error in MODULE.guard_against_previous(previous, stale))
    edited = json.loads(json.dumps(previous))
    edited["markets"]["KOSPI"]["sessions"][50]["foreign_eok"] += 10
    assert any("without a revision record" in error for error in MODULE.guard_against_previous(previous, edited))
    rolled = json.loads(json.dumps(previous))
    rolled["window_start"] = "2025-10-27"
    rolled["markets"]["KOSPI"]["sessions"] = [r for r in rolled["markets"]["KOSPI"]["sessions"] if r["date"] >= "2025-10-27"]
    rolled["markets"]["KOSDAQ"]["sessions"] = [r for r in rolled["markets"]["KOSDAQ"]["sessions"] if r["date"] >= "2025-10-27"]
    assert MODULE.guard_against_previous(previous, rolled) == []


def test_validation_rejects_duplicates_order_nan_and_short_history():
    payload = seed_payload()
    assert MODULE.validate_payload(payload, weekdays) == []
    for mutate, text in (
        (lambda rows: rows.insert(5, dict(rows[5])), "duplicated or out of order"),
        (lambda rows: rows.reverse(), "duplicated or out of order"),
        (lambda rows: rows[3].update(foreign_eok=float("nan")), "not a finite number"),
        (lambda rows: rows[3].update(foreign_eok=True), "not a finite number"),
        (lambda rows: rows.__delitem__(slice(0, len(rows) - 10)), "session count"),
    ):
        broken = json.loads(json.dumps(payload))
        mutate(broken["markets"]["KOSPI"]["sessions"])
        assert any(text in error for error in MODULE.validate_payload(broken, weekdays)), text
    wrong = json.loads(json.dumps(payload))
    wrong["production_weight"] = 0.1
    assert MODULE.validate_payload(wrong, weekdays)


def test_http_get_retries_server_errors_but_not_client_errors():
    class Reply:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.body

    outcomes = [urllib.error.HTTPError("u", 503, "busy", {}, None), TimeoutError("slow"), Reply(b"{}")]
    pauses = []

    def opener(request, timeout):
        assert request.headers["User-agent"].startswith("stock-pred-research-chart")
        result = outcomes.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert MODULE.http_get("https://example.test/x", opener=opener, sleep=pauses.append) == b"{}"
    assert pauses == [2.0, 4.0]
    attempts = []

    def forbidden(request, timeout):
        attempts.append(1)
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)

    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        MODULE.http_get("https://example.test/y", opener=forbidden, sleep=pauses.append)
    assert len(attempts) == 1


def test_raw_responses_are_kept_when_a_directory_is_given(tmp_path):
    naver = FakeNaver(weekdays("2025-01-01", "2026-11-24"))
    rows, skipped = MODULE.fetch_sessions("KOSPI", "2026-11-24", 3, get=naver, raw_dir=tmp_path / "raw",
                                          sleep=lambda seconds: None)
    assert [row["date"] for row in rows] == ["2026-11-20", "2026-11-23", "2026-11-24"] and skipped == []
    assert json.loads((tmp_path / "raw/KOSPI_20261124_p0.json").read_text())["content"][0]["bizdate"] == "20261124"


def test_window_date_comes_from_the_korean_calendar_not_the_runner_clock(tmp_path):
    database = tmp_path / "index_forecast.sqlite"
    conn = sqlite3.connect(database)
    conn.execute("CREATE TABLE investor_flow (market TEXT, session_date TEXT, investor_type TEXT, net_value REAL,"
                 " source TEXT, ingested_at TEXT)")
    conn.close()
    with pytest.raises(ValueError, match="starts None"):
        MODULE.collect(db_path=database, today=None)
    assert MODULE.kst_date(datetime(2026, 11, 24, 16, 0, tzinfo=UTC)) == "2026-11-25"
    assert MODULE.kst_date(datetime(2026, 11, 24, 14, 59, tzinfo=UTC)) == "2026-11-24"


def test_flow_status_marks_provisional_dates():
    payload = chart_payload(dated_rows(30))
    plain = render(payload)["kospi-flow-status"]["textContent"]
    assert "잠정" not in plain
    payload["provisional_dates"] = ["2024-01-30"]
    marked = render(payload)["kospi-flow-status"]["textContent"]
    assert "2024-01-30 잠정치" in marked and "확정값으로 교체" in marked
    payload["provisional_dates"] = ["not-a-date"]
    assert "잠정" not in render(payload)["kospi-flow-status"]["textContent"]


def test_page_says_the_flow_source_is_unofficial_and_the_latest_day_can_be_provisional():
    section = (ROOT / "docs/reliable-index-forecast.html").read_text().split('id="kospi-flow"', 1)[1].split("</section>", 1)[0]
    assert "비공식" in section and "잠정치" in section and "20:30" in section
    assert "기타법인은 제외" in section


def workflow_steps():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["forecast"]["steps"]


def test_flow_step_runs_without_the_runner_sqlite_and_cannot_stop_the_forecast():
    steps = workflow_steps()
    names = [step["name"] for step in steps]
    step = steps[names.index("Collect KOSPI investor flow chart")]
    assert step["if"] == "steps.operation.outputs.collect == 'true'"
    assert step["continue-on-error"] is True and step["timeout-minutes"] == 2
    assert "--sqlite" not in step["run"] and "index_forecast.sqlite" not in step["run"]
    assert "--output docs/data/kospi_investor_flow_chart.json" in step["run"]
    assert "secrets." not in json.dumps(step)
    # Runs after the restored cache is open and before the price refresh and the forecast.
    assert names.index("Open private cache") < names.index(step["name"]) < names.index("Refresh prices")
    # The chart file is committed by the existing reviewed-public-payload step, nothing else.
    publish = steps[names.index("Commit reviewed public payloads")]
    assert "!= 'validate'" in publish["if"] and "kospi" not in publish["run"]
    from roboquant.index_forecast.automation_state import PUBLIC_NAMES
    assert "kospi_investor_flow_chart.json" in PUBLIC_NAMES


def test_validate_mode_exercises_the_collector_into_the_temp_dir_and_never_publishes():
    steps = workflow_steps()
    names = [step["name"] for step in steps]
    step = steps[names.index("Validate KOSPI investor flow chart without publication")]
    # Validate-only, and still runs when an unrelated earlier contract test failed.
    assert step["if"] == "steps.operation.outputs.mode == 'validate' && !cancelled()"
    assert "continue-on-error" not in step
    assert '--output "$RUNNER_TEMP/kospi-flow-validation/' in step["run"]
    assert "--seed docs/data/kospi_investor_flow_chart.json" in step["run"]
    assert "--output docs/" not in step["run"] and "git " not in step["run"]
    upload = steps[names.index("Upload compact validation status")]
    assert upload["if"] == "steps.operation.outputs.mode == 'validate' && !cancelled()"
    assert "kospi-flow-validation/kospi_investor_flow_chart.json" in upload["with"]["path"]
    assert upload["with"]["retention-days"] <= 14
    # The only steps that write docs/data skip validate mode, and so does the commit.
    assert "!= 'validate'" in steps[names.index("Commit reviewed public payloads")]["if"]
    collect = steps[names.index("Collect KOSPI investor flow chart")]
    assert "validate" not in collect["if"]
    gate = (ROOT / "scripts/accuracy_operation.py").read_text()
    assert "mode != 'validate'" in gate.split("args.phase == 'public-paths'", 1)[1].split("return", 1)[0]

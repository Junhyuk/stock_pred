from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

from roboquant.index_forecast import economic_sources
from roboquant.index_forecast.economic_sources import collect_economic_sources

NOW = datetime(2026, 11, 13, 15, 0, tzinfo=UTC)


def _collect(*args, **kwargs):
    kwargs.setdefault("clock", lambda: NOW)
    return collect_economic_sources(*args, **kwargs)


def _response(body: str, *, content_type: str = "text/html; charset=utf-8"):
    return SimpleNamespace(
        status=200,
        headers={"Content-Type": content_type},
        body=body.encode("utf-8"),
    )


def test_bls_first_release_parses_published_values_and_does_not_fill_missing_yoy():
    body = """
    <h1>Consumer Price Index Summary</h1>
    <p>Transmission of material in this release is embargoed until
    8:30 a.m. (ET) Tuesday, November 10, 2026</p>
    <p>The Consumer Price Index for All Urban Consumers (CPI-U) increased
    0.4 percent on a seasonally adjusted basis in October 2026.</p>
    <p>The index for all items less food and energy rose 0.3 percent in October.</p>
    <p>BLS did not collect survey data for October 2025. As a result, BLS
    cannot publish 12-month percent changes for October 2026.</p>
    <table><tr><td>All items</td><td>0.4</td><td>-</td></tr></table>
    </html>
    """
    spec = {
        "source_id": "us_bls_cpi_current",
        "country": "US",
        "kind": "actual",
        "source_url": "https://www.bls.gov/news.release/cpi.nr0.htm",
        "parser": "bls_cpi",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: _response(body),
    )

    assert result["budget_exhausted"] is False
    assert result["sources"][0]["status"] == "partial"
    assert result["sources"][0]["source_hash"] == hashlib.sha256(body.encode()).hexdigest()
    assert result["sources"][0]["first_seen_at"] == NOW.isoformat()
    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "cpi_headline_mom": 0.4,
        "cpi_core_mom": 0.3,
    }
    assert all(row["vintage"] == "first_release" for row in result["releases"])
    assert all(row["actual_release_at"] == "2026-11-10T08:30:00-05:00" for row in result["releases"])
    assert all(row["actual_release_at_utc"] == "2026-11-10T13:30:00+00:00" for row in result["releases"])
    assert all(row["seasonal_adjustment"] == "SA" for row in result["releases"])
    assert not any(row["measure"] == "cpi_headline_yoy" for row in result["releases"])
    assert any("12-month" in item for item in result["sources"][0]["unavailable_measures"])


def test_annual_api_key_source_is_explicitly_unavailable_without_a_request():
    calls = []
    spec = {
        "source_id": "korea_kosis_api",
        "country": "KR",
        "kind": "actual_api",
        "source_url": "https://kosis.kr/openapi/Param/statisticsParameterData.do",
        "parser": "none",
        "access": "api_key_required",
        "format": "json",
        "key_env": "KOSIS_API_KEY",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert calls == []
    assert result["releases"] == []
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "api_key_required"
    assert result["sources"][0]["key_env"] == "KOSIS_API_KEY"


def test_a_timeout_is_retried_once_and_is_not_reported_as_an_empty_success():
    calls = []

    def timeout(url, timeout_seconds):
        calls.append((url, timeout_seconds))
        raise TimeoutError("fixture timeout")

    spec = {
        "source_id": "ea_unemployment",
        "country": "EA",
        "kind": "actual",
        "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m",
        "parser": "eurostat_unemployment",
        "access": "public_no_key",
        "format": "json",
    }

    result = _collect(
        now=NOW,
        retries=1,
        timeout_seconds=3,
        registry={"sources": [spec]},
        fetch=timeout,
    )

    assert len(calls) == 2
    assert calls[0][1] == calls[1][1] == 3
    assert result["sources"][0]["attempt_count"] == 2
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "timeout"
    assert result["releases"] == []


def test_bls_schedule_keeps_2027_release_time_and_eastern_timezone():
    body = """
    <h1>Schedule of Selected Releases 2027</h1>
    <p>Friday, January 08, 2027 08:30 AM Employment Situation for December 2026</p>
    <p>Wednesday, January 13, 2027 08:30 AM Consumer Price Index for December 2026</p>
    <p>NOTE: All times on calendar are Eastern Time.</p>
    <p>Last Modified Date: November 09, 2026</p>
    </html>
    """
    spec = {
        "source_id": "us_bls_selected_releases_2027",
        "country": "US",
        "kind": "schedule",
        "source_url": "https://www.bls.gov/schedule/2027/home.htm",
        "parser": "bls_schedule",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: _response(body),
    )

    assert result["sources"][0]["status"] == "ok"
    assert len(result["events"]) == 2
    jobs, cpi = result["events"]
    assert (jobs["event_type"], jobs["scheduled_date"], jobs["scheduled_at"]) == (
        "employment",
        "2027-01-08",
        "2027-01-08T08:30:00-05:00",
    )
    assert (cpi["event_type"], cpi["scheduled_date"], cpi["scheduled_at"]) == (
        "inflation",
        "2027-01-13",
        "2027-01-13T08:30:00-05:00",
    )
    assert all(event["timezone"] == "America/New_York" for event in result["events"])
    assert jobs["scheduled_at_utc"] == "2027-01-08T13:30:00+00:00"
    assert all(event["time_precision"] == "datetime" for event in result["events"])


def _start_server(handler_type):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_type)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def test_default_http_client_is_killed_at_its_wall_clock_deadline():
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(2)
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(b"late")
            except BrokenPipeError:
                pass

        def log_message(self, fmt, *args):
            return

    server, base_url = _start_server(SlowHandler)
    started = time.monotonic()

    try:
        economic_sources._wall_bounded_fetch(f"{base_url}/slow", 0.5)
    except TimeoutError as error:
        assert str(error) == "source_wall_timeout"
    else:
        raise AssertionError("a hanging source must be terminated")
    finally:
        server.shutdown()

    assert time.monotonic() - started < 1.5


def test_large_http_reply_is_drained_before_waiting_for_worker_exit():
    payload = b"x" * (1024 * 1024)

    class LargeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            return

    server, base_url = _start_server(LargeHandler)
    try:
        response = economic_sources._wall_bounded_fetch(f"{base_url}/large", 5)
    finally:
        server.shutdown()

    assert response.status == 200
    assert response.body == payload


def test_bls_current_series_uses_available_periods_and_never_invents_missing_yoy():
    body = json.dumps({
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUSR0000SA0", "data": [
            {"year": "2026", "period": "M10", "periodName": "October", "value": "337.476"},
            {"year": "2026", "period": "M09", "periodName": "September", "value": "336.130"},
            {"year": "2025", "period": "M09", "periodName": "September", "value": "321.950"},
        ]}]},
    })
    spec = {
        "source_id": "us_bls_cpi_latest_sa",
        "country": "US",
        "kind": "actual_api",
        "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/CUSR0000SA0?startyear=2025&endyear=2026",
        "parser": "bls_series",
        "access": "public_no_key",
        "format": "json",
        "series_id": "CUSR0000SA0",
        "seasonal_adjustment": "SA",
        "measures": [
            {"measure": "cpi_headline_mom", "transform": "pct_change_mom", "unit": "percent"},
            {"measure": "cpi_headline_yoy", "transform": "pct_change_yoy", "unit": "percent"},
        ],
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: SimpleNamespace(
            status=200, headers={"Content-Type": "application/json"}, body=body.encode()),
    )

    assert result["sources"][0]["status"] == "partial"
    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "cpi_headline_mom": 0.4,
    }
    assert "cpi_headline_yoy:no_matching_reference_month" in result["sources"][0]["unavailable_measures"]
    assert all(row["vintage"] == "current_snapshot" for row in result["releases"])


def test_bea_latest_release_parses_both_pce_rates_and_keeps_release_time_unknown():
    body = """
    Personal Income and Outlays, September 2026
    From the preceding month, the PCE price index for September increased 0.5 percent.
    Excluding food and energy, the PCE price index increased 0.3 percent.
    From the same month one year ago, the PCE price index for September increased 3.6 percent.
    Excluding food and energy, the PCE price index increased 3.1 percent from one year ago.
    Current release: October 29, 2026.
    """
    spec = {
        "source_id": "us_bea_pce_latest",
        "country": "US",
        "kind": "actual",
        "source_url": "https://www.bea.gov/news/current-releases",
        "parser": "bea_pce",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: _response(body),
    )

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "pce_headline_mom": 0.5,
        "core_pce_mom": 0.3,
        "pce_headline_yoy": 3.6,
        "core_pce_yoy": 3.1,
    }
    assert all(row["actual_release_at"] is None for row in result["releases"])
    assert all(row["vintage"] == "first_release" for row in result["releases"])


def test_korean_cpi_first_release_parses_separate_core_and_headline_values():
    body = """
    Consumer Price Index in October 2026
    Date 2026-11-03
    The Consumer Price Index was 120.73 (2020=100) in October 2026.
    The index increased 0.2 percent from the preceding month and rose 2.8 percent from the same month of the previous year.
    The index excluding food and energy was 116.98 in October 2026.
    The index increased 0.4 percent from the preceding month and rose 2.8 percent from the same month of the previous year.
    """
    spec = {
        "source_id": "kr_mods_cpi_latest",
        "country": "KR",
        "kind": "actual",
        "source_url": "https://mods.go.kr/board.es?bid=11751&mid=a20101000000",
        "parser": "mods_cpi",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: _response(body),
    )

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "cpi_headline_mom": 0.2,
        "cpi_headline_yoy": 2.8,
        "cpi_core_mom": 0.4,
        "cpi_core_yoy": 2.8,
    }
    assert all(row["reference_period"] == "2026-10" for row in result["releases"])
    assert all(row["vintage"] == "first_release" for row in result["releases"])


def test_japanese_stat_pages_decode_shift_jis_and_keep_current_snapshot_vintage():
    body = """<html><meta charset=Shift_JIS><body>
    2025年基準 消費者物価指数 全国 2026年9月分（2026年10月23日公表）
    (1) 総合指数は2025年を100として102.2 前年同月比は2.1%の上昇
    (2) 生鮮食品を除く総合指数は102.3 前年同月比は2.1％の上昇
    </body></html>""".encode("shift_jis")
    spec = {
        "source_id": "jp_stats_cpi_latest",
        "country": "JP",
        "kind": "actual",
        "source_url": "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html",
        "parser": "jp_cpi",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: SimpleNamespace(
            status=200, headers={"Content-Type": "text/html; charset=Shift_JIS"}, body=body),
    )

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "cpi_headline_yoy": 2.1,
        "cpi_core_yoy": 2.1,
    }
    assert all(row["reference_period"] == "2026-09" for row in result["releases"])
    assert all(row["vintage"] == "current_snapshot" for row in result["releases"])


def test_event_id_is_stable_when_a_known_release_date_is_rescheduled():
    source = {
        "source_id": "us_bls_selected_releases_2027",
        "country": "US",
        "source_url": "https://www.bls.gov/schedule/2027/home.htm",
    }
    first = economic_sources._event(
        source, NOW, "a" * 64, "employment", "Employment Situation", "2027-01-08",
        timezone="America/New_York", status="confirmed", reference_period="2026-12")
    revised = economic_sources._event(
        source, NOW, "b" * 64, "employment", "Employment Situation", "2027-01-15",
        timezone="America/New_York", status="confirmed", reference_period="2026-12")

    assert first["event_id"] == revised["event_id"]
    assert first["scheduled_date"] != revised["scheduled_date"]


def test_fed_schedule_does_not_invent_a_statement_clock_time():
    body = """
    <h2>2026 FOMC Meetings</h2>
    <p>December 8-9</p>
    <h2>2027 FOMC Meetings</h2>
    <p>January 26-27</p>
    <p>March 16-17</p>
    <h2>2028</h2>
    <p>January 25-26</p>
    """
    spec = {
        "source_id": "us_fed_fomc_schedule",
        "country": "US",
        "kind": "schedule",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "parser": "fed_schedule",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: _response(body),
    )

    assert [event["scheduled_date"] for event in result["events"]] == [
        "2026-12-09", "2027-01-27", "2027-03-17"]
    assert all(event["scheduled_at"] is None for event in result["events"])
    assert all(event["time_precision"] == "date" for event in result["events"])


def test_unclassified_release_vintage_defaults_to_current_snapshot():
    source = {
        "source_id": "example_official_current_page",
        "country": "EA",
        "source_url": "https://official.example/current",
    }

    release = economic_sources._release(
        source, NOW, "c" * 64, "unemployment_rate", 6.5, "percent", "2026-09")

    assert release["vintage"] == "current_snapshot"


def test_latest_release_index_follows_the_published_official_item():
    listing_url = "https://www.bea.gov/news/current-releases"
    article_url = "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026"
    listing = '<a href="/news/2026/personal-income-and-outlays-september-2026">Personal Income and Outlays, September 2026</a>'
    article = """
    Personal Income and Outlays, September 2026
    From the preceding month, the PCE price index for September increased 0.5 percent.
    Excluding food and energy, the PCE price index increased 0.3 percent.
    From the same month one year ago, the PCE price index for September increased 3.6 percent.
    Excluding food and energy, the PCE price index increased 3.1 percent from one year ago.
    """
    responses = {
        listing_url: _response(listing),
        article_url: _response(article),
    }
    calls = []
    spec = {
        "source_id": "us_bea_pce_latest",
        "country": "US",
        "kind": "actual",
        "source_url": listing_url,
        "parser": "bea_pce",
        "follow_link_text": "Personal Income and Outlays",
        "access": "public_no_key",
        "format": "html",
    }

    def fetch(url, timeout_seconds):
        calls.append(url)
        return responses[url]

    result = _collect(now=NOW, registry={"sources": [spec]}, fetch=fetch)

    assert calls == [listing_url, article_url]
    assert result["sources"][0]["resolved_source_url"] == article_url
    assert result["sources"][0]["discovery_source_hash"] == hashlib.sha256(listing.encode()).hexdigest()
    assert {row["measure"] for row in result["releases"]} == {
        "pce_headline_mom", "pce_headline_yoy", "core_pce_mom", "core_pce_yoy"}
    assert all(row["source_url"] == article_url for row in result["releases"])


def test_eurostat_current_observations_are_point_in_time_snapshots():
    body = json.dumps({
        "version": "2.0",
        "class": "dataset",
        "label": "Unemployment by sex and age - monthly data",
        "updated": "2026-11-05T23:00:00+0100",
        "id": ["freq", "unit", "s_adj", "age", "sex", "geo", "time"],
        "size": [1, 1, 1, 1, 1, 1, 2],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}}},
            "unit": {"category": {"index": {"PC_ACT": 0}}},
            "s_adj": {"category": {"index": {"SA": 0}}},
            "age": {"category": {"index": {"TOTAL": 0}}},
            "sex": {"category": {"index": {"T": 0}}},
            "geo": {"category": {"index": {"EA21": 0}}},
            "time": {"category": {"index": {"2026-09": 0, "2026-08": 1}}},
        },
        "value": {"0": 6.5, "1": 6.4},
    })
    spec = {
        "source_id": "ea_eurostat_unemployment_api",
        "country": "EA",
        "kind": "actual_api",
        "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m",
        "parser": "eurostat_api",
        "access": "public_no_key",
        "format": "json-stat",
        "measure": "unemployment_rate",
        "unit": "percent",
        "seasonal_adjustment": "SA",
        "definition_version": "eurostat:une_rt_m:EA21:T:TOTAL:SA:PC_ACT",
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: SimpleNamespace(
            status=200, headers={"Content-Type": "application/json"}, body=body.encode()),
    )

    assert result["sources"][0]["status"] == "ok"
    assert result["releases"][0]["measure"] == "unemployment_rate"
    assert result["releases"][0]["value"] == 6.5
    assert result["releases"][0]["reference_period"] == "2026-09"
    assert result["releases"][0]["vintage"] == "current_snapshot"
    assert result["releases"][0]["numeric_eligibility"] == "observed_current_receipt_v1"
    assert result["releases"][0]["actual_release_at"] is None


def test_japan_cpi_schedule_parses_current_and_next_year_as_date_only():
    body = """
    <table>
      <tr><th>Survey month</th><th>Date of release</th></tr>
      <tr><td>December 2026</td><td>January 22, 2027</td></tr>
      <tr><td>January, 2027</td><td>February 19</td></tr>
    </table>
    """
    spec = {
        "source_id": "jp_stats_cpi_schedule",
        "country": "JP",
        "kind": "schedule",
        "source_url": "https://www.stat.go.jp/english/data/cpi/1582.htm",
        "parser": "jp_cpi_schedule",
        "schedule_kind": "cpi",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout_seconds: _response(body))

    assert [(event["reference_period"], event["scheduled_date"]) for event in result["events"]] == [
        ("2026-12", "2027-01-22"), ("2027-01", "2027-02-19")]
    assert all(event["scheduled_at"] is None for event in result["events"])
    assert all(event["timezone"] == "Asia/Tokyo" for event in result["events"])


def test_boj_meeting_calendar_uses_decision_day_not_a_guessed_time():
    body = """
    <p>Table : 2026</p>
    <table>
      <tr><th>Date of MPM</th><th>Release Schedule</th></tr>
      <tr><td>Dec. 17 (Thurs.), 18 (Fri.)</td><td>-</td></tr>
    </table>
    <p>Table : 2027</p>
    <table>
      <tr><th>Date of MPM</th><th>Release Schedule</th></tr>
      <tr><td>Jan. 21 (Thurs.), 22 (Fri.)</td><td>Jan. 22 (Fri.)</td></tr>
    </table>
    """
    spec = {
        "source_id": "jp_boj_meeting_schedule",
        "country": "JP",
        "kind": "schedule",
        "source_url": "https://www.boj.or.jp/en/mopo/mpmsche_minu/",
        "parser": "boj_schedule",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout_seconds: _response(body))

    assert [event["scheduled_date"] for event in result["events"]] == [
        "2026-12-18", "2027-01-22"]
    assert all(event["scheduled_at"] is None for event in result["events"])
    assert all(event["time_precision"] == "date" for event in result["events"])


def test_ecb_schedule_uses_policy_day_and_preserves_date_only_precision():
    body = """
    <p>03/02/2027 Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 1)</p>
    <p>04/02/2027 Governing Council of the ECB: monetary policy meeting in Frankfurt (Day 2), followed by press conference</p>
    <p>24/02/2027 Governing Council of the ECB: non-monetary policy meeting (virtual)</p>
    """
    spec = {
        "source_id": "ea_ecb_meeting_schedule",
        "country": "EA",
        "kind": "schedule",
        "source_url": "https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html",
        "parser": "ecb_schedule",
        "access": "public_no_key",
        "format": "html",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout_seconds: _response(body))

    assert [(event["event_type"], event["scheduled_date"]) for event in result["events"]] == [
        ("policy_rate", "2027-02-04")]
    assert result["events"][0]["timezone"] == "Europe/Luxembourg"
    assert result["events"][0]["scheduled_at"] is None


def test_stale_api_reference_period_is_reported_unavailable_not_latest():
    body = json.dumps({
        "updated": "2026-02-06T23:00:00+0100",
        "id": ["freq", "unit", "coicop", "geo", "time"],
        "size": [1, 1, 1, 1, 1],
        "dimension": {
            "freq": {"category": {"index": {"M": 0}}},
            "unit": {"category": {"index": {"RCH_A": 0}}},
            "coicop": {"category": {"index": {"CP00": 0}}},
            "geo": {"category": {"index": {"EA21": 0}}},
            "time": {"category": {"index": {"2025-12": 0}}},
        },
        "value": {"0": 4.0},
    })
    spec = {
        "source_id": "ea_eurostat_hicp_latest_api",
        "country": "EA",
        "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr",
        "parser": "eurostat_api",
        "access": "public_no_key",
        "format": "json-stat",
        "measure": "cpi_headline_yoy",
        "unit": "percent",
        "seasonal_adjustment": "NSA",
        "definition_version": "eurostat:prc_hicp_manr:EA21:CP00:RCH_A",
        "max_reference_lag_months": 2,
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec]},
        fetch=lambda url, timeout_seconds: SimpleNamespace(
            status=200, headers={"Content-Type": "application/json"}, body=body.encode()),
    )

    assert result["releases"] == []
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "stale_reference_period:2025-12"


def test_fed_current_rate_without_date_is_not_ingested():
    body = "Current target range for the federal funds rate is 3-1/2 to 3-3/4 percent."
    spec = {
        "source_id": "us_fed_current_policy_rate",
        "country": "US",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/openmarket.htm",
        "parser": "fed_policy",
        "access": "public_no_key",
        "format": "html",
        "definition_version": "fed:current-target-range",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout_seconds: _response(body))

    assert result["releases"] == []
    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "policy_rate_reference_date_not_found"


def test_source_http_health_does_not_claim_expected_measure_coverage():
    spec = {
        "source_id": "ea_policy_placeholder",
        "country": "EA",
        "source_url": "https://official.example/policy",
        "parser": "ecb_policy",
        "access": "public_no_key",
        "format": "html",
        "expected_measures": ["policy_rate"],
    }

    result = _collect(
        now=NOW,
        registry={"sources": [spec], "required_measures": {"EA": ["policy_rate"]}},
        fetch=lambda url, timeout_seconds: _response("Official page without a policy rate."),
    )

    assert result["sources"][0]["status"] == "unavailable"
    assert result["coverage"] == [{
        "country": "EA", "measure": "policy_rate", "status": "unavailable",
        "source_ids": ["ea_policy_placeholder"], "source_statuses": ["unavailable"],
        "reason": "deposit_facility_rate_not_found;actual_time_unknown",
        "verified_first_release_ready": False,
    }]


def test_fed_current_rate_page_reads_latest_official_table_level():
    body = """
    <h2>FOMC's target federal funds rate or range, change (basis points) and level (%)</h2>
    <h3>2026</h3>
    <table>
      <tr><th>Date</th><th>Increase</th><th>Decrease</th><th>Level (%)</th></tr>
      <tr><td>September 17</td><td>25</td><td>0</td><td>3.75-4.00</td></tr>
    </table>
    </html>
    """
    spec = {
        "source_id": "us_fed_current_target_range",
        "country": "US",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/openmarket.htm",
        "parser": "fed_policy",
        "access": "public_no_key",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "policy_rate_lower": 3.75,
        "policy_rate_upper": 4.0,
        "policy_rate": 3.875,
    }
    assert all(row["vintage"] == "current_snapshot" for row in result["releases"])


def test_fed_table_date_is_rate_reference_date_not_release_timestamp():
    body = """
    <h2>FOMC's target federal funds rate or range, change (basis points) and level</h2>
    <h3>2026</h3>
    <table>
      <tr><th>Date</th><th>Increase</th><th>Decrease</th><th>Level (%)</th></tr>
      <tr><td>September 17</td><td>25</td><td>0</td><td>3.75-4.00</td></tr>
    </table>
    </html>
    """
    spec = {
        "source_id": "us_fed_current_target_range",
        "country": "US",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/openmarket.htm",
        "parser": "fed_policy",
        "access": "public_no_key",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "policy_rate_lower": 3.75,
        "policy_rate_upper": 4.0,
        "policy_rate": 3.875,
    }
    assert next(row for row in result["releases"] if row["measure"] == "policy_rate")["derivation"] == "arithmetic_midpoint_of_lower_upper"
    assert result["sources"][0]["status"] == "ok"
    assert {row["reference_period"] for row in result["releases"]} == {"2026-09-17"}
    assert all(row["actual_release_date"] is None for row in result["releases"])
    assert all(row["actual_release_at"] is None for row in result["releases"])
    assert all(row["vintage"] == "current_snapshot" for row in result["releases"])


def test_japan_cpi_parser_accepts_official_spacing_and_keeps_reference_month():
    body = """
    <h1>2025年基準 消費者物価指数 全国 2026年（令和8年）9月分（2026年10月23日公表）</h1>
    <p>総合指数 は2025年を100として102.2</p>
    <p>前年同月比は2.1%の上昇</p>
    <p>生鮮食品を除く総合指数 は102.3</p>
    <p>前年同月比は2.1％の上昇</p>
    </html>
    """
    spec = {
        "source_id": "jp_stats_cpi_latest",
        "country": "JP",
        "source_url": "https://www.stat.go.jp/data/cpi/sokuhou/tsuki/index-z.html",
        "parser": "jp_cpi",
        "access": "public_no_key",
        "measure_definitions": {
            "cpi_headline_yoy": "Japan-Statistics:CPI:all-items:yoy:NSA:2025-base",
            "cpi_core_yoy": "Japan-Statistics:CPI:ex-fresh-food:yoy:NSA:2025-base",
        },
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "cpi_headline_yoy": 2.1,
        "cpi_core_yoy": 2.1,
    }
    assert {row["reference_period"] for row in result["releases"]} == {"2026-09"}
    assert all(row["vintage"] == "current_snapshot" for row in result["releases"])


def test_mods_employment_change_parser_does_not_stop_at_decimal_level():
    body = """
    <h1>Economically Active Population Survey in October 2026</h1>
    <p>Date 2026-11-11</p>
    <p>The number of employed persons totaled 29.179 million persons in October,
       which went up 139 thousand persons or 0.5% year-on-year.</p>
    <p>The unemployment rate marked 2.0% in October, down 0.2%p year-on-year.</p>
    </html>
    """
    spec = {
        "source_id": "kr_mods_employment_latest_release",
        "country": "KR",
        "source_url": "https://mods.go.kr/board.es?bid=11732&mid=a20105020000",
        "parser": "mods_employment",
        "access": "public_no_key",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert {row["measure"]: row["value"] for row in result["releases"]} == {
        "employment_yoy_change": 139.0,
        "unemployment_rate": 2.0,
    }
    assert {row["reference_period"] for row in result["releases"]} == {"2026-10"}


def test_eurostat_api_uses_latest_nonmissing_observation():
    body = json.dumps({
        "id": ["geo", "time"],
        "size": [1, 3],
        "updated": "2026-11-05T11:00:00+0100",
        "dimension": {
            "geo": {"category": {"index": {"EA21": 0}}},
            "time": {"category": {"index": {"2026-08": 0, "2026-09": 1, "2026-10": 2}}},
        },
        "value": {"0": 6.4, "1": 6.5},
    })
    spec = {
        "source_id": "ea_eurostat_unemployment_latest",
        "country": "EA",
        "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_m",
        "parser": "eurostat_api",
        "access": "public_no_key",
        "measure": "unemployment_rate",
        "unit": "percent",
        "seasonal_adjustment": "SA",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body, content_type="application/json"))

    assert len(result["releases"]) == 1
    assert result["releases"][0]["value"] == 6.5
    assert result["releases"][0]["reference_period"] == "2026-09"
    assert result["releases"][0]["vintage"] == "current_snapshot"


def test_bea_schedule_extracts_split_news_rows_and_december_year_boundary():
    body = """
    <h2>Year 2026</h2>
    <p>November 25</p><p>8:30 AM</p><p>N ews</p>
    <p>Personal Income and Outlays, October 2026</p>
    <h2>Year 2027</h2>
    <p>January 28</p><p>8:30 AM</p><p>N ews</p>
    <p>Personal Income and Outlays, December 2026</p>
    </html>
    """
    spec = {
        "source_id": "us_bea_release_schedule_current_year",
        "country": "US",
        "source_url": "https://www.bea.gov/news/schedule/full",
        "parser": "bea_schedule",
        "calendar_year": "current",
        "access": "public_no_key",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert [(row["scheduled_date"], row["scheduled_at"], row["reference_period"])
            for row in result["events"]] == [
        ("2026-11-25", "2026-11-25T08:30:00-05:00", "2026-10"),
        ("2027-01-28", "2027-01-28T08:30:00-05:00", "2026-12"),
    ]


def test_ecb_latest_current_summary_is_not_mislabeled_as_first_release():
    body = """
    <p>Latest press conference: 14 October 2026</p>
    <h2>Key ECB interest rates</h2>
    <p>With effect from: 16 September 2026</p>
    <h3>Deposit facility</h3>
    <p>2.50&nbsp;%</p>
    </html>
    """
    spec = {
        "source_id": "ea_ecb_latest_policy_rates",
        "country": "EA",
        "source_url": "https://www.ecb.europa.eu/press/press_conference/html/index.en.html",
        "parser": "ecb_policy",
        "access": "public_no_key",
        "vintage": "current_snapshot",
        "definition_version": "ECB:deposit-facility-rate:current:percent",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    assert len(result["releases"]) == 1
    release = result["releases"][0]
    assert (release["measure"], release["value"], release["unit"]) == (
        "policy_rate", 2.5, "percent")
    assert release["reference_period"] == "2026-09"
    assert release["vintage"] == "current_snapshot"
    assert release["actual_release_at"] is None
    assert release["actual_release_date"] is None


def test_fed_schedule_carries_year_sections_across_long_calendar_rows():
    body = """
    <h2>2026 FOMC Meetings</h2>
    <p>January 27-28</p><p>March 17-18</p><p>April 28-29</p>
    <p>June 16-17</p><p>July 28-29</p><p>September 15-16</p>
    <p>October 27-28</p><p>December 8-9</p>
    <h2>2027 FOMC Meetings</h2>
    <p>January 26-27</p><p>March 16-17</p><p>April 27-28</p>
    <p>June 8-9</p><p>July 27-28</p><p>September 14-15</p>
    <p>October 26-27</p><p>November 16-17</p>
    <p>Note: 2027 dates are tentative until confirmed at the preceding meeting.</p>
    </html>
    """
    spec = {
        "source_id": "us_fed_fomc_calendar",
        "country": "US",
        "source_url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
        "parser": "fed_schedule",
        "access": "public_no_key",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body))

    future = [event for event in result["events"] if event["scheduled_date"].startswith("2027")]
    assert [event["scheduled_date"] for event in future] == [
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
        "2027-07-28", "2027-09-15", "2027-10-27", "2027-11-17",
    ]
    assert all(event["status"] == "tentative" for event in future)


def test_eurostat_empty_selected_geo_is_reported_as_unavailable():
    body = json.dumps({
        "id": ["geo", "time"],
        "size": [0, 1],
        "dimension": {
            "geo": {"category": {"index": {}}},
            "time": {"category": {"index": {"2026-09": 0}}},
        },
        "value": {},
    })
    spec = {
        "source_id": "ea_eurostat_hicp_current_series",
        "country": "EA",
        "source_url": "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_manr?geo=EA21",
        "parser": "eurostat_api",
        "access": "public_no_key",
        "measure": "cpi_headline_yoy",
        "unit": "percent",
    }

    result = _collect(now=NOW, registry={"sources": [spec]},
                      fetch=lambda url, timeout: _response(body, content_type="application/json"))

    assert result["sources"][0]["status"] == "unavailable"
    assert result["sources"][0]["reason"] == "eurostat_no_data_for_dimension:geo"
    assert result["releases"] == []


def test_bls_dynamic_series_url_requests_three_calendar_years_in_january():
    requested_urls = []
    spec = {
        "source_id": "us_bls_cpi_headline_latest",
        "country": "US",
        "source_url": "https://api.bls.gov/publicAPI/v1/timeseries/data/CUSR0000SA0?startyear={start_year}&endyear={end_year}",
        "url_year": "current",
        "parser": "bls_series",
        "access": "public_no_key",
        "series_id": "CUSR0000SA0",
        "expected_measures": ["cpi_headline_yoy"],
        "measures": [{"measure": "cpi_headline_yoy", "transform": "pct_change_yoy", "unit": "percent"}],
    }
    api_body = json.dumps({
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{"seriesID": "CUSR0000SA0", "data": [
            {"year": "2027", "period": "M01", "value": "310.0"},
            {"year": "2026", "period": "M01", "value": "300.0"},
            {"year": "2025", "period": "M01", "value": "290.0"},
        ]}]},
    })
    january = datetime(2027, 1, 3, 0, 0, tzinfo=UTC)

    result = _collect(now=january, registry={"sources": [spec]},
                      fetch=lambda url, timeout: (requested_urls.append(url) or
                          _response(api_body, content_type="application/json")),
                      clock=lambda: january)

    assert requested_urls == [
        "https://api.bls.gov/publicAPI/v1/timeseries/data/CUSR0000SA0?startyear=2025&endyear=2027"]
    assert result["releases"][0]["value"] == 3.3
    assert result["releases"][0]["reference_period"] == "2027-01"

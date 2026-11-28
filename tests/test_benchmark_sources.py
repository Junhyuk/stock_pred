from __future__ import annotations

import json
import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from roboquant.index_forecast.benchmark_sources import (
    DEFAULT_RIGHTS,
    HttpResponse,
    SourceError,
    SourceRights,
    benchmark_source_status,
    collect_benchmark_history,
    import_benchmark_csv,
    load_benchmark_history,
)
from roboquant.index_forecast.indexes import RESEARCH_INDEXES, get_official_index


def krx(day="20261127", close="758.51", name="코스피 200"):
    return {"BAS_DD": day, "IDX_NM": name, "CLSPRC_IDX": close,
            "OPNPRC_IDX": "719.92", "HGPRC_IDX": "760.12", "LWPRC_IDX": "719.92",
            "ACC_TRDVOL": "20,000", "ACC_TRDVAL": "9000000"}


def response(rows):
    return HttpResponse(200, json.dumps({"OutBlock_1": rows}).encode())


def collect(path, rows=None, **kwargs):
    options = dict(index_code="KOSPI200", start_date="2026-11-27", end_date="2026-11-27",
                   env={"KRX_OPENAPI_KEY": "secret-test"},
                   fetch=lambda request, timeout: response([krx()] if rows is None else rows),
                   receipt_clock=lambda: "2026-11-28T00:00:00Z", sleep=lambda _: None)
    options.update(kwargs)
    return collect_benchmark_history(path, **options)


def test_exact_registry_does_not_expand_existing_universe():
    assert get_official_index("KOSPI200").pykrx_code == "1028"
    assert get_official_index("KOSDAQ150").pykrx_code == "2203"
    assert RESEARCH_INDEXES == ("KOSPI", "KOSDAQ", "KOSPI100", "KOSDAQ100")


def test_missing_key_has_real_typed_status_and_no_network(tmp_path):
    path = tmp_path / "private.sqlite"
    result = collect(path, env={}, fetch=lambda *args: pytest.fail("must not call"))
    assert result.reason_codes == ["credential_missing"]
    assert result.rows_received == 0
    status = benchmark_source_status(path, "KOSPI200", as_of="2026-11-28T01:00Z", expected_session="2026-11-27", env={})
    assert status["rows"] == 0
    assert status["allow_train"] is False
    assert status["strict_live_eligible"] is False
    assert status["official_code"] == "1028"
    assert path.stat().st_mode & 0o777 == 0o600


def test_exact_name_no_proxy_futures_or_total_return(tmp_path):
    path = tmp_path / "private.sqlite"
    result = collect(path, [krx(name="코스피"), krx(name="코스피 100"), krx(name="코스피 200 TR"), krx(name="코스피 200 선물")])
    assert result.rows_inserted == 0
    assert result.reason_codes == ["no_exact_index_rows"]
    result = collect(path, [krx(name="코스피200")])
    assert result.rows_inserted == 1
    assert load_benchmark_history(path, "KOSPI200").iloc[0]["close"] == 758.51
    assert load_benchmark_history(path, "KOSDAQ150").empty


def test_receipt_cutoff_and_revisions_are_immutable(tmp_path):
    path = tmp_path / "private.sqlite"
    collect(path, receipt_clock=lambda: "2026-11-28T00:00Z")
    collect(path, [krx(close="759.0")], receipt_clock=lambda: "2026-11-28T02:00Z")
    collect(path, [krx(close="759.0")], receipt_clock=lambda: "2026-11-28T03:00Z")
    before = load_benchmark_history(path, "KOSPI200", as_of="2026-11-27T23:59Z", strict_live=True)
    initial = load_benchmark_history(path, "KOSPI200", as_of="2026-11-28T01:00Z")
    revised = load_benchmark_history(path, "KOSPI200", as_of="2026-11-28T04:00Z")
    first = load_benchmark_history(path, "KOSPI200", first_observed=True)
    assert before.empty
    assert initial.iloc[0]["close"] == first.iloc[0]["close"] == 758.51
    assert revised.iloc[0]["close"] == 759.0
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM benchmark_observation").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM benchmark_collection").fetchone()[0] == 3
    assert load_benchmark_history(path, "KOSPI200", public_raw_only=True).empty
    assert len(load_benchmark_history(path, "KOSPI200", derived_only=True)) == 1
    assert first.iloc[0]["attribution"] == "한국거래소 통계정보"


def test_reverting_revision_is_a_new_version(tmp_path):
    path = tmp_path / "private.sqlite"
    for hour, close in enumerate(["758.51", "759.0", "758.51"]):
        collect(path, [krx(close=close)], receipt_clock=lambda hour=hour: f"2026-11-28T0{hour}:00Z")
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM benchmark_observation").fetchone()[0] == 3
    assert load_benchmark_history(path, "KOSPI200").iloc[0]["close"] == 758.51


@pytest.mark.parametrize("code,reason", [(401,"authentication_failed"),(403,"authentication_failed"),(429,"rate_limited"),(503,"network_error")])
def test_errors_are_typed_and_secret_is_not_persisted(tmp_path, code, reason):
    path = tmp_path / "private.sqlite"
    result = collect(path, fetch=lambda *args: HttpResponse(code, b"secret-test"))
    assert result.reason_codes == [reason]
    with sqlite3.connect(path) as conn:
        assert "secret-test" not in conn.execute("SELECT result_json FROM benchmark_collection").fetchone()[0]


@pytest.mark.parametrize("row", [krx(close="NaN"),krx(close="0"),krx(close="761"),krx(day="20261126")])
def test_invalid_quotes_and_wrong_requested_session_are_not_saved(tmp_path, row):
    path = tmp_path / "private.sqlite"
    result = collect(path, [row])
    assert result.reason_codes == ["invalid_response"]
    assert load_benchmark_history(path, "KOSPI200").empty


def test_fsc_business_day_publication_and_derived_scope(tmp_path):
    path = tmp_path / "private.sqlite"
    calls = []
    def fetch(request, timeout):
        calls.append(parse_qs(urlparse(request.full_url).query))
        row = {"idxNm": "코스피 200", "basDt": "20261127", "clpr": "758.51"}
        return HttpResponse(200, json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": {"item": [row]}}}}).encode())
    result = collect(path, source="fsc_openapi", env={"DATA_GO_KR_SERVICE_KEY": "ab%2Bcd%3D"}, fetch=fetch)
    assert result.rows_inserted == 1
    assert calls[0]["serviceKey"] == ["ab+cd="]
    assert calls[0]["idxNm"] == ["코스피 200"]
    assert load_benchmark_history(path, "KOSPI200", as_of="2026-11-30T03:59Z").empty
    frame = load_benchmark_history(path, "KOSPI200", as_of="2026-11-30T04:01Z")
    assert len(frame) == 1
    assert load_benchmark_history(path, "KOSPI200", derived_only=True).empty
    # Same date with independent KRX rights remains a valid derived lineage.
    collect(path)
    assert len(load_benchmark_history(path, "KOSPI200", derived_only=True)) == 1


def test_krx_get_request_budget_resumes_missing_dates(tmp_path):
    path = tmp_path / "private.sqlite"
    called = []
    def fetch(request, timeout):
        assert request.get_method() == "GET"
        day = parse_qs(urlparse(request.full_url).query)["basDd"][0]
        called.append(day)
        return response([krx(day=day)])
    kwargs = dict(start_date="2026-11-23", refresh_sessions=1, max_requests=2, fetch=fetch)
    a = collect(path, **kwargs)
    assert a.reason_codes == ["request_budget_exhausted"]
    assert called == ["20261127", "20261126"]
    b = collect(path, **kwargs)
    assert b.rows_inserted == 1
    assert called[-2:] == ["20261127", "20261125"]


def test_krx_absent_exact_index_is_checkpointed_across_restarts(tmp_path):
    path = tmp_path / "private.sqlite"
    called = []
    def fetch(request, timeout):
        day = parse_qs(urlparse(request.full_url).query)["basDd"][0]
        called.append(day)
        # A successful, dated official KOSDAQ-series response may legitimately
        # lack the exact target before inception. It is not a substitute quote.
        row = krx(day=day, name="코스닥" if day == "20261125" else "코스닥 150")
        row["IDX_CLSS"] = "KOSDAQ"
        return response([row])
    kwargs = dict(index_code="KOSDAQ150", start_date="2026-11-23", refresh_sessions=1,
                  max_requests=2, fetch=fetch)
    first = collect(path, **kwargs)
    second = collect(path, **kwargs)
    third = collect(path, **kwargs)
    fourth = collect(path, **kwargs)
    assert first.rows_inserted == 2
    assert second.rows_inserted == 0
    assert third.rows_inserted == fourth.rows_inserted == 1
    assert called == ["20261127", "20261126", "20261127", "20261125",
                      "20261127", "20261124", "20261127", "20261123"]
    assert fourth.remaining_sessions == 0
    frame = load_benchmark_history(path, "KOSDAQ150", derived_only=True)
    assert len(frame) == 4
    assert "2026-11-25" not in frame.index.strftime("%Y-%m-%d")
    assert set(frame.official_code) == {"2203"}
    assert not frame.allow_chart.any()
    with sqlite3.connect(path) as conn:
        checked = conn.execute("SELECT session_date,index_code,source,response_rows FROM benchmark_absent_session").fetchall()
    assert checked == [("2026-11-25", "KOSDAQ150", "krx_openapi", 1)]


def test_krx_small_budget_preserves_backfill_progress(tmp_path):
    path = tmp_path / "private.sqlite"
    called = []
    def fetch(request, timeout):
        day = parse_qs(urlparse(request.full_url).query)["basDd"][0]
        called.append(day)
        return response([krx(day=day)])
    for _ in range(5):
        collect(path, start_date="2026-11-23", max_requests=1, refresh_sessions=3, fetch=fetch)
    assert called == ["20261127", "20261126", "20261125", "20261124", "20261123"]
    assert len(load_benchmark_history(path, "KOSPI200")) == 5


@pytest.mark.parametrize("rows", [[], [krx(name="코스닥")],
                                  [{**krx(name="코스닥"), "IDX_CLSS": "KOSDAQ"}]])
def test_krx_empty_or_wrong_series_absence_remains_retryable(tmp_path, rows):
    path = tmp_path / "private.sqlite"
    calls = []
    def fetch(request, timeout):
        calls.append(request.full_url)
        return response(rows)
    for _ in range(2):
        collect(path, refresh_sessions=0, fetch=fetch)
    assert len(calls) == 2
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM benchmark_absent_session").fetchone()[0] == 0


def test_krx_recent_confirmed_absence_is_rechecked_for_delayed_publication(tmp_path):
    path = tmp_path / "private.sqlite"
    absent = {**krx(name="코스닥"), "IDX_CLSS": "KOSDAQ"}
    first = collect(path, [absent], index_code="KOSDAQ150", refresh_sessions=1)
    assert first.absent_sessions_checked == 1
    second = collect(path, [krx(name="코스닥 150")], index_code="KOSDAQ150", refresh_sessions=1)
    assert second.rows_inserted == 1
    assert len(load_benchmark_history(path, "KOSDAQ150")) == 1


def test_krx_multiyear_restarts_reach_model_history_before_old_absences(tmp_path):
    from roboquant.index_forecast.market_calendar import exchange_schedule

    path = tmp_path / "private.sqlite"
    _, sessions = exchange_schedule("XKRX", "2022-01-03", "2026-11-27")
    days = sessions.strftime("%Y%m%d").tolist()
    absent_days = set(days[:10])
    called = []
    def fetch(request, timeout):
        day = parse_qs(urlparse(request.full_url).query)["basDd"][0]
        called.append(day)
        row = krx(day=day, name="코스닥" if day in absent_days else "코스닥 150")
        row["IDX_CLSS"] = "KOSDAQ"
        return response([row])
    options = dict(index_code="KOSDAQ150", start_date="2022-01-03", max_requests=400,
                   refresh_sessions=3, fetch=fetch, budget_seconds=60)
    first = collect(path, **options)
    assert first.rows_inserted == 400
    assert called == list(reversed(days))[:400]
    for _ in range(4):
        result = collect(path, **options)
        if result.remaining_sessions == 0:
            break
    assert result.remaining_sessions == 0
    history = load_benchmark_history(path, "KOSDAQ150", derived_only=True)
    assert len(history) == len(days) - len(absent_days) >= 1089
    assert all(called.count(day) == 1 for day in absent_days)
    assert not history.allow_chart.any()
    calls_before = len(called)
    collect(path, **options)
    assert called[calls_before:] == list(reversed(days))[:3]


def test_fsc_partial_backfill_narrows_missing_range(tmp_path):
    path = tmp_path / "private.sqlite"
    queries = []
    def fetch(request, timeout):
        q = parse_qs(urlparse(request.full_url).query)
        queries.append(q)
        row = {"idxNm": "코스피 200", "basDt": "20261127", "clpr": "758.51"}
        return HttpResponse(200, json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 1001, "items": {"item": [row]}}}}).encode())
    for _ in range(2):
        collect(path, source="fsc_openapi", env={"DATA_GO_KR_SERVICE_KEY": "test"}, start_date="2026-11-23", max_requests=1, fetch=fetch)
    assert queries[0]["endBasDt"] == ["20261128"]
    assert queries[1]["endBasDt"] == ["20261127"]


def test_licensed_csv_cannot_backdate_receipts_or_use_proxy(tmp_path):
    path, csv = tmp_path / "private.sqlite", tmp_path / "input.csv"
    rights = SourceRights(True, True, True, "https://license.example/grant", "Licensed research dataset")
    csv.write_text("index_code,official_code,session_date,close,received_at\nKOSDAQ150,2203,2025-01-02,1234,2025-01-02T08:00Z\n")
    result = import_benchmark_csv(path, csv, index_code="KOSDAQ150", source="licensed_export", source_url="https://source.example/export", rights=rights, receipt_clock=lambda: "2026-11-28T00:00Z")
    assert result.rows_inserted == 1
    assert load_benchmark_history(path, "KOSDAQ150", as_of="2025-01-03T00:00Z").empty
    row = load_benchmark_history(path, "KOSDAQ150", public_raw_only=True).iloc[0]
    assert row["received_at"] == "2026-11-28T00:00:00+00:00"
    assert row["pit_class"] == "historical_reconstruction"
    csv.write_text("index_code,official_code,session_date,close\nKOSDAQ150,2001,2025-01-02,1234\n")
    with pytest.raises(SourceError):
        import_benchmark_csv(path, csv, index_code="KOSDAQ150", source="licensed_export", source_url="https://source.example/export", rights=rights)


def test_scope_revocation_does_not_resurrect_earlier_grant(tmp_path):
    path = tmp_path / "private.sqlite"
    collect(path)
    rights = SourceRights(True, False, False, "https://license.example/revocation", "한국거래소 통계정보")
    collect(path, rights=rights, receipt_clock=lambda: "2026-11-28T01:00Z")
    assert load_benchmark_history(path, "KOSPI200", derived_only=True).empty
    assert load_benchmark_history(path, "KOSPI200", derived_only=True, first_observed=True).empty


def test_private_destination_and_aware_cutoff_required(tmp_path):
    with pytest.raises(ValueError, match="public site"):
        collect(tmp_path / "docs" / "private.sqlite")
    with pytest.raises(ValueError, match="as_of"):
        load_benchmark_history(tmp_path / "private.sqlite", "KOSPI200", strict_live=True)
    with pytest.raises(ValueError, match="timezone"):
        benchmark_source_status(tmp_path / "private.sqlite", "KOSPI200", as_of="2026-11-28")
    assert DEFAULT_RIGHTS["krx_openapi"].allow_chart is False
    with pytest.raises(ValueError):
        SourceRights("false", True, False, "https://evidence.example", "source")


def test_status_eligibility_depends_on_exact_session_and_receipt(tmp_path):
    path = tmp_path / "private.sqlite"
    collect(path)
    good = benchmark_source_status(path, "KOSPI200", as_of="2026-11-28T01:00Z", expected_session="2026-11-27")
    stale = benchmark_source_status(path, "KOSPI200", as_of="2026-11-30T08:00Z", expected_session="2026-11-30")
    assert good["strict_live_eligible"] is True
    assert stale["strict_live_eligible"] is False
    assert "stale_anchor" in stale["reason_codes"]
    assert "758.51" not in json.dumps(good)


def test_fallback_preserves_restricted_rights_and_network_retry(tmp_path):
    path = tmp_path / "private.sqlite"
    calls = []
    def fetch(request, timeout):
        calls.append(request.full_url)
        if "data-dbg.krx" in request.full_url:
            return HttpResponse(401, b"")
        if len(calls) == 2:
            raise TimeoutError("simulated timeout")
        row = {"idxNm": "코스피 200", "basDt": "20261127", "clpr": "758.51"}
        return HttpResponse(200, json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"totalCount": 1, "items": {"item": [row]}}}}).encode())
    result = collect(path, env={"KRX_OPENAPI_KEY": "bad", "DATA_GO_KR_SERVICE_KEY": "good"}, fetch=fetch)
    assert result.source == "fsc_openapi"
    assert result.rows_inserted == 1
    assert len(calls) == 3
    assert result.requests == 3
    assert load_benchmark_history(path, "KOSPI200", derived_only=True).empty


def test_fsc_uses_response_page_size_for_pagination(tmp_path):
    path = tmp_path / "private.sqlite"
    pages = []
    def fetch(request, timeout):
        page = int(parse_qs(urlparse(request.full_url).query)["pageNo"][0])
        pages.append(page)
        row = {"idxNm": "코스피 200", "basDt": "20261127" if page == 1 else "20261126", "clpr": "758.51"}
        return HttpResponse(200, json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"numOfRows": 1, "totalCount": 2, "items": {"item": [row]}}}}).encode())
    result = collect(path, source="fsc_openapi", env={"DATA_GO_KR_SERVICE_KEY": "test"}, start_date="2026-11-26", fetch=fetch)
    assert pages == [1, 2]
    assert result.rows_inserted == 2

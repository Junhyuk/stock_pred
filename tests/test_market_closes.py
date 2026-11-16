from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from roboquant.index_forecast.market_closes import (
    collect_market_closes,
    effective_index_eod_frame,
    record_observation,
)
from roboquant.index_forecast.sqlite_store import connect_index_forecast

KST = ZoneInfo('Asia/Seoul')
NOW = datetime(2026, 11, 16, 18, 0, tzinfo=KST)
SESSIONS = ['2026-11-13', '2026-11-16', '2026-11-17']


def bar(day, close, opening, high, low):
    return {'localTradedAt': day, 'closePrice': close, 'openPrice': opening,
            'highPrice': high, 'lowPrice': low}


def test_naver_fallback_collects_completed_closes_without_inventing_publication_time(tmp_path):
    data = {
        'KOSPI': [bar('2026-11-16', '5,100.50', '5,305.40', '5,327.99', '5,069.83'),
                  bar('2026-11-13', '5,290.53', '5,452.41', '5,504.44', '5,259.09')],
        'KOSDAQ': [bar('2026-11-16', '665.65', '690', '700', '660'),
                   bar('2026-11-13', '699.66', '700', '710', '690')],
    }
    result = collect_market_closes(tmp_path / 'db.sqlite', now=NOW, fetch_naver=lambda market: data[market],
                                   fetch_yahoo=lambda market: [], sessions=SESSIONS)
    for market, expected in [('KOSPI', 5100.50), ('KOSDAQ', 665.65)]:
        item = result['markets'][market]
        assert item['status'] == 'single_source'
        assert item['session_date'] == '2026-11-16'
        assert item['close'] == expected
        assert item['published_at'] is None
        assert item['received_at'] == NOW.isoformat()
    conn = connect_index_forecast(tmp_path / 'db.sqlite')
    assert conn.execute('select count(*) from index_eod').fetchone()[0] == 4
    conn.close()


def test_conflict_blocks_current_market_and_does_not_replace_original(tmp_path):
    db = tmp_path / 'db.sqlite'
    conn = connect_index_forecast(db)
    conn.execute("insert into index_eod(index_code,session_date,close,source,ingested_at) values('KOSPI','2026-11-16',5000,'first','2026-11-16T06:40:00+00:00')")
    conn.commit()
    conn.close()
    result = collect_market_closes(db, now=NOW,
        fetch_naver=lambda market: [bar('2026-11-16', '5100.50', '5100', '5200', '5000')],
        fetch_yahoo=lambda market: [{'session_date': '2026-11-16', 'close': 5101.0}], sessions=SESSIONS)
    assert result['markets']['KOSPI']['status'] == 'conflict'
    assert result['markets']['KOSPI']['close'] is None
    assert effective_index_eod_frame(db, cutoff=NOW).query("index_code == 'KOSPI'").empty
    conn = connect_index_forecast(db)
    assert conn.execute("select close from index_eod where index_code='KOSPI'").fetchone()[0] == 5000
    conn.close()


def test_late_revision_never_changes_historical_training_rows(tmp_path):
    db = tmp_path / 'db.sqlite'
    conn = connect_index_forecast(db)
    conn.execute("insert into index_eod(index_code,session_date,close,source,ingested_at) values('KOSPI','2026-11-13',5290.53,'first','2026-11-13T07:00:00+00:00')")
    conn.commit()
    record_observation(conn, {'index_code': 'KOSPI', 'session_date': '2026-11-13', 'close': 5300.0,
                              'source': 'revision', 'received_at': NOW.isoformat(), 'status': 'single_source'})
    conn.close()
    before = effective_index_eod_frame(db, cutoff='2026-11-13T09:00:00+00:00')
    after = effective_index_eod_frame(db, cutoff=NOW)
    assert before.iloc[0]['close'] == pytest.approx(5290.53)
    assert after.iloc[0]['close'] == pytest.approx(5290.53)


def test_yahoo_close_is_used_when_naver_unavailable_and_stale_prior_is_preserved(tmp_path):
    db = tmp_path / 'db.sqlite'
    result = collect_market_closes(db, now=NOW, fetch_naver=lambda market: [],
        fetch_yahoo=lambda market: [{'session_date': '2026-11-16', 'close': 5100.5}], sessions=SESSIONS)
    assert result['markets']['KOSPI']['status'] == 'single_source'
    assert result['markets']['KOSPI']['close'] == 5100.5
    assert 'Yahoo' in result['markets']['KOSPI']['source']
    failed = collect_market_closes(db, now=NOW, fetch_naver=lambda market: [],
        fetch_yahoo=lambda market: [], sessions=SESSIONS)
    assert failed['markets']['KOSPI']['status'] == 'stale'
    assert failed['markets']['KOSPI']['session_date'] == '2026-11-16'
    assert failed['markets']['KOSPI']['close'] == 5100.5


def test_revision_is_available_to_live_windows_but_not_retroactive_training(tmp_path):
    from roboquant.index_forecast.market_closes import apply_live_revisions
    db = tmp_path / 'db.sqlite'
    conn = connect_index_forecast(db)
    conn.execute("insert into index_eod(index_code,session_date,close,source,ingested_at) values('KOSPI','2026-11-13',5290.53,'first','2026-11-13T07:00:00+00:00')")
    conn.commit()
    record_observation(conn, {'index_code': 'KOSPI', 'session_date': '2026-11-13', 'close': 5300.0,
                              'source': 'revision', 'received_at': NOW.isoformat(), 'status': 'single_source'})
    conn.close()
    before = apply_live_revisions(effective_index_eod_frame(db, cutoff='2026-11-13T09:00:00Z'))
    panel = effective_index_eod_frame(db, cutoff='2026-11-17T09:00:00Z')
    live = apply_live_revisions(panel)
    assert before.iloc[0]['close'] == 5290.53
    assert panel.iloc[0]['close'] == 5290.53
    assert live.iloc[0]['close'] == 5300.0


def test_close_receipt_authenticates_exact_payload(tmp_path):
    import hashlib
    import json

    from roboquant.index_forecast.market_closes import write_market_closes
    payload = {'schema_version': 'market_closes_v1', 'run_id': 'run-1', 'markets': {}}
    output = tmp_path / 'market_closes.json'
    receipt = write_market_closes(payload, output, published_at=NOW)
    stored = json.loads((tmp_path / 'market_closes_publication.json').read_text())
    assert stored == receipt
    assert stored['run_id'] == 'run-1'
    assert stored['payload_checksums']['market_closes.json'] == hashlib.sha256(output.read_bytes()).hexdigest()


def test_failed_collection_preserves_verified_previous_json_even_without_database(tmp_path):
    from roboquant.index_forecast.market_closes import preserve_previous_closes, write_market_closes
    old = {'schema_version': 'market_closes_v1', 'run_id': 'old', 'markets': {
        'KOSPI': {'session_date': '2026-11-13', 'close': 5290.53, 'source': 'vendor',
                  'received_at': '2026-11-13T08:00:00+00:00', 'status': 'verified'}}}
    output = tmp_path / 'market_closes.json'
    write_market_closes(old, output, published_at=NOW)
    new = {'schema_version': 'market_closes_v1', 'run_id': 'new', 'markets': {
        'KOSPI': {'session_date': None, 'close': None, 'status': 'unavailable'}}}
    merged = preserve_previous_closes(new, output)
    assert merged['markets']['KOSPI']['session_date'] == '2026-11-13'
    assert merged['markets']['KOSPI']['close'] == 5290.53
    assert merged['markets']['KOSPI']['status'] == 'stale'
    output.write_text('tampered')
    assert preserve_previous_closes(new, output)['markets']['KOSPI']['close'] is None


def test_publication_clock_is_read_after_payload_is_written(tmp_path):
    from roboquant.index_forecast.market_closes import write_market_closes

    output = tmp_path / 'market_closes.json'
    def checked_at():
        assert output.exists()
        return NOW
    receipt = write_market_closes({'run_id': 'x'}, output, published_at=checked_at)
    assert receipt['publication_checked_at'] == NOW.isoformat()


def test_conflicting_latest_close_is_excluded_on_holiday_and_next_day(tmp_path):
    from roboquant.index_forecast.market_closes import apply_live_revisions
    db = tmp_path / 'db.sqlite'
    conn = connect_index_forecast(db)
    conn.execute("insert into index_eod(index_code,session_date,close,source,ingested_at) values('KOSPI','2026-11-13',5000,'first','2026-11-13T07:00:00Z')")
    conn.execute("insert into index_eod(index_code,session_date,close,source,ingested_at) values('KOSDAQ','2026-11-13',700,'first','2026-11-13T07:00:00Z')")
    conn.commit()
    record_observation(conn, {'index_code':'KOSPI', 'session_date':'2026-11-13', 'close':5100,
                              'source':'vendor', 'received_at':NOW.isoformat(), 'status':'conflict'})
    conn.close()
    for cutoff in (NOW, '2026-11-17T00:00:00Z'):
        frame = effective_index_eod_frame(db, cutoff=cutoff)
        live = apply_live_revisions(frame)
        assert live.query("index_code == 'KOSPI'").empty
        assert live.query("index_code == 'KOSDAQ'").iloc[0]['close'] == 700


def test_historical_conflict_blocks_live_rolling_window_without_rewriting_training(tmp_path):
    import pandas as pd

    from roboquant.index_forecast.market_closes import apply_live_revisions
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame
    db = tmp_path / 'db.sqlite'
    dates = pd.bdate_range('2026-10-01', '2026-11-16').strftime('%Y-%m-%d').tolist()
    conn = connect_index_forecast(db)
    conn.executemany('insert into index_eod(index_code,session_date,close,source,ingested_at) values(?,?,?,?,?)',
                     [('KOSPI', day, 100 + i, 'first', '2026-10-01T00:00:00Z') for i,day in enumerate(dates)])
    conn.commit()
    record_observation(conn, {'index_code':'KOSPI', 'session_date':dates[-10], 'close':200,
                              'source':'vendor', 'received_at':NOW.isoformat(), 'status':'conflict'})
    conn.close()
    panel = effective_index_eod_frame(db, cutoff=NOW)
    live = apply_live_revisions(panel)
    assert panel.query('session_date == @dates[-10]').iloc[0]['close'] == 100 + len(dates) - 10
    assert pd.isna(live.query('session_date == @dates[-10]').iloc[0]['close'])
    context = {'stage':'postclose', 'asof_date':dates[-1], 'prediction_cutoff':NOW.isoformat()}
    live_features = build_reliable_price_frame(live, sessions=dates + ['2026-11-17'], forecast_context=context)
    assert pd.isna(live_features.iloc[-1]['volatility_20d'])


@pytest.mark.parametrize('first_status', ['verified', 'single_source'])
def test_unresolved_conflict_survives_outage(tmp_path, first_status):
    db = tmp_path / 'db.sqlite'
    def naver(_):
        return [bar('2026-11-16', '5100', '5100', '5200', '5000')]
    def initial_yahoo(_):
        return [{'session_date':'2026-11-16', 'close':5100}] if first_status == 'verified' else []
    assert collect_market_closes(db, now=NOW, fetch_naver=naver, fetch_yahoo=initial_yahoo, sessions=SESSIONS)['markets']['KOSPI']['status'] == first_status
    def conflict(_):
        return [{'session_date':'2026-11-16', 'close':5200}]
    assert collect_market_closes(db, now=NOW, fetch_naver=naver, fetch_yahoo=conflict, sessions=SESSIONS)['markets']['KOSPI']['status'] == 'conflict'
    failed = collect_market_closes(db, now=NOW, fetch_naver=lambda _: [], fetch_yahoo=lambda _: [], sessions=SESSIONS)
    assert failed['markets']['KOSPI']['status'] == 'conflict'
    assert failed['markets']['KOSPI']['close'] is None


def test_bad_vendor_date_does_not_discard_valid_rows_or_other_market(tmp_path):
    rows = [bar('2026-11-00', '5100','5100','5200','5000'),
            bar('2026-11-16', '5100','5100','5200','5000')]
    result = collect_market_closes(tmp_path/'db', now=NOW, fetch_naver=lambda _: rows,
                                   fetch_yahoo=lambda _: [], sessions=None)
    assert result['markets']['KOSPI']['close'] == 5100
    assert result['markets']['KOSDAQ']['close'] == 5100


def test_bad_vendor_date_allows_yahoo_fallback(tmp_path):
    result = collect_market_closes(tmp_path/'db', now=NOW,
        fetch_naver=lambda _: [bar('2026-11-00', '5100','5100','5200','5000')],
        fetch_yahoo=lambda _: [{'session_date':'2026-11-16','close':5100}], sessions=None)
    assert result['markets']['KOSPI']['close'] == 5100
    assert 'Yahoo' in result['markets']['KOSPI']['source']


def test_transient_naver_error_gets_one_retry_without_retrying_bad_rows(tmp_path):
    calls = {'KOSPI':0, 'KOSDAQ':0}
    def fetch(market):
        calls[market] += 1
        if market == 'KOSPI' and calls[market] == 1:
            raise OSError('temporary')
        return [bar('2026-11-16' if market == 'KOSPI' else 'bad', '5100','5100','5200','5000')]
    result = collect_market_closes(tmp_path/'db', now=NOW, fetch_naver=fetch,
                                   fetch_yahoo=lambda _: [], sessions=SESSIONS)
    assert calls == {'KOSPI':2, 'KOSDAQ':1}
    assert result['markets']['KOSPI']['close'] == 5100
    assert result['markets']['KOSDAQ']['status'] == 'unavailable'


def test_one_vendor_cannot_resolve_previously_disputed_close(tmp_path):
    db = tmp_path / 'db.sqlite'
    def naver(_):
        return [bar('2026-11-16','5100','5100','5200','5000')]
    collect_market_closes(db, now=NOW, fetch_naver=naver,
        fetch_yahoo=lambda _: [{'session_date':'2026-11-16','close':5200}], sessions=SESSIONS)
    result = collect_market_closes(db, now=NOW, fetch_naver=naver, fetch_yahoo=lambda _: [], sessions=SESSIONS)
    assert result['markets']['KOSPI']['status'] == 'conflict'
    assert result['markets']['KOSPI']['close'] is None


def test_unresolved_prior_receipt_conflict_survives_database_loss(tmp_path):
    from roboquant.index_forecast.market_closes import preserve_previous_closes, write_market_closes
    prior = {'schema_version':'market_closes_v1','run_id':'old','markets':{
        'KOSPI':{'session_date':'2026-11-16','status':'conflict','close':None,'source':'vendor','received_at':NOW.isoformat()}}}
    output = tmp_path/'market_closes.json'
    write_market_closes(prior, output, published_at=NOW)
    fresh = {'schema_version':'market_closes_v1','run_id':'new','markets':{
        'KOSPI':{'session_date':None,'status':'unavailable','close':None}}}
    assert preserve_previous_closes(fresh, output)['markets']['KOSPI']['status'] == 'conflict'

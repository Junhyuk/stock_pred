import json
import math
import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from scripts import collect_market_chart_history as charts


def test_yahoo_uses_provider_local_day_and_excludes_incomplete_quote():
    stamps = [int(datetime(2026, 11, day, hour, tzinfo=UTC).timestamp())
              for day, hour in ((23, 14), (24, 14), (25, 1))]
    payload = {'chart': {'error': None, 'result': [{
        'meta': {'symbol': 'ES=F', 'currency': 'USD', 'instrumentType': 'FUTURE',
                 'dataGranularity': '1d', 'exchangeTimezoneName': 'America/New_York',
                 'currentTradingPeriod': {'regular': {'end': stamps[-1] + 3600 * 20}}},
        'timestamp': stamps, 'indicators': {'quote': [{'close': [7000, 7030, 7100]}]},
    }]}}
    assert charts.yahoo_points('es_futures', payload, datetime(2026, 11, 25, 1, tzinfo=UTC)) == {
        '2026-11-23': 7000,
    }
    payload['chart']['result'][0]['meta']['symbol'] = 'NQ=F'
    with pytest.raises(ValueError, match='identity'):
        charts.yahoo_points('es_futures', payload, datetime(2026, 11, 25, 1, tzinfo=UTC))


def test_basis_pairs_only_same_day_closes():
    spot = {'2026-11-20': 760.5, '2026-11-23': 762, '2026-11-24': 760}
    futures = {'2026-11-19': 760, '2026-11-20': 761, '2026-11-23': 761.4}
    assert charts.basis_points(spot, futures) == {'2026-11-20': 0.5, '2026-11-23': -0.6}


def test_oil_contract_can_settle_below_zero():
    stamp = int(datetime(2020, 4, 20, 20, tzinfo=UTC).timestamp())
    chart = {'chart': {'error': None, 'result': [{
        'meta': {'symbol': 'CL=F', 'currency': 'USD', 'instrumentType': 'FUTURE',
                 'dataGranularity': '1d', 'exchangeTimezoneName': 'America/New_York'},
        'timestamp': [stamp], 'indicators': {'quote': [{'close': [-37.63]}]},
    }]}}
    assert charts.yahoo_points('wti', chart, datetime(2020, 4, 21, 15, tzinfo=UTC)) == {
        '2020-04-20': -37.63,
    }


def test_realized_volatility_breaks_at_a_missing_trading_session():
    sessions = [f'2026-11-{day:02}' for day in range(1, 26)]
    close = {day: 100 + 0.5 * i for i, day in enumerate(sessions) if i != 2}
    returns, volatility = charts.domestic_derived(close, sessions)
    assert sessions[3] not in returns
    assert returns[sessions[4]] == pytest.approx((102 / 101.5 - 1) * 100)
    assert list(volatility) == sessions[23:]
    assert all(math.isfinite(value) for value in volatility.values())


def test_reference_month_transform_matches_prior_year_and_skips_unpaired_months():
    prices = {'2025-01-01': 120, '2025-03-01': 140, '2026-01-01': 126, '2026-02-01': 130}
    assert charts.year_over_year(prices, as_percent=True) == {'2026-01-01': 5.0}
    assert charts.year_over_year(prices, as_percent=False) == {'2026-01-01': 6.0}


def test_release_dates_require_matching_measure_value_and_adjustment(tmp_path):
    path = tmp_path / 'economic_history.json'
    observations = {'2026-10-01': 2.8, '2026-09-01': 2.6, '2026-08-01': 2.5}
    reference = {'country': 'KR', 'measure': 'cpi_headline_yoy', 'unit': 'percent',
                 'reference_period': '2026-10', 'seasonal_adjustment': 'NSA',
                 'source_id': 'kr_mods_cpi_latest_release', 'value': 2.8,
                 'actual_release_date': '2026-11-03', 'available_at': '2026-11-04T01:00:00+00:00',
                 'source_url': 'https://mods.go.kr/release'}
    path.write_text(json.dumps({'schema_version': 'economic_history_v1', 'release_versions': [
        reference, {**reference, 'reference_period': '2026-09', 'value': 2.7,
                    'actual_release_date': '2026-10-03'},
        {**reference, 'reference_period': '2026-08', 'value': 2.5, 'seasonal_adjustment': 'SA',
         'actual_release_date': '2026-09-03'},
    ]}))
    actual = charts.release_metadata(path, {'kr_inflation': observations}, datetime(2026, 11, 9, tzinfo=UTC))
    assert actual['kr_inflation']['2026-10-01'] == ('2026-11-03', 'https://mods.go.kr/release')
    assert len(actual['kr_inflation']) == 1


def test_export_preserves_seed_on_download_failure_and_reads_private_data_without_writing(tmp_path):
    db_path = tmp_path / 'private.sqlite'
    with sqlite3.connect(db_path) as db:
        db.execute('CREATE TABLE macro_series(series_code, observation_date, value, source, ingested_at, revision_no)')
        db.executemany('INSERT INTO macro_series VALUES (?, ?, ?, ?, ?, ?)', [
            ('KR_KTB_1Y', '2026-11-24', 3.5, 'bok_ecos_817Y002_sample', '2026-11-24T10:00:00+00:00', 0),
            ('KR_KTB_1Y', '2026-11-25', 3.6, 'unknown', '2026-11-25T10:00:00+00:00', 0),
            ('US_TREASURY_1Y', '2026-11-25', 4.1, 'us_treasury_daily_par_yield_curve', '2026-11-29T10:00:00+00:00', 0),
        ])
    original = db_path.read_bytes()
    public = tmp_path / "public"
    public.mkdir()
    seed = public / "seed.json"
    wanted = [['2025-12-01', 68.5], ['2026-11-23', 90.0]]
    seed.write_text(json.dumps({'schema_version': charts.SCHEMA, 'series': {
        'brent': {**charts.SPECS['brent'].metadata(), 'points': wanted + [['2026-11-25', 'NaN']]},
    }}))

    def offline(url):
        raise OSError('offline')

    output = public / "export.json"
    result = charts.export_history(db_path, output, seed=seed, public_dir=public,
                                   as_of=datetime(2026, 11, 28, 12, tzinfo=UTC), fetch=offline)
    assert result == json.loads(output.read_text())
    assert result['series']['brent']['points'] == wanted
    assert result['series']['kr_1y']['points'] == [['2026-11-24', 3.5]]
    assert result['series']['us_1y']['points'] == []
    assert db_path.read_bytes() == original


def test_naver_close_parser_checks_identity_and_drops_after_cutoff():
    raw = b'''<?xml version="1.0" encoding="EUC-KR"?>
    <protocol><chartdata symbol="FUT" timeframe="day">
    <item data="20261120|500|501|499|500.50|107"/>
    <item data="20261123|502|502|499|501.30|190"/>
    </chartdata></protocol>'''
    assert charts.naver_points('FUT', raw, date(2026, 11, 20)) == {'2026-11-20': 500.5}
    with pytest.raises(ValueError, match='identity'):
        charts.naver_points('KPI200', raw, date(2026, 11, 23))


def test_bls_archive_uses_published_reference_month_and_ignores_commented_future_release():
    raw = b'''
    <!-- <a href="/news.release/archives/empsit_12042026.htm">November 2026 Employment Situation</a> -->
    <a href="/news.release/archives/empsit_11062026.htm">October 2026 Employment Situation</a>
    <a href="/news.release/archives/empsit_10022026.htm">September 2026 Employment Situation</a>
    <a href="/news.release/archives/empsit_10022026.pdf">September 2026 Employment Situation PDF</a>
    '''
    assert charts.bls_release_dates('empsit', raw, date(2026, 11, 28)) == {
        '2026-10-01': ('2026-11-06', 'https://www.bls.gov/news.release/archives/empsit_11062026.htm'),
        '2026-09-01': ('2026-10-02', 'https://www.bls.gov/news.release/archives/empsit_10022026.htm'),
    }


def test_cache_appends_revisions_without_repeating_identical_observations(tmp_path):
    db_path = tmp_path / 'private.sqlite'
    row = {**charts.SPECS['brent'].metadata(), 'points': [['2026-11-20', 90.0]]}
    first = datetime(2026, 11, 21, 0, tzinfo=UTC)
    rerun = datetime(2026, 11, 21, 1, tzinfo=UTC)
    revision = datetime(2026, 11, 21, 2, tzinfo=UTC)
    charts.persist(db_path, {'brent': row}, first)
    charts.persist(db_path, {'brent': row}, rerun)
    charts.persist(db_path, {'brent': {**row, 'points': [['2026-11-20', 90.01]]}}, revision)
    with sqlite3.connect(db_path) as db:
        assert db.execute('SELECT value, received_at FROM market_chart_observation ORDER BY received_at').fetchall() == [
            (90, first.isoformat()), (90.01, revision.isoformat()),
        ]
    assert charts.read_sqlite(db_path, rerun, date(2026, 11, 1)) == {'brent': {'2026-11-20': 90}}


def test_failed_refresh_keeps_an_older_valid_chart_until_new_data_arrives(tmp_path):
    output = tmp_path / 'public' / 'history.json'
    output.parent.mkdir()
    row = {**charts.SPECS['brent'].metadata(), 'points': [['2024-11-20', 90.0]]}
    output.write_text(json.dumps({'schema_version': charts.SCHEMA, 'series': {'brent': row}}))

    def offline(url):
        raise OSError('offline')

    data = charts.export_history(tmp_path / 'private.sqlite', output, fetch=offline,
                                 as_of=datetime(2026, 11, 28, 12, tzinfo=UTC))
    assert data['series']['brent']['points'] == row['points']


def test_ecos_daily_request_reads_multiple_full_pages():
    first = date(2024, 1, 1)
    days = [first + timedelta(days=offset) for offset in range(741)]

    def fetch(url):
        _, _, _, _, first_row, last_row, table, _, _, _, item = url.split('/')[-11:]
        return json.dumps({'StatisticSearch': {'list_total_count': len(days), 'row': [
            {'STAT_CODE': table, 'ITEM_CODE1': item, 'ITEM_CODE2': None, 'UNIT_NAME': '연%',
             'TIME': day.strftime('%Y%m%d'), 'DATA_VALUE': '3.5'}
            for day in days[int(first_row) - 1:int(last_row)]
        ]}}).encode()

    actual = charts.fetch_ecos('817Y002', '010200001', None, '연%', 'D', first, days[-1], 'key', fetch)
    assert (len(actual), actual['2024-01-01'], actual['2026-01-10']) == (741, 3.5, 3.5)


def test_treasury_5_year_is_read_from_its_own_official_xml_element():
    raw = b'''<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices">
      <entry><content><m:properties><d:NEW_DATE>2026-11-27T00:00:00</d:NEW_DATE>
      <d:BC_1YEAR>4.61</d:BC_1YEAR><d:BC_3YEAR>4.72</d:BC_3YEAR>
      <d:BC_5YEAR>4.80</d:BC_5YEAR><d:BC_10YEAR>4.94</d:BC_10YEAR>
      </m:properties></content></entry></feed>'''
    actual = charts.fetch_us_yields(2026, date(2026, 11, 28), lambda _url: raw)
    assert actual.get('us_5y') == {'2026-11-27': 4.80}


def test_crypto_has_weekend_observations_and_excludes_unfinished_utc_day():
    stamps = [int(datetime(2026, 11, day, tzinfo=UTC).timestamp()) for day in (21, 22, 23)]
    payload = {'chart': {'error': None, 'result': [{
        'meta': {'symbol': 'BTC-USD', 'currency': 'USD', 'instrumentType': 'CRYPTOCURRENCY',
                 'dataGranularity': '1d', 'exchangeTimezoneName': 'UTC',
                 'currentTradingPeriod': {'regular': {'end': stamps[-1] + 3600}}},
        'timestamp': stamps, 'indicators': {'quote': [{'close': [90_000, 91_000, 92_000]}]},
    }]}}
    assert charts.fetch_yahoo('btc', datetime(2026, 11, 23, 12, tzinfo=UTC),
                              lambda _url: json.dumps(payload).encode()) == {
        '2026-11-21': 90_000, '2026-11-22': 91_000,
    }


def test_refresh_keeps_three_year_fx_but_uses_legacy_window_for_other_prices(tmp_path):
    rows = {}
    for key in ('dxy', 'gold'):
        rows[key] = {**charts.SPECS[key].metadata(), 'points': [
            ['2023-11-27', 100], ['2023-11-28', 101], ['2025-11-28', 102],
        ]}
    seed = tmp_path / 'seed.json'
    seed.write_text(json.dumps({'schema_version': charts.SCHEMA, 'series': rows}))

    def fetch(url):
        if '/DX-Y.NYB?' in url:
            symbol, kind = 'DX-Y.NYB', 'INDEX'
        elif '/GC%3DF?' in url:
            symbol, kind = 'GC=F', 'FUTURE'
        else:
            raise OSError('offline')
        return json.dumps({'chart': {'error': None, 'result': [{
            'meta': {'symbol': symbol, 'currency': 'USD', 'instrumentType': kind,
                     'dataGranularity': '1d', 'exchangeTimezoneName': 'America/New_York'},
            'timestamp': [int(datetime(2026, 11, 27, 15, tzinfo=UTC).timestamp())],
            'indicators': {'quote': [{'close': [103]}]},
        }]}}).encode()

    result = charts.export_history(tmp_path / 'private.sqlite', tmp_path / 'public' / 'out.json',
                                   seed=seed, as_of=datetime(2026, 11, 28, 12, tzinfo=UTC), fetch=fetch)
    rows = result['series']
    assert result.get('as_of_date') == '2026-11-28'
    assert rows['dxy']['points'] == [['2023-11-28', 101], ['2025-11-28', 102], ['2026-11-27', 103]]
    assert rows['dxy']['coverage'] == {'rows': 3, 'first': '2023-11-28', 'last': '2026-11-27'}
    assert rows['gold']['points'] == [['2025-11-28', 102], ['2026-11-27', 103]]


def test_jgb_csv_carries_yield_sign_and_excludes_unpublished_rows():
    raw = b'''Interest Rate,,,,(Unit : %)
Date,5Y,3Y,1Y,10Y
2023/11/28,0.355,-0.075,-0.120,0.761
2026/11/27,2.571,1.550,1.254,3.026
'''
    actual = charts.japan_points(raw, date(2023, 11, 28), date(2026, 11, 26))
    assert actual == {
        'jp_1y': {'2023-11-28': -0.120}, 'jp_3y': {'2023-11-28': -0.075},
        'jp_5y': {'2023-11-28': 0.355}, 'jp_10y': {'2023-11-28': 0.761},
    }


def test_adjacent_month_delta_skips_gaps_and_crosses_year():
    values = {'2025-09-01': 120, '2025-11-01': 121, '2025-12-01': 119, '2026-01-01': 123}
    assert charts.month_over_month(values) == {'2025-12-01': -2, '2026-01-01': 4}


def test_mods_archive_connects_the_misspelled_july_2024_cpi_listing():
    raw = b'''<a class="board_link" href="javascript:addSearchParam('/board.es?act=view&amp;bid=11751&amp;list_no=432068');">
    Cousumer Price Index in July 2024</a><strong>Date</strong><span>2024-08-02</span>'''
    assert charts.fetch_mods_releases('mods_cpi', 3, date(2026, 11, 28), lambda _url: raw) == {
        '2024-07-01': ('2024-08-02', 'https://mods.go.kr/board.es?act=view&bid=11751&list_no=432068'),
    }


def test_establishment_payroll_uses_the_combined_shutdown_release(tmp_path):
    def fetch(url):
        if url.endswith('/bls/news-release/empsit.htm'):
            return b'<a href="/news.release/archives/empsit_12162025.htm">November 2025 Employment Situation</a>'
        if '/CES0000000001?' in url:
            return json.dumps({'status': 'REQUEST_SUCCEEDED', 'Results': {'series': [{
                'seriesID': 'CES0000000001', 'data': [
                    {'year': '2025', 'period': 'M09', 'value': '100'},
                    {'year': '2025', 'period': 'M10', 'value': '99'},
                    {'year': '2025', 'period': 'M11', 'value': '101'},
                ],
            }]}}).encode()
        raise OSError('offline')

    result = charts.export_history(tmp_path / 'private.sqlite', tmp_path / 'public' / 'out.json',
                                   as_of=datetime(2026, 11, 28, 12, tzinfo=UTC), fetch=fetch)['series']
    for key in ('us_employment', 'us_employment_change'):
        assert result[key]['release_dates']['2025-10-01'] == '2025-12-16'
        assert result[key]['release_urls']['2025-10-01'].endswith('/empsit_12162025.htm')
    assert result['us_unemployment']['points'] == []


def test_policy_anchor_and_verified_through_survive_failed_refresh(tmp_path):
    path = tmp_path / 'public' / 'out.json'
    private = tmp_path / 'private.sqlite'

    def fetch(url):
        if url == charts.BOK_RATE:
            return '''<table><tr><th>변경일자</th></tr>
            <tr><td>2026</td><td>11월 26일</td><td>3.25</td></tr>
            <tr><td>2023</td><td>1월 13일</td><td>3.50</td></tr>
            <tr><td>2022</td><td>11월 24일</td><td>3.25</td></tr></table>'''.encode()
        raise OSError('offline')

    def offline(_url):
        raise OSError('offline')

    first = charts.export_history(private, path, as_of=datetime(2026, 11, 28, 12, tzinfo=UTC), fetch=fetch)
    second = charts.export_history(private, path, as_of=datetime(2026, 11, 29, 12, tzinfo=UTC), fetch=offline)
    for result in (first, second):
        policy = result['series']['kr_policy_rate']
        assert policy['points'] == [['2023-01-13', 3.5], ['2026-11-26', 3.25]]
        assert policy.get('verified_through') == '2026-11-28'


def test_daily_refresh_uses_the_recent_overlap_only_after_backfill_is_complete():
    first = date(2023, 11, 28)
    days = [(first + timedelta(days=offset)).isoformat() for offset in range(1096)]
    full = dict.fromkeys(days, 100)
    assert charts.daily_refresh_start(full, first, every_day=True) == date(2026, 11, 13)
    for day in days[250:350]:
        del full[day]
    assert charts.daily_refresh_start(full, first, every_day=True) == first

import json
import math
import sqlite3
from datetime import UTC, date, datetime

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

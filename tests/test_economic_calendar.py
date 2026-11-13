"""Immutable official facts, cutoff clocks and first-release numeric inputs."""
import importlib

import pytest


def engine():
    spec = importlib.util.find_spec('roboquant.index_forecast.economic_calendar')
    assert spec is not None, 'economic calendar store is missing'
    return importlib.import_module('roboquant.index_forecast.economic_calendar')


def event(**changes):
    return {'event_id': 'us-cpi-2026-10', 'country': 'US', 'event_type': 'inflation',
            'title': 'Consumer Price Index', 'scheduled_at': '2026-11-10T08:30:00-05:00',
            'scheduled_date': '2026-11-10', 'timezone': 'America/New_York',
            'time_precision': 'datetime', 'status': 'confirmed', 'published_at': None,
            'first_seen_at': '2026-11-01T00:00:00Z', 'reference_period': '2026-10',
            'source_id': 'bls', 'source_url': 'https://www.bls.gov/schedule/',
            'source_hash': 'source-fixture-v1', **changes}


def release(**changes):
    return {'event_id': 'us-cpi-2026-10', 'country': 'US', 'measure': 'cpi_headline_yoy',
            'value': 3.2, 'unit': 'percent', 'reference_period': '2026-10',
            'actual_release_at': '2026-11-10T08:30:00-05:00',
            'first_seen_at': '2026-11-10T13:31:00Z', 'vintage': 'first_release',
            'seasonal_adjustment': 'NSA', 'source_id': 'bls',
            'definition_version': 'BLS:test-series:NSA:YoY:v1',
            'source_url': 'https://www.bls.gov/news.release/cpi.htm',
            'source_hash': 'release-fixture-v1', 'revision_of': None, **changes}


def test_schedule_revision_is_append_only_and_asof_does_not_see_later_postponement(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    first = {'events': [event()], 'releases': [], 'sources': []}
    store.ingest(first, collected_at='2026-11-01T00:00:00Z')
    store.ingest(first, collected_at='2026-11-02T00:00:00Z')
    changed = event(scheduled_at='2026-11-12T08:30:00-05:00', scheduled_date='2026-11-12',
                    first_seen_at='2026-11-05T00:00:00Z', status='postponed', source_hash='changed')
    store.ingest({'events': [changed], 'releases': [], 'sources': []}, collected_at='2026-11-05T00:00:00Z')
    assert store.events_asof('2026-11-03T00:00:00Z')[0]['scheduled_at'] == '2026-11-10T13:30:00+00:00'
    assert store.events_asof('2026-11-06T00:00:00Z')[0]['scheduled_at'] == '2026-11-12T13:30:00+00:00'
    exported = store.export_history()
    assert len(exported['event_versions']) == 2
    assert exported['event_versions'][0]['timezone'] == 'America/New_York'


def test_date_only_event_never_acquires_a_fabricated_release_hour(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    store.ingest({'events': [event(event_id='boj', country='JP', event_type='policy_rate',
        scheduled_at=None, scheduled_date='2026-11-10', timezone='Asia/Tokyo', time_precision='date')],
        'releases': [], 'sources': []}, collected_at='2026-11-01T00:00:00Z')
    rows = store.events_asof('2026-11-02T00:00:00Z')
    assert rows[0]['scheduled_at'] is None
    context = store.forecast_context(asof_close='2026-11-09T06:30:00Z',
        cutoff='2026-11-09T23:00:00Z', target_close='2026-11-10T06:30:00Z')
    assert context['pending_or_unknown']
    assert context['event_bucket'] is None


def test_actual_arrival_after_cutoff_cannot_enter_numeric_inputs_and_revisions_remain_separate(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    older = release(event_id='us-cpi-2026-09', value=3.0, reference_period='2026-09',
        actual_release_at='2026-10-10T12:30:00Z', first_seen_at='2026-10-10T12:31:00Z')
    store.ingest({'events': [], 'releases': [older], 'sources': []}, collected_at='2026-10-10T12:31:00Z')
    store.ingest({'events': [], 'releases': [release()], 'sources': []}, collected_at='2026-11-10T13:31:00Z')
    before = store.numeric_features('2026-11-10T13:30:30Z', policy='observed_current_receipt_v1')
    assert before['features']['us_cpi_yoy_change_pp'] is None
    after = store.numeric_features('2026-11-10T13:32:00Z', policy='observed_current_receipt_v1')
    assert after['features']['us_cpi_yoy_change_pp'] == pytest.approx(.2)
    revision = release(value=9.0, vintage='revision', first_seen_at='2026-11-11T00:00:00Z',
                       revision_of='us-cpi-2026-10', source_hash='revision')
    store.ingest({'events': [], 'releases': [revision], 'sources': []}, collected_at='2026-11-11T00:00:00Z')
    assert store.numeric_features('2026-11-10T13:32:00Z', policy='observed_current_receipt_v1')['features']['us_cpi_yoy_change_pp'] == pytest.approx(.2)
    assert store.numeric_features('2026-11-12T00:00:00Z', policy='observed_current_receipt_v1')['features']['us_cpi_yoy_change_pp'] == pytest.approx(6.)
    assert store.numeric_features('2026-11-12T00:00:00Z')['features']['us_cpi_yoy_change_pp'] is None
    assert len(store.export_history()['release_versions']) == 3
    assert store.releases_asof('2026-11-12T00:00:00Z')[-1]['value'] == 9.0


def test_nfp_is_official_monthly_change_not_change_of_monthly_changes(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    payroll = release(measure='nfp_monthly_change', value=180, unit='thousand_persons', seasonal_adjustment='SA',
                      definition_version='BLS:CES0000000001:SA:monthly_change:v1')
    store.ingest({'events': [], 'releases': [payroll], 'sources': []}, collected_at='2026-11-10T14:00:00Z')
    result = store.numeric_features('2026-11-11T00:00:00Z', policy='observed_current_receipt_v1')
    assert result['features']['us_nfp_change_thousands'] == 180
    assert len(result['features']) == 14
    assert not result['ready']
    assert not any('surprise' in key for key in result['features'])


def test_wrong_units_or_adjustment_prevent_cross_vintage_difference(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    older = release(reference_period='2026-09', value=3.0, unit='index_points',
                    first_seen_at='2026-10-10T12:31:00Z', actual_release_at='2026-10-10T12:30:00Z')
    store.ingest({'events': [], 'releases': [older, release()], 'sources': []}, collected_at='2026-11-10T14:00:00Z')
    assert store.numeric_features('2026-11-11T00:00:00Z', policy='observed_current_receipt_v1')['features']['us_cpi_yoy_change_pp'] is None


def test_compact_durable_history_roundtrip_and_hash_tampering(tmp_path):
    store = engine().EconomicCalendarStore(tmp_path / 'one.sqlite')
    store.ingest({'events': [event()], 'releases': [release()], 'sources': []}, collected_at='2026-11-10T14:00:00Z')
    exported = store.export_history()
    restored = engine().EconomicCalendarStore(tmp_path / 'two.sqlite')
    restored.import_history(exported, trusted_archive=True)
    assert restored.events_asof('2026-11-11T00:00:00Z') == store.events_asof('2026-11-11T00:00:00Z')
    assert restored.releases_asof('2026-11-11T00:00:00Z') == store.releases_asof('2026-11-11T00:00:00Z')
    exported['release_versions'][0]['value'] = 999
    with pytest.raises(ValueError, match='hash'):
        restored.import_history(exported, trusted_archive=True)


def test_observed_current_policy_is_explicit_and_never_backdates_unknown_publication(tmp_path):
    import inspect
    store = engine().EconomicCalendarStore(tmp_path / 'calendar.sqlite')
    assert 'policy' in inspect.signature(store.numeric_features).parameters, 'explicit observed-current policy is missing'
    older = release(reference_period='2026-09', value=3., actual_release_at=None, vintage='current_snapshot')
    current = release(value=3.2, actual_release_at=None, vintage='current_snapshot')
    store.ingest({'events': [], 'releases': [older, current], 'sources': []}, collected_at='2026-11-10T14:00:00Z')
    assert store.numeric_features('2026-11-10T13:59:00Z', policy='observed_current_receipt_v1')['features']['us_cpi_yoy_change_pp'] is None
    result = store.numeric_features('2026-11-10T14:01:00Z', policy='observed_current_receipt_v1')
    assert result['features']['us_cpi_yoy_change_pp'] == pytest.approx(.2)
    assert result['feature_policy'] == 'observed_current_receipt_v1'
    assert store.numeric_features('2026-11-10T14:01:00Z')['features']['us_cpi_yoy_change_pp'] is None

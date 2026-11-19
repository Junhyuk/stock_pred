import hashlib
import json

import pandas as pd
import pytest

from roboquant.index_forecast.accuracy_audit import audit_recent_accuracy


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def fixture():
    sessions = pd.DataFrame([
        {'date': '2026-11-18', 'open_at': '2026-11-18T00:00:00Z', 'close_at': '2026-11-18T06:30:00Z'},
        {'date': '2026-11-19', 'open_at': '2026-11-19T01:00:00Z', 'close_at': '2026-11-19T07:30:00Z'},
    ])
    prices = pd.DataFrame([{'index_code': m, 'session_date': d, 'close': c}
        for m in ('KOSPI', 'KOSDAQ') for d, c in [('2026-11-18', 100.), ('2026-11-19', 102.)]])
    protocol = {'name': 'test'}
    ph = digest(protocol)
    f = {'market': 'KOSPI', 'asof_date': '2026-11-18', 'target_date': '2026-11-19',
         'role': 'primary', 'fit_provenance': 'live_captured', 'protocol_hash': ph, 'issued_at': '2026-11-18T09:00:00Z',
         'prediction_cutoff': '2026-11-18T08:00:00Z', 'target_close_at': '2026-11-19T06:30:00Z',
         'run_id': 'run', 'issued_baseline_close': 100., 'outcome_basis': 'issued_close_v1',
         'expected_return': .01, 'p_up': .6, 'range_low': -.01, 'range_high': .03}
    f['forecast_key'] = digest({k: f[k] for k in ('market', 'target_date', 'protocol_hash', 'role')})
    outcome = {'market': 'KOSPI', 'asof_date': '2026-11-18', 'target_date': '2026-11-19',
         'role': 'primary', 'protocol_hash': ph, 'forecast_key': f['forecast_key'],
         'target_close_at': '2026-11-19T06:30:00Z', 'target_observed_at': '2026-11-19T08:00:00Z',
         'label_available_at': '2026-11-19T06:30:00Z', 'observed_at': '2026-11-19T08:01:00Z',
         'issued_baseline_close': 100., 'target_close': 102., 'actual_return': .02,
         'outcome_basis': 'issued_close_v1', 'mature': True}
    data = {'schema_version': 'forward_history_v1', 'protocols': [{'protocol': protocol, 'protocol_hash': ph, 'registered_at': '2026-11-18T07:00:00Z'}],
            'forecasts': [f], 'outcomes': [outcome], 'outcome_attempts': [],
            'attempts': [{'run_id': 'run', 'protocol_hash': ph, 'status': 'verified', 'attempted_at': '2026-11-18T09:01:00Z', 'accepted_roles': ['KOSPI:primary']}]}
    archive = {'markets': [{'market': 'KOSPI', 'evaluation': {'observations': [{
        'asof_date': '2026-11-18', 'target_date': '2026-11-19', 'actual_return': .02,
        'prediction_cutoff': '2026-11-18T08:00:00Z', 'target_close_at': '2026-11-19T06:30:00Z',
        'expected_return': .01, 'p_up': .7,
        'label_available_at': '2026-11-19T06:30:00Z', 'baselines': {'legacy': {'expected_return': .01, 'p_up': .6}, 'mean': {'expected_return': .005}}}], 'candidate_observations': {}}}]}
    return prices, sessions, archive, {**data, 'history_hash': digest(data)}


def run(prices, sessions, archive, ledger, as_of='2026-11-19T09:00:00Z'):
    ledger['history_hash'] = digest({k: v for k, v in ledger.items() if k != 'history_hash'})
    return audit_recent_accuracy(prices, archive, ledger, sessions=sessions, as_of=pd.Timestamp(as_of), days=1)


def test_verified_late_clock_and_historical_price_metrics():
    prices, sessions, archive, ledger = fixture()
    result = run(prices, sessions, archive, ledger)
    m = result['markets']['KOSPI']
    assert m['cohorts'][0]['metrics']['n'] == 1
    assert m['cohorts'][0]['metrics']['price_mae_points'] == pytest.approx(1.)
    assert m['historical'][0]['metrics']['price_mae_points'] == pytest.approx(1.)
    assert m['daily'][0]['official_close_at'] == '2026-11-19T07:30:00+00:00'
    assert 'stored_target_close_mismatch' in m['cohorts'][0]['daily'][0]['flags']


def test_future_observations_and_before_official_close_excluded():
    prices, sessions, archive, ledger = fixture()
    ledger['outcomes'][0]['target_observed_at'] = '2026-11-19T07:00:00Z'
    result = run(prices, sessions, archive, ledger)
    assert result['markets']['KOSPI']['cohorts'][0]['metrics']['n'] == 0
    assert result['markets']['KOSPI']['cohorts'][0]['daily'][0]['status'] == 'target_observed_before_official_close'
    ledger['outcomes'][0]['target_observed_at'] = '2026-11-19T08:00:00Z'
    ledger['outcomes'][0]['observed_at'] = '2026-11-19T10:00:00Z'
    assert run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]['status'] == 'outcome_not_observed_as_of'


def test_duplicate_conflicts_and_cohorts_stay_separate():
    prices, sessions, archive, ledger = fixture()
    ledger['forecasts'].append(dict(ledger['forecasts'][0]))
    assert run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['metrics']['n'] == 1
    ledger['forecasts'][1]['expected_return'] = .5
    cohort = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert cohort['metrics']['n'] == 0
    assert cohort['daily'][0]['status'] == 'conflicting_forecast_key'


def test_prices_conflict_and_archive_label_mismatch_are_excluded():
    prices, sessions, archive, ledger = fixture()
    extra = prices.iloc[[0]].copy()
    extra['close'] = 99.
    result = run(pd.concat([prices, extra]), sessions, archive, ledger)
    assert result['markets']['KOSPI']['cohorts'][0]['metrics']['n'] == 0
    prices, sessions, archive, ledger = fixture()
    archive['markets'][0]['evaluation']['observations'][0]['actual_return'] = .5
    assert run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['metrics']['n'] == 0


def test_hash_integrity_is_required():
    prices, sessions, archive, ledger = fixture()
    ledger['history_hash'] = 'invalid'
    with pytest.raises(ValueError, match='history_hash'):
        audit_recent_accuracy(prices, archive, ledger, sessions=sessions, as_of=pd.Timestamp('2026-11-19T09:00:00Z'), days=1)


def test_historical_selected_probability_is_not_replaced_by_raw_baseline():
    prices, sessions, archive, ledger = fixture()
    selected = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]
    assert selected['model'] == 'selected'
    assert selected['daily'][0]['p_up'] == .7
    assert selected['baselines']['flat']['price_mae_points'] == pytest.approx(2.)


def test_live_flat_and_mean_baselines_use_scored_pairs():
    prices, sessions, archive, ledger = fixture()
    ledger['forecasts'][0]['baselines'] = {'mean': {'expected_return': .005, 'p_up': .5}}
    cohort = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert cohort['baselines']['flat']['price_mae_points'] == pytest.approx(2.)
    assert cohort['baselines']['mean']['price_mae_points'] == pytest.approx(1.5)


def test_distinct_protocols_and_roles_are_never_pooled():
    prices, sessions, archive, ledger = fixture()
    f = dict(ledger['forecasts'][0], role='incumbent_reference', expected_return=0.)
    f['forecast_key'] = digest({k: f[k] for k in ('market', 'target_date', 'protocol_hash', 'role')})
    ledger['forecasts'].append(f)
    o = dict(ledger['outcomes'][0], role='incumbent_reference', forecast_key=f['forecast_key'])
    ledger['outcomes'].append(o)
    ledger['attempts'][0]['accepted_roles'].append('KOSPI:incumbent_reference')
    cohorts = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts']
    assert len(cohorts) == 2
    assert {c['role']: c['metrics']['price_mae_points'] for c in cohorts} == pytest.approx({'primary': 1., 'incumbent_reference': 2.})


def test_replay_or_forecast_without_verified_provenance_is_excluded():
    prices, sessions, archive, ledger = fixture()
    ledger['forecasts'][0]['replay'] = True
    cohort = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert cohort['daily'][0]['status'] == 'replay_forecast'
    assert cohort['metrics']['n'] == 0


def test_historical_missing_or_future_label_not_scored():
    prices, sessions, archive, ledger = fixture()
    del archive['markets'][0]['evaluation']['observations'][0]['label_available_at']
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['daily'][0]
    assert row['status'] == 'label_not_available_as_of'


def test_cutoff_must_follow_previous_session_close():
    prices, sessions, archive, ledger = fixture()
    archive['markets'][0]['evaluation']['observations'][0]['prediction_cutoff'] = '2026-11-18T05:00:00Z'
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['daily'][0]
    assert row['status'] == 'invalid_prediction_cutoff'


def test_baseline_denominator_only_includes_scored_predictions():
    prices, sessions, archive, ledger = fixture()
    archive['markets'][0]['evaluation']['observations'][0]['expected_return'] = None
    historical = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]
    assert historical['metrics']['n'] == 0
    assert historical['baselines']['flat']['n'] == 0


def test_merged_source_conflicts_are_explicitly_reported():
    prices, sessions, archive, ledger = fixture()
    prices.loc[(prices.index_code == 'KOSPI') & (prices.session_date == '2026-11-19'), 'status'] = 'conflict'
    prices.loc[(prices.index_code == 'KOSPI') & (prices.session_date == '2026-11-19'), 'close'] = float('nan')
    cohort = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert cohort['daily'][0]['status'] == 'conflicting_price'


def test_absent_historical_mean_is_not_replaced_by_model_prediction():
    prices, sessions, archive, ledger = fixture()
    del archive['markets'][0]['evaluation']['observations'][0]['baselines']['mean']
    historical = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]
    assert historical['metrics']['n'] == 1
    assert historical['baselines']['mean']['n'] == 0


def test_live_uses_issued_baseline_after_independent_rounded_check():
    prices, sessions, archive, ledger = fixture()
    prices.loc[(prices.index_code == 'KOSPI') & (prices.session_date == '2026-11-18'), 'close'] = 100.004
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]
    assert row['status'] == 'scored'
    assert row['baseline_close'] == 100.
    assert row['expected_index'] == pytest.approx(101.)


def test_primary_requires_live_captured_and_malformed_predictions_do_not_crash():
    prices, sessions, archive, ledger = fixture()
    ledger['forecasts'][0]['fit_provenance'] = 'retrospective'
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]
    assert row['status'] == 'non_live_fit_provenance'
    ledger['forecasts'][0]['fit_provenance'] = 'live_captured'
    ledger['forecasts'][0]['p_up'] = '0.6'
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert row['metrics']['brier'] == pytest.approx(.16)
    ledger['forecasts'][0]['expected_return'] = -1.1
    assert run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]['status'] == 'invalid_return'


def test_future_duplicate_forecast_and_outcome_do_not_change_earlier_audit():
    prices, sessions, archive, ledger = fixture()
    future = dict(ledger['forecasts'][0], issued_at='2026-11-20T09:00:00Z', expected_return=.5)
    ledger['forecasts'].append(future)
    ledger['outcomes'].append(dict(ledger['outcomes'][0], observed_at='2026-11-20T10:00:00Z', actual_return=.5))
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]
    assert row['status'] == 'scored'
    assert row['expected_return'] == .01


def test_price_publication_after_audit_cutoff_is_not_used():
    prices, sessions, archive, ledger = fixture()
    prices.loc[(prices.index_code == 'KOSPI') & (prices.session_date == '2026-11-19'), 'published_at'] = '2026-11-19T10:00:00Z'
    cohort = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]
    assert cohort['daily'][0]['status'] == 'price_not_observed_as_of'


def test_historical_mean_baseline_does_not_inherit_forecast_probability_or_interval():
    prices, sessions, archive, ledger = fixture()
    row = archive['markets'][0]['evaluation']['observations'][0]
    row.update(range_low=-.1, range_high=.1)
    mean = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['baselines']['mean']
    assert mean['n'] == 1
    assert mean['probability_n'] == 0
    assert mean['interval_n'] == 0


def test_live_string_baseline_probabilities_and_ranges_are_numeric():
    prices, sessions, archive, ledger = fixture()
    ledger['forecasts'][0]['baselines'] = {'mean': {'expected_return': '.01', 'p_up': '.6',
                                                     'range_low': '-.01', 'range_high': '.03'}}
    mean = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['baselines']['mean']
    assert mean['brier'] == pytest.approx(.16)
    assert mean['interval_coverage'] == 1


@pytest.mark.parametrize('updates', [
    {'fit_cutoff': '2026-11-18T09:00:00Z'},
    {'fit_cutoff': '2026-11-18T07:00:00'},
    {'fit_asof': '2026-11-19'},
    {'fit_asof': 'bad-date'},
])
def test_historical_fit_evidence_cannot_postdate_prediction(updates):
    prices, sessions, archive, ledger = fixture()
    archive['markets'][0]['evaluation']['observations'][0].update(updates)
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['daily'][0]
    assert row['status'] == 'invalid_fit_evidence'


def test_effective_label_time_uses_official_close_and_is_reported():
    prices, sessions, archive, ledger = fixture()
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['historical'][0]['daily'][0]
    assert row['status'] == 'scored'
    assert row['stored_label_available_at'] == '2026-11-19T06:30:00Z'
    assert row['effective_label_available_at'] == '2026-11-19T07:30:00+00:00'
    assert 'stored_label_before_official_close' in row['flags']
    ledger['outcomes'][0]['label_available_at'] = '2026-11-19T08:30:00Z'
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]
    assert row['status'] == 'outcome_observed_before_effective_label'


def test_upstream_price_not_observed_status_is_propagated():
    prices, sessions, archive, ledger = fixture()
    prices.loc[(prices.index_code == 'KOSPI') & (prices.session_date == '2026-11-19'), 'status'] = 'price_not_observed_as_of'
    row = run(prices, sessions, archive, ledger)['markets']['KOSPI']['cohorts'][0]['daily'][0]
    assert row['status'] == 'price_not_observed_as_of'

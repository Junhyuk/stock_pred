"""Economic overlays execute real model paths while preserving price forecasts."""
import importlib
from dataclasses import replace

import numpy as np
import pandas as pd
from test_reliable_accuracy_protocol import config, sample_frame

from roboquant.index_forecast.economic_calendar import MACRO_FEATURES
from roboquant.index_forecast.reliable_intervals import normalized_interval_shadows


def api():
    spec = importlib.util.find_spec('roboquant.index_forecast.economic_forecast')
    assert spec is not None, 'economic forecast integration is missing'
    return importlib.import_module('roboquant.index_forecast.economic_forecast')


class SyntheticPITStore:
    def numeric_features(self, cutoff, *, policy):
        day = pd.Timestamp(cutoff).dayofyear
        return {'features': {name: float(np.sin(day / (i + 4))) for i, name in enumerate(MACRO_FEATURES)},
                'ready': True, 'missing_features': [], 'feature_policy': policy}

    def forecast_context(self, **kwargs):
        return {'event_bucket': pd.Timestamp(kwargs['cutoff']).day % 2 == 0, 'calendar_complete': True}


def test_fixed_numeric_shadow_runs_real_fit_and_matched_price_ablation(tmp_path):
    frame = sample_frame()
    settings = replace(config(), calendar_name='XNYS')
    result = api().numeric_shadow(frame, store=SyntheticPITStore(), settings=settings,
        market='US_SPX', state_dir=tmp_path, live=False)
    assert result['candidate'] == 'linear:price_macro_v1'
    assert result['forecast']['expected_return'] is not None
    assert result['forecast']['fit_provenance'] == 'retrospective'
    assert result['production_weight'] == 0
    histories = result['evaluation']['candidate_observations']
    assert set(histories) == {'linear:price', 'linear:price_macro_v1'}
    price, macro = histories['linear:price'], histories['linear:price_macro_v1']
    assert len(macro) > 30
    assert [(r['asof_date'], r['fit_asof'], r['fit_cutoff'], r['train_n']) for r in price] == [
           (r['asof_date'], r['fit_asof'], r['fit_cutoff'], r['train_n']) for r in macro]
    assert result['comparison']['same_eligible_dates'] is True
    assert result['feature_policy'] == 'observed_current_receipt_v1'


def test_missing_numeric_inputs_abstain_without_model_evaluation(tmp_path, monkeypatch):
    class EmptyStore(SyntheticPITStore):
        def numeric_features(self, cutoff, *, policy):
            return {'features': dict.fromkeys(MACRO_FEATURES), 'ready': False,
                    'missing_features': list(MACRO_FEATURES), 'feature_policy': policy}
    result = api().numeric_shadow(sample_frame(), store=EmptyStore(), settings=config(),
        market='KOSPI', state_dir=tmp_path, live=False)
    assert result['status'] == 'not_ready'
    assert result['forecast']['expected_return'] is None
    assert result['readiness']['missing_features'] == list(MACRO_FEATURES)


def test_event_interval_uses_same_bucket_or_explicit_pooled_fallback():
    rows = [{'label_available_at': '2020-01-01T00:00:00Z', 'target_close_at': '2020-01-01T00:00:00Z',
             'expected_return': 0., 'actual_return': .02 if i < 60 else .001,
             'scale_at_prediction': .01, 'calendar_event_bucket': i < 60,
             'baselines': {'mean': {'expected_return': 0.}}} for i in range(120)]
    kwargs = dict(cutoff='2020-01-02T00:00:00Z', predicted=0., scale=.01, mean_prediction=0.)
    conditional = normalized_interval_shadows(rows, event_bucket=True, **kwargs)['event_conditional']
    assert conditional['status'] == 'ready'
    assert conditional['calibration_n'] == 60
    assert conditional['range_high'] == .02
    fallback = normalized_interval_shadows(rows[1:], event_bucket=True, **kwargs)['event_conditional']
    assert fallback['fallback'] == 'pooled_volatility_normalized'
    assert fallback['bucket_calibration_n'] == 59
    unknown = normalized_interval_shadows(rows, event_bucket=None, **kwargs)['event_conditional']
    assert unknown['fallback_reason'] == 'calendar_coverage_unknown'


def test_actual_pipeline_writes_verified_reference_evidence_and_calendar_context(tmp_path, monkeypatch):
    import json
    from datetime import UTC, datetime, timedelta

    from test_reliable_pipeline import config as pipeline_config
    from test_reliable_pipeline import database

    from roboquant.index_forecast import reliable_pipeline

    class Clock:
        calls = 0

        @classmethod
        def now(cls, tz=None):
            cls.calls += 1
            return (datetime(2026, 10, 25, 23, 1, tzinfo=UTC) + timedelta(seconds=cls.calls)).astimezone(tz)
    monkeypatch.setattr(reliable_pipeline, 'datetime', Clock)
    path, sessions = database(tmp_path)
    output = tmp_path / 'published'
    result = reliable_pipeline.run_reliable_pipeline(path, output, config=pipeline_config(), sessions=sessions)
    assert result['status'] == 'research_only'
    assert 'economic_calendar' in result['markets'][0]
    assert result['markets'][0]['numeric_shadow']['status'] == 'not_ready'
    history = json.loads((output / 'forward_history.json').read_text())
    manifest = json.loads((output / 'reliable_publication.json').read_text())
    references = [r for r in history['forecasts'] if r['role'] == 'incumbent_reference']
    assert references
    assert all(r['issued_at'] == manifest['publication_checked_at'] for r in references)
    assert not [r for r in history['forecasts'] if r['role'] == 'primary']
    assert (output / 'forward_validation.json').is_file()


def test_published_numeric_forecast_contains_cutoff_known_baselines(tmp_path):
    result = api().numeric_shadow(sample_frame(), store=SyntheticPITStore(),
        settings=replace(config(), calendar_name='XNYS'), market='US_SPX', state_dir=tmp_path, live=False)
    baselines = result['forecast']['baselines']
    assert baselines['mean']['expected_return'] is not None
    assert 0 <= baselines['up_rate']['p_up'] <= 1
    assert baselines['up_rate']['known_label_n'] > 0


def test_corrupt_optional_economic_history_does_not_block_price_path(tmp_path):
    bad = tmp_path / 'economic_history.json'
    bad.write_text('{"schema_version":"economic_history_v1","history_hash":"tampered"}')
    store = api().load_calendar({'economic_calendar': {'sqlite_path': str(tmp_path / 'private.sqlite'),
                                                     'history_path': str(bad)}})
    assert store is None


def test_unavailable_macro_candidate_still_reports_ten_required_evidence_fields(tmp_path):
    result = api().numeric_shadow(sample_frame(), store=None, settings=config(), market='KOSPI', state_dir=tmp_path, live=False)
    assert set(result['evidence']) == {'economic_hypothesis', 'data_access', 'publication_clock', 'cost',
        'missingness', 'history_length', 'leakage_risks', 'baseline_increase', 'regime_performance', 'ablation'}
    assert result['evidence']['baseline_increase'] == 'unestablished'

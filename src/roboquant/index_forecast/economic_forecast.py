"""Predeclared calendar and numeric research overlays, separate from price selection."""
from __future__ import annotations

from dataclasses import replace

import pandas as pd

from roboquant.index_forecast.dataset import PRICE_FEATURES
from roboquant.index_forecast.economic_calendar import MACRO_FEATURES
from roboquant.index_forecast.reliable import (
    evaluate_reliable_forecasts,
    forecast_reliable_challenger,
    forecast_reliable_latest,
    resolve_live_fit,
)

NUMERIC_POLICY = 'observed_current_receipt_v1'


def calendar_frame(frame, store):
    """Knowledge is queried at each prediction's own cutoff, never today's snapshot."""
    result = frame.copy()
    result['calendar_event_bucket'] = pd.Series([None] * len(result), index=result.index, dtype=object)
    contexts = {}
    if store is None:
        return result, contexts
    for index, row in result.iterrows():
        if pd.isna(row.get('target_close_at')):
            continue
        # Close timestamps are supplied by input builders. The conservative
        # fallback uses the prediction cutoff, excluding already released events.
        start = row.get('asof_close_at', row.get('session_close_at', row['prediction_cutoff']))
        context = store.forecast_context(asof_close=start, cutoff=row['prediction_cutoff'], target_close=row['target_close_at'])
        result.at[index, 'calendar_event_bucket'] = context['event_bucket']
        contexts[row['asof_date']] = context
    return result, contexts


def numeric_shadow(frame, *, store, settings, market, state_dir, live, allow_capture=True):
    result = {'candidate': 'linear:price_macro_v1', 'feature_policy': NUMERIC_POLICY,
              'status': 'not_ready', 'production_weight': 0.,
              'forecast': {'status': 'not_ready', 'expected_return': None, 'p_up': None,
                           'range_low': None, 'range_high': None, 'production_weight': 0.},
              'comparison': {'same_eligible_dates': False},
              'evidence': {
                  'economic_hypothesis': 'Scheduled macro releases may alter interval risk; observed changes may add information beyond price.',
                  'data_access': 'Official Fed/BOK/BOJ/ECB, BLS/BEA and national/Eurostat sources; normalized receipt ledger.',
                  'publication_clock': 'Actual receipt controls availability; unknown original publication is never backdated.',
                  'cost': 'Public free sources; bounded120-second collection; API keys only where explicitly configured.',
                  'missingness': {'missing_current': 14, 'current_denominator': 14, 'complete_history_rows': 0},
                  'history_length': {'eligible_rows': 0, 'start': None, 'end': None},
                  'leakage_risks': 'Revisions, retroactively known schedules, date-only timestamps and receipt delays; own-cutoff queries and immutable vintages required.',
                  'baseline_increase': 'unestablished',
                  'regime_performance': 'unmeasured; future cohort/regime denominators required',
                  'ablation': 'Predeclared linear:price versus linear:price_macro_v1 on identical complete dates; unmeasured until ready'},
              'evidence_status': 'incremental_accuracy_unestablished; synthetic_tests_are_not_market_evidence'}
    if frame.empty or store is None:
        return {**result, 'readiness': {'ready': False, 'missing_features': list(MACRO_FEATURES)}}
    readiness = store.numeric_features(frame.iloc[-1]['prediction_cutoff'], policy=NUMERIC_POLICY)
    result['readiness'] = readiness
    result['evidence']['missingness']['missing_current'] = len(readiness['missing_features'])
    if not readiness['ready']:
        return result
    enriched = frame.copy()
    features = [*PRICE_FEATURES, *MACRO_FEATURES]
    for index, row in enriched.iterrows():
        values = store.numeric_features(row['prediction_cutoff'], policy=NUMERIC_POLICY)['features']
        for feature in MACRO_FEATURES:
            enriched.at[index, feature] = values[feature]
    common = enriched.dropna(subset=[*features, 'label_return', 'target_date'])
    minimum = settings.initial_train_days + settings.purge_days + settings.min_calibration_rows + settings.development_days + settings.test_days
    result['comparison'].update(eligible_n=len(common), required_n=minimum)
    result['evidence']['missingness']['complete_history_rows'] = len(common)
    result['evidence']['history_length'] = {'eligible_rows': len(common),
        'start': str(common.asof_date.min()) if not common.empty else None,
        'end': str(common.asof_date.max()) if not common.empty else None}
    if len(common) < minimum:
        return {**result, 'reason': 'insufficient_observed_macro_history'}
    config = replace(settings, model_families=('linear',))
    latest = enriched.iloc[[-1]].copy()
    evaluation = evaluate_reliable_forecasts(common, feature_groups={'price': list(PRICE_FEATURES), 'price_macro_v1': features},
        settings=config, reporting_cutoff=latest.iloc[0]['prediction_cutoff'], prediction_grid=enriched)
    forecast = forecast_reliable_challenger(common, latest, evaluation, family='linear', group='price_macro_v1', settings=config)
    fit_state = {'status': 'retrospective_replay'}
    if live:
        selected = {'family': 'linear', 'group': 'price_macro_v1', 'features': features}
        fit_state = resolve_live_fit(common, latest, selected, settings=config, state_dir=state_dir,
                                    market=f'{market}_MACRO', allow_capture=allow_capture)
        own = {**evaluation, 'selected': selected, 'status': 'rejected',
               'gate_reasons': ['numeric_shadow_not_promoted'],
               'calibration_observations': evaluation.get('candidate_observations', {}).get('linear:price_macro_v1', [])}
        forecast = forecast_reliable_latest(common, latest, own, settings=config,
            fit_artifact=fit_state.get('artifact'), require_live_snapshot=True)
    histories = evaluation.get('candidate_observations', {})
    price = histories.get('linear:price', [])
    macro = histories.get('linear:price_macro_v1', [])
    result['comparison'].update(same_eligible_dates=[r['asof_date'] for r in price] == [r['asof_date'] for r in macro],
                                ablation='linear:price vs fixed linear:price_macro_v1 on identical complete input rows')
    result['evidence']['ablation'] = {'candidate_reports': evaluation.get('candidate_reports', []),
                                    'interpretation': 'retrospective/exploratory; no independent incremental accuracy claim'}
    return {**result, 'status': forecast['status'], 'forecast': forecast, 'evaluation': evaluation,
            'fit_state': {k: v for k, v in fit_state.items() if k != 'artifact'}}


def load_calendar(config):
    """Optional normalized history; missing source never blocks price context."""
    import json
    from pathlib import Path

    from roboquant.index_forecast.economic_calendar import EconomicCalendarStore

    options = config.get('economic_calendar', {})
    path = options.get('sqlite_path')
    if not path:
        return None
    store = EconomicCalendarStore(path)
    history = options.get('history_path')
    if history and Path(history).is_file():
        try:
            store.import_history(json.loads(Path(history).read_text()), trusted_archive=True)
        except (OSError, ValueError, KeyError, TypeError):
            return None
    return store


def begin_forward(output, *, config, settings, started_at, code_hash, replay):
    import json
    from dataclasses import asdict
    from pathlib import Path

    from roboquant.index_forecast.forward_ledger import ForwardLedger

    output = Path(output)
    import hashlib
    private_key = hashlib.sha256(str(output.resolve()).encode()).hexdigest()[:16]
    ledger = ForwardLedger(config.get('forward_ledger_path',
        Path(config.get('private_state_dir', 'data/processed/accuracy-state')) / f'forward-{private_key}.sqlite'))
    history = output / 'forward_history.json'
    if history.is_file():
        ledger.import_history(json.loads(history.read_text()), trusted_archive=True)
    protocol = {'protocol': 'accuracy_calendar_v1', 'code_hash': code_hash, 'settings': asdict(settings),
                'numeric_candidate': 'linear:price_macro_v1', 'numeric_policy': NUMERIC_POLICY,
                'interval_candidates': ['volatility_normalized', 'event_conditional'],
                'primary_metrics': ['brier', 'interval_score80'], 'minimum_future_mature_rows': 252,
                'promotion': 'independent_review_required; existing_gates_preserved; production_weight_zero'}
    if not replay:
        ledger.register_protocol(protocol, registered_at=started_at)
    return ledger, protocol


def finish_forward(output, *, evidence, frames, observed_at):
    from pathlib import Path

    from roboquant.index_forecast.reliable_pipeline import _atomic_json

    ledger, protocol = evidence
    ledger.record_publication(output, protocol=protocol, attempted_at=observed_at)
    observations = []
    for market, frame in frames.items():
        for _, row in frame.iterrows():
            if pd.notna(row.get('label_return')) and pd.notna(row.get('target_close_at')) and pd.notna(row.get('label_available_at')):
                observations.append({'market': market, 'asof_date': row['asof_date'], 'target_date': row['target_date'],
                    'actual_return': float(row['label_return']), 'target_close_at': row['target_close_at'],
                    'label_available_at': row['label_available_at']})
    # Only issued targets need outcome attempts; do not persist full price history.
    issued = {(r['market'], r['asof_date'], r['target_date']) for r in ledger.export_history()['forecasts']}
    ledger.record_outcomes([r for r in observations if (r['market'], r['asof_date'], r['target_date']) in issued], observed_at=observed_at)
    _atomic_json(Path(output) / 'forward_history.json', ledger.export_history())
    _atomic_json(Path(output) / 'forward_validation.json', ledger.report())

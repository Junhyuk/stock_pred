import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/export_market_chart_model_inputs.py'


def load_exporter():
    spec = importlib.util.spec_from_file_location('chart_model_inputs', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_selected_but_unready_a_and_absent_frozen_snapshot_are_not_claimed_as_used():
    export = load_exporter()
    a = {'run_id': 'a', 'markets': [{'market': 'KOSPI', 'context': {'asof_date': '2026-11-27'},
         'forecast': {'status': 'not_ready', 'selected': {'features': ['return_1d']}}}]}
    shared = {'asof_date': '2026-11-27', 'target_date': '2026-11-30', 'issue_stage': 'premarket',
              'prediction_cutoff': '2026-11-27T23:04:55+00:00', 'publication_id': 'x'}
    b = {**shared, 'markets': [{'market': 'KOSPI', 'close': 100}]}
    c = {**shared, 'status': 'ready', 'trigger_returns': {'KOSPI': .04},
         'markets': [{'market': 'KOSPI', 'close': 100, 'method': 'hist_gradient'}]}
    result = export.build_export(a, b, c)
    model_a = next(x for x in result['models'] if x['key'] == 'A')
    assert model_a['rows'][0]['availability'] == 'selected_not_ready'
    assert model_a['rows'][0]['value'] is None
    model_b = next(x for x in result['models'] if x['key'] == 'B')
    assert next(x for x in model_b['rows'] if x['key'] == 'close')['value'] == 100
    assert next(x for x in model_b['rows'] if x['key'] == 'return_1d')['value'] is None
    koru = next(x for x in model_b['rows'] if x['key'] == 'koru_return_1d')
    assert koru['heads'] == ['close'] and koru['value'] is None
    model_c = next(x for x in result['models'] if x['key'] == 'C')
    assert next(x for x in model_c['rows'] if x['key'] == 'trigger_return')['value'] == .04


def test_c_without_trigger_has_no_fictitious_scoring_features():
    export = load_exporter()
    c = {'status': 'not_triggered', 'trigger_returns': {'KOSPI': .01, 'KOSDAQ': -.01}, 'markets': []}
    result = export.build_export({}, {}, c)
    for model in (x for x in result['models'] if x['key'] == 'C'):
        assert [row['key'] for row in model['rows']] == ['trigger_return']


def test_reconstruct_requires_exact_publication_and_uses_frozen_morning_sessions(tmp_path, monkeypatch):
    from datetime import datetime
    import pandas as pd
    import pytest
    from test_bc_cpu_afterclose import prepared_inputs
    from test_bc_morning import observations_for
    from roboquant.index_forecast.bc_morning import BASE_SYMBOLS
    from roboquant.index_forecast import automation_state
    monkeypatch.setattr(automation_state, 'source_fingerprint', lambda: 'stable-test-code')

    export = load_exporter()
    observed = observations_for(['2026-11-24', '2026-11-25'], (*BASE_SYMBOLS, 'KORU'))
    manifest = prepared_inputs(tmp_path, {'KOSPI': 1050, 'KOSDAQ': 1000},
                               cutoff='2026-11-25T23:00:00+00:00',
                               operation_mode='morning', observations=observed)
    import json
    info = json.loads(manifest.read_text())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat('2026-11-25T23:01:00+00:00').astimezone(tz)
    monkeypatch.setattr(export, 'datetime', Clock)
    b = {'publication_id': info['manifest_hash'], 'prediction_cutoff': info['prepared_at'],
         'issue_stage': 'premarket', 'asof_date': '2026-11-25', 'target_date': '2026-11-26',
         'markets': [{'market': 'KOSPI', 'close': 1050, 'asof_date': '2026-11-25',
                      'prediction_cutoff': info['prepared_at']}]}
    result = export.reconstruct(manifest, b, {})
    row = result[('B', 'KOSPI')]
    assert row['return_1d'] == pytest.approx(.05)
    assert row['us_session_date'] == '2026-11-25'
    actual = observed.loc[observed.symbol.eq('KORU'), 'close'].tolist()
    assert row['koru_return_1d'] == pytest.approx(actual[1] / actual[0] - 1)
    b['publication_id'] = 'different'
    assert export.reconstruct(manifest, b, {}) == {}


def test_carried_forward_postclose_row_uses_its_own_dates_and_recipe():
    export = load_exporter()
    b = {'issue_stage': 'premarket', 'asof_date': '2026-11-27', 'target_date': '2026-11-30',
         'prediction_cutoff': 'new', 'markets': [{'market': 'KOSPI', 'asof_date': '2026-11-26',
         'target_date': '2026-11-27', 'prediction_cutoff': 'old', 'close': 100,
         'carried_forward': True, 'variant': '장후 검증 기준선 · 종가 유지'}]}
    model = export.build_export({}, b, {})['models'][0]
    assert model['asof_date'] == '2026-11-26'
    assert model['target_date'] == '2026-11-27'
    assert model['prediction_cutoff'] == 'old'
    assert model['issue_stage'] == 'postclose'
    assert 'sp500_return_1d' not in {row['key'] for row in model['rows']}


def test_selected_a_us_features_do_not_attach_to_domestic_index():
    export = load_exporter()
    a = {'markets': [{'market': 'KOSPI', 'context': {'asof_date': '2026-11-27'},
         'forecast': {'status': 'not_ready', 'selected': {'features': ['us_spx_return_1d', 'sox_nasdaq_spread_1d']}}}]}
    rows = export.build_export(a, {}, {})['models'][0]['rows']
    assert rows[0]['chart_series_key'] == 'sp500'
    assert rows[1]['chart_series_key'] is None


def test_postclose_b_volatility_is_an_interval_input():
    export = load_exporter()
    b = {'issue_stage': 'postclose', 'asof_date': '2026-11-27',
         'markets': [{'market': 'KOSPI', 'close': 100}]}
    rows = export.build_export({}, b, {})['models'][0]['rows']
    assert next(row for row in rows if row['key'] == 'volatility_20d')['heads'] == ['interval']


def test_a_research_candidate_with_published_score_uses_verified_values():
    export = load_exporter()
    a = {'markets': [{'market': 'KOSPI', 'context': {'asof_date': '2026-11-27'},
         'forecast': {'status': 'research_candidate', 'issued_at': '2026-11-27T23:10:00Z',
                      'expected_return': .01, 'selected': {'features': ['return_1d']}}}]}
    result = export.build_export(a, {}, {}, {('A', 'KOSPI'): {'return_1d': .02}})
    row = result['models'][0]['rows'][0]
    assert row['value'] == .02
    assert row['availability'] == 'reconstructed'


def test_a_requires_published_feature_hash_before_showing_frozen_price_vector(tmp_path, monkeypatch):
    from datetime import datetime
    import json
    from test_bc_cpu_afterclose import prepared_inputs
    from roboquant.index_forecast.market_closes import effective_index_eod_frame, apply_live_revisions
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame, _frame_hash
    from roboquant.index_forecast.dataset import PRICE_FEATURES
    from roboquant.index_forecast.reliable_inputs import utc_timestamp
    from roboquant.index_forecast import automation_state
    monkeypatch.setattr(automation_state, 'source_fingerprint', lambda: 'stable-test-code')

    export = load_exporter()
    manifest = prepared_inputs(tmp_path, {'KOSPI': 1050})
    info = json.loads(manifest.read_text())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat('2026-11-25T09:14:00+00:00').astimezone(tz)
    monkeypatch.setattr(export, 'datetime', Clock)
    context = {'asof_date':'2026-11-25', 'target_date':'2026-11-26', 'stage':'postclose',
               'prediction_cutoff':info['prepared_at']}
    prices = effective_index_eod_frame(manifest.parent / info['files']['sqlite']['path'],
                                       cutoff=utc_timestamp(info['prepared_at']))
    frame = build_reliable_price_frame(prices.loc[prices.index_code == 'KOSPI'], forecast_context=context)
    live = build_reliable_price_frame(apply_live_revisions(prices).loc[lambda x: x.index_code == 'KOSPI'], forecast_context=context)
    mask = frame.asof_date == context['asof_date']
    frame.loc[mask, ['close', *PRICE_FEATURES]] = live.loc[mask, ['close', *PRICE_FEATURES]].to_numpy()
    feature_hash = _frame_hash(frame.loc[mask, ['asof_date', *PRICE_FEATURES]])
    item = {'market':'KOSPI', 'context':context, 'feature_hash':feature_hash,
            'forecast':{'issued_at':'2026-11-25T09:14:00+00:00', 'expected_return':.01,
                        'selected':{'group':'price', 'features':PRICE_FEATURES}}}
    a = {'markets':[item]}
    result = export.reconstruct(manifest, {}, {}, a)
    assert ('A', 'KOSPI') in result
    item['feature_hash'] = 'wrong'
    assert export.reconstruct(manifest, {}, {}, a) == {}


def test_a_log_volatility_and_unknown_feature_are_not_misrepresented_as_simple_chart_transforms():
    export = load_exporter()
    a = {'markets':[{'market':'KOSPI', 'context':{'asof_date':'2026-11-27'},
        'forecast':{'status':'not_ready','selected':{'features':['volatility_20d','foreign_imbalance']}}}]}
    rows = export.build_export(a, {}, {})['models'][0]['rows']
    assert rows[0]['chart_transform'] is None
    assert rows[1]['chart_series_key'] is None


def test_c_regime_prior_does_not_claim_live_one_or_five_day_features():
    export = load_exporter()
    c = {'status':'ready', 'asof_date':'2026-11-27', 'markets':[
        {'market':'KOSDAQ','close':100,'method':'regime_prior'}]}
    rows = export.build_export({}, {}, c)['models'][0]['rows']
    assert [row['key'] for row in rows] == ['close', 'volatility_20d']
    assert rows[0]['unit'] == 'index'


def test_c_histogram_scores_are_conditional_on_fallback_and_use_daily_sample_volatility():
    export = load_exporter()
    c = {'status':'ready', 'asof_date':'2026-11-27', 'markets':[
        {'market':'KOSPI','close':100,'method':'hist_gradient'}]}
    rows = export.build_export({}, {}, c)['models'][0]['rows']
    volatility = next(row for row in rows if row['key'] == 'volatility_20d')
    assert volatility['chart_transform'] == 'volatility20_daily'
    assert '대체' in volatility['derivation']


def test_a_selected_feature_labels_are_user_facing_korean():
    export = load_exporter()
    a = {'markets':[{'market':'KOSPI','context':{},'forecast':{'status':'not_ready',
          'selected':{'features':['return_1d','volatility_20d','ma_gap_5d','us_spx_return_1d']}}}]}
    rows = export.build_export(a, {}, {})['models'][0]['rows']
    assert [r['label'] for r in rows] == ['1일 수익률','20일 일간 로그 수익률 변동성','5일 이동평균 괴리율','S&P 500 1일 수익률']


def test_retains_verified_inputs_only_for_identical_published_model_identity():
    export = load_exporter()
    b = {'issue_stage':'premarket','publication_id':'manifest-1','asof_date':'2026-11-27',
         'target_date':'2026-11-30','prediction_cutoff':'cutoff-1',
         'markets':[{'market':'KOSPI','close':100}]}
    old = export.build_export({}, b, {}, {('B','KOSPI'):{'return_1d':.12}})
    fresh = export.build_export({}, b, {})
    merged = export.retain_verified_inputs(fresh, old)
    row = next(row for row in merged['models'][0]['rows'] if row['key'] == 'return_1d')
    assert row['value'] == .12 and row['availability'] == 'reconstructed'
    b['publication_id'] = 'manifest-2'
    changed = export.retain_verified_inputs(export.build_export({}, b, {}), old)
    assert next(row for row in changed['models'][0]['rows'] if row['key'] == 'return_1d')['value'] is None


def test_replay_uses_verified_completion_clock_but_never_future_completion(tmp_path, monkeypatch):
    from datetime import datetime
    import json
    from test_bc_cpu_afterclose import prepared_inputs
    from roboquant.index_forecast import automation_state
    monkeypatch.setattr(automation_state, 'source_fingerprint', lambda: 'stable-test-code')
    export = load_exporter()
    manifest = prepared_inputs(tmp_path, {'KOSPI':1050})
    info = json.loads(manifest.read_text())
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat('2026-11-26T09:14:00+00:00').astimezone(tz)
    monkeypatch.setattr(export, 'datetime', Clock)
    b = {'publication_id':info['manifest_hash'], 'prediction_cutoff':info['prepared_at'],
         'issue_stage':'postclose','asof_date':'2026-11-25','target_date':'2026-11-26',
         'markets':[{'market':'KOSPI','close':1050,'asof_date':'2026-11-25',
                     'prediction_cutoff':info['prepared_at']}]}
    assert export.reconstruct(manifest,b,{}) == {}
    assert ('B','KOSPI') in export.reconstruct(manifest,b,{},replay_completed=True)
    receipt_path = manifest.parent / 'completion.json'
    receipt = json.loads(receipt_path.read_text())
    receipt['completed_at'] = '2026-11-25T09:14:00+00:00'
    receipt_path.write_text(json.dumps(receipt))
    assert export.reconstruct(manifest,b,{},replay_completed=True) == {}
    class Earlier(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat('2026-11-24T09:14:00+00:00').astimezone(tz)
    monkeypatch.setattr(export, 'datetime', Earlier)
    assert export.reconstruct(manifest,b,{},replay_completed=True) == {}


def test_a_vix_level_uses_index_units():
    export = load_exporter()
    a = {'markets':[{'market':'KOSPI','forecast':{'selected':{'features':['us_vix_level']}}}]}
    row = export.build_export(a,{}, {})['models'][0]['rows'][0]
    assert row['unit'] == 'index'
    assert row['chart_transform'] == 'level'

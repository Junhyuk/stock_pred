#!/usr/bin/env python3
"""Export published-model input metadata, reconstructing only from matched frozen inputs.

The prepared manifest and completion receipt are verified with the same restore
routine used by the forecast runner. No model is fitted or forecast generated.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

MARKETS = ('KOSPI', 'KOSDAQ')
DOMESTIC = {
    'return_1d': ('1일 수익률', 'return1'),
    'return_5d': ('5일 수익률', 'return5'),
    'volatility_20d': ('20일 일간 수익률 변동성', 'volatility20_daily'),
}
A_LABELS = {'return_1d': '1일 수익률', 'return_5d': '5일 수익률', 'return_20d': '20일 수익률',
            'volatility_5d': '5일 일간 로그 수익률 변동성', 'volatility_20d': '20일 일간 로그 수익률 변동성',
            'ma_gap_5d': '5일 이동평균 괴리율', 'ma_gap_20d': '20일 이동평균 괴리율',
            'us_spx_return_1d': 'S&P 500 1일 수익률', 'us_nasdaq_return_1d': 'Nasdaq 1일 수익률',
            'us_sox_return_1d': 'SOX 1일 수익률', 'us_vix_level': 'VIX 지수',
            'us_vix_return_1d': 'VIX 1일 수익률', 'sox_nasdaq_spread_1d': 'SOX와 Nasdaq 1일 수익률 차이'}
US = {'^GSPC': ('sp500', 'S&P 500'), '^IXIC': ('nasdaq', 'Nasdaq'),
      '^SOX': ('sox', 'SOX'), 'EWY': ('ewy', 'EWY'), 'KORU': ('koru', 'KORU')}


def _row(key, label, date, market, *, value=None, reason=None, heads=None,
         availability='unavailable', chart_series_key='domestic', transform=None, derivation=None):
    return {'key': key, 'label': label, 'date': date, 'value': value, 'unit': 'ratio',
            'availability': availability, 'reason': reason, 'heads': heads or [],
            'chart_series_key': market.lower() if chart_series_key == 'domestic' else chart_series_key, 'chart_transform': transform,
            'derivation': derivation}


def _finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def _stage(payload, item):
    if not item.get('carried_forward'):
        return payload.get('issue_stage')
    variant = item.get('variant', '')
    if variant == '장후 검증 기준선 · 종가 유지':
        return 'postclose'
    if variant == '국내 종가와 미국 시장 수익률 · Ridge':
        return 'premarket'
    return None


def _model(key, payload, market, item, rows, *, status=None, provenance='unavailable'):
    context = item.get('context', {}) if key == 'A' else {}
    return {'key': key, 'label': f'모델 {key}', 'market': market,
            'status': status or item.get('status') or payload.get('status') or 'ready',
            'asof_date': context.get('asof_date') or item.get('asof_date') or payload.get('asof_date'),
            'target_date': context.get('target_date') or item.get('target_date') or payload.get('target_date'),
            'issue_stage': context.get('stage') or _stage(payload, item),
            'prediction_cutoff': context.get('prediction_cutoff') or item.get('prediction_cutoff') or payload.get('prediction_cutoff'),
            'run_id': None if item.get('carried_forward') else payload.get('run_id'),
            'publication_id': None if item.get('carried_forward') else payload.get('publication_id'),
            'carried_forward': bool(item.get('carried_forward')),
            'forecast_id': (item.get('forecast_id') or (item.get('forecast') or {}).get('forecast_id')) if key == 'A' else None,
            'provenance': provenance, 'rows': rows}


def build_export(a, b, c, reconstructed=None):
    reconstructed = reconstructed or {}
    models = []
    for market in MARKETS:
        a_item = next((r for r in a.get('markets', []) if r.get('market') == market), None)
        if a_item:
            forecast = a_item.get('forecast') or {}
            selected = (forecast.get('selected') or {}).get('features') or []
            not_ready = not forecast.get('issued_at') or _finite(forecast.get('expected_return')) is None
            a_values = reconstructed.get(('A', market), {}) if not not_ready else {}
            context = a_item.get('context') or {}
            rows = [_row(name, A_LABELS.get(name, name), context.get('asof_date'), market, value=_finite(a_values.get(name)),
                         availability='selected_not_ready' if not_ready else 'reconstructed' if _finite(a_values.get(name)) is not None else 'unavailable',
                         reason='선택되었으나 예측이 준비되지 않았습니다.' if not_ready else None if _finite(a_values.get(name)) is not None else '게시된 입력 벡터를 확인할 수 없습니다.',
                         chart_series_key={'us_spx_return_1d': 'sp500', 'us_nasdaq_return_1d': 'nasdaq',
                                           'us_sox_return_1d': 'sox', 'us_vix_level': 'vix',
                                           'us_vix_return_1d': 'vix', 'sox_nasdaq_spread_1d': None}.get(
                                               name, 'domestic' if name in {'return_1d', 'return_5d', 'return_20d'} else None),
                         transform={'return_1d':'return1', 'return_5d':'return5', 'return_20d':'return20', 'us_spx_return_1d':'return1', 'us_nasdaq_return_1d':'return1',
                                    'us_sox_return_1d':'return1', 'us_vix_level':'level', 'us_vix_return_1d':'return1'}.get(name)) for name in selected]
            for row in rows:
                if row['key'] == 'us_vix_level':
                    row['unit'] = 'index'
            models.append(_model('A', a, market, a_item, rows, status=forecast.get('status', 'unavailable'),
                                 provenance='frozen' if a_values else 'unavailable'))
        for key, payload in (('B', b), ('C', c)):
            item = next((r for r in payload.get('markets', []) if r.get('market') == market), None)
            if key == 'B' and item is None:
                continue
            trigger = (payload.get('trigger_returns') or {}).get(market) if key == 'C' else None
            if key == 'C' and item is None and trigger is None:
                continue
            item = item or {}
            day = item.get('asof_date') or payload.get('asof_date')
            data = reconstructed.get((key, market), {})
            rows = []
            if key == 'C' and trigger is not None:
                rows.append(_row('trigger_return', '급변 발동 기준 수익률', day, market,
                                 value=_finite(trigger), availability='observed_in_forecast',
                                 heads=['trigger'], transform='return1', derivation='게시된 급변 발동 수익률'))
            if item:
                close = _finite(item.get('close'))
                row = _row('close', '기준 종가', day, market, value=close,
                           availability='observed_in_forecast' if close is not None else 'unavailable',
                           reason=None if close is not None else '게시된 종가가 없습니다.',
                           heads=['close'], transform='level', derivation='예측 게시물에 기록된 종가')
                row['unit'] = 'index'
                rows.append(row)
                # The regime-prior C method uses live volatility, not 1d/5d returns.
                names = list(DOMESTIC) if key == 'B' or item.get('method') != 'regime_prior' else ['volatility_20d']
                if key == 'B' and _stage(payload, item) != 'premarket':
                    names = ['volatility_20d']
                for name in names:
                    label, transform = DOMESTIC[name]
                    value = _finite(data.get(name))
                    rows.append(_row(name, label, day, market, value=value, transform=transform,
                                     availability='reconstructed' if value is not None else 'unavailable',
                                     reason=None if value is not None else '이 게시물과 일치하는 동결 입력을 확인할 수 없습니다.',
                                     heads=(['interval'] if key == 'B' and _stage(payload, item) == 'postclose'
                                            else ['close', 'direction', 'interval'] if key == 'B' and name == 'volatility_20d'
                                            else ['close', 'direction'] if key == 'B' else ['probability']),
                                     derivation=('동결 종가에서 계산; 표본 부족 시 확률 구성 요소는 과거 빈도로 대체될 수 있음'
                                                 if key == 'C' and item.get('method') == 'hist_gradient'
                                                 else '동결된 종가에서 모델 입력 함수를 적용한 값')))
                if key == 'B' and _stage(payload, item) == 'premarket':
                    for symbol, (series, label) in US.items():
                        if symbol == 'KORU' and market != 'KOSPI':
                            continue
                        name = f'{series}_return_1d'
                        value = _finite(data.get(name))
                        rows.append(_row(name, f'{label} 1일 수익률', data.get('us_session_date'), market,
                                         value=value, chart_series_key=series, transform='return1',
                                         availability='reconstructed' if value is not None else 'unavailable',
                                         reason=None if value is not None else '이 게시물과 일치하는 동결 미국 종가를 확인할 수 없습니다.',
                                         heads=['close'] if symbol == 'KORU' else ['close', 'direction'],
                                         derivation='컷오프 이전 확정 미국 거래일 종가 / 직전 거래일 종가 - 1'))
            models.append(_model(key, payload, market, item, rows,
                                 status=payload.get('status') if key == 'C' else None,
                                 provenance='frozen' if data else 'unavailable'))
    return {'schema_version': 'market_chart_model_inputs_v1',
            'generated_at': datetime.now(UTC).isoformat(), 'models': models}



def retain_verified_inputs(current, previous):
    """Preserve previously reconstructed values only for the identical published score."""
    if previous.get('schema_version') != current.get('schema_version'):
        return current
    identity_fields = ('key', 'market', 'asof_date', 'target_date', 'issue_stage', 'prediction_cutoff',
                       'publication_id', 'forecast_id', 'carried_forward')
    for model in current['models']:
        identity = model.get('forecast_id') if model['key'] == 'A' else model.get('publication_id')
        if not identity or model.get('carried_forward'):
            continue
        old = next((item for item in previous.get('models', [])
                    if all(item.get(field) == model.get(field) for field in identity_fields)), None)
        if not old:
            continue
        previous_rows = {row['key']: row for row in old.get('rows', []) if 'key' in row}
        for row in model['rows']:
            old_row = previous_rows.get(row['key'], {})
            if (row['availability'] == 'unavailable' and old_row.get('availability') == 'reconstructed'
                    and _finite(old_row.get('value')) is not None
                    and all(old_row.get(field) == row.get(field)
                            for field in ('chart_series_key', 'chart_transform', 'unit', 'heads'))):
                row.update(value=old_row['value'], date=old_row.get('date'),
                           availability='reconstructed', reason=None)
                model['provenance'] = 'frozen'
    return current

def reconstruct(prepared_manifest, b, c, a=None, *, replay_completed=False):
    """Recreate B/C live feature rows only for the exact restored publication."""
    if not prepared_manifest:
        return {}
    import pandas as pd
    from roboquant.index_forecast import automation_state as state
    from roboquant.index_forecast.bc_morning import _domestic_features, us_return_features, MARKET_SYMBOLS, NY, BUFFER
    from roboquant.index_forecast.market_closes import effective_index_eod_frame, apply_live_revisions
    from roboquant.index_forecast.postclose import PROTOCOL, verify_market_closes
    from roboquant.index_forecast.reliable_inputs import session_schedule, utc_timestamp
    from roboquant.index_forecast.reliable_pipeline import build_reliable_price_frame, _frame_hash
    from roboquant.index_forecast.dataset import PRICE_FEATURES
    from roboquant.index_forecast.us_market_inputs import us_close_schedule
    from run_bc_cpu_afterclose import daily_inputs, HISTORY_START

    current = datetime.now(UTC)
    if replay_completed:
        try:
            receipt = json.loads((Path(prepared_manifest).parent / 'completion.json').read_text())
            completed = utc_timestamp(receipt['completed_at'])
            if pd.isna(completed) or completed.tzinfo is None or completed > current:
                return {}
            current = completed.to_pydatetime()
        except (OSError, ValueError, KeyError, TypeError):
            return {}
    restored = state.restore_inputs(Path(prepared_manifest).parent, now=current,
                                    code_hash=state.source_fingerprint(), issuance_protocol=PROTOCOL)
    if restored['status'] != 'ready':
        return {}
    manifest, files = restored['manifest'], restored['paths']
    required = {'sqlite', 'market_closes', 'market_closes_receipt'}
    if not required <= files.keys():
        return {}
    output = {}
    cutoff = utc_timestamp(manifest['prepared_at'])
    prices = effective_index_eod_frame(files['sqlite'], cutoff=cutoff)
    live_prices = apply_live_revisions(prices)
    # A records a hash of the exact live feature frame, including the row index.
    # Only the price group is reconstructed; other feature groups remain unknown.
    for item in (a or {}).get('markets', []):
        context, forecast = item.get('context') or {}, item.get('forecast') or {}
        selected = forecast.get('selected') or {}
        market = item.get('market')
        if (market not in MARKETS or not forecast.get('issued_at')
                or _finite(forecast.get('expected_return')) is None
                or context.get('prediction_cutoff') != manifest['prepared_at']
                or selected.get('group') != 'price' or selected.get('features') != PRICE_FEATURES):
            continue
        frame = build_reliable_price_frame(prices.loc[prices.index_code == market], forecast_context=context)
        live = build_reliable_price_frame(live_prices.loc[live_prices.index_code == market], forecast_context=context)
        mask = frame.asof_date == context['asof_date']
        live_row = live.loc[live.asof_date == context['asof_date']]
        if not mask.any() or live_row.empty:
            continue
        frame.loc[mask, ['close', *PRICE_FEATURES]] = live_row[['close', *PRICE_FEATURES]].to_numpy()
        latest = frame.loc[mask, ['asof_date', *PRICE_FEATURES]]
        if _frame_hash(latest) == item.get('feature_hash'):
            output[('A', market)] = latest.iloc[0][PRICE_FEATURES].to_dict()
    for key, payload in (('B', b), ('C', c)):
        if payload.get('publication_id') != manifest['manifest_hash'] or payload.get('prediction_cutoff') != manifest['prepared_at']:
            continue
        cutoff = utc_timestamp(manifest['prepared_at'])
        anchor, target = payload.get('asof_date'), payload.get('target_date')
        if not anchor or not target:
            continue
        sessions, _ = session_schedule(HISTORY_START, anchor)
        if sessions.empty or sessions.date.iloc[-1] != anchor:
            continue
        prices = effective_index_eod_frame(files['sqlite'], cutoff=cutoff)
        quotes = verify_market_closes(files['market_closes'], files['market_closes_receipt'])['markets']
        _, histories = daily_inputs(prices, quotes, sessions, cutoff)
        observations = pd.read_csv(files['us_csv']) if 'us_csv' in files else pd.DataFrame()
        for item in payload.get('markets', []):
            market = item.get('market')
            if (market not in histories or item.get('carried_forward') or
                item.get('asof_date') != anchor or item.get('prediction_cutoff') != payload.get('prediction_cutoff')):
                continue
            levels, receipts = histories[market]
            if _finite(item.get('close')) != _finite(levels[-1]):
                continue
            # The C CPU and B morning implementations share these exact domestic formulas.
            domestic, _, _, cutoffs, _ = _domestic_features(levels, sessions, target, receipts, cutoff)
            values = dict(zip(DOMESTIC, domestic[-1], strict=True))
            if key == 'B' and payload.get('issue_stage') == 'premarket':
                symbols = MARKET_SYMBOLS[market]
                us = us_return_features(observations, cutoffs, symbols)[-1]
                values.update({f'{US[s][0]}_return_1d': v for s, v in zip(symbols, us, strict=True)})
                calendar = us_close_schedule((cutoff.tz_convert(NY).date() - timedelta(days=14)).isoformat(), cutoff.tz_convert(NY).date().isoformat())
                calendar = calendar.loc[calendar.symbol.eq('^GSPC')].copy()
                release = pd.to_datetime(calendar.session_close_at, utc=True) + BUFFER
                ready = calendar.loc[release <= cutoff]
                if not ready.empty:
                    values['us_session_date'] = ready.session_date.iloc[-1]
            output[(key, market)] = values
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared-manifest', type=Path)
    parser.add_argument('--replay-completed', action='store_true',
                        help='verify an archived manifest at its completed receipt time')
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'docs/data')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/data/market-chart-model-inputs.json')
    args = parser.parse_args()
    def read(name):
        path = args.data_dir / name
        return json.loads(path.read_text()) if path.exists() else {}
    a, b, c = (read(name) for name in ('reliable_index_forecast.json', 'model_b_forecast.json', 'model_c_forecast.json'))
    try:
        inputs = reconstruct(args.prepared_manifest, b, c, a, replay_completed=args.replay_completed)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Warning: frozen input reconstruction unavailable: {exc}', file=sys.stderr)
        inputs = {}
    result = build_export(a, b, c, inputs)
    if args.output.exists():
        try:
            result = retain_verified_inputs(result, json.loads(args.output.read_text()))
        except (OSError, ValueError, TypeError, KeyError):
            pass
    from roboquant.index_forecast.automation_state import atomic_json
    atomic_json(args.output, result, indent=2)
    print(f'Wrote {args.output} ({len(result["models"])} market/model entries)')


if __name__ == '__main__':
    main()

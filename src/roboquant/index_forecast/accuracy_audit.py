"""Read-only, point-in-time accuracy audit of archived and published forecasts.

The caller must supply the independently verified archive and official session schedule.
Ledger hashes show integrity, not authenticity of historical publication timestamps.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import date as calendar_date

import pandas as pd


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _time(value):
    try:
        value = pd.Timestamp(value)
        if pd.isna(value) or value.tzinfo is None:
            return None
        return value.tz_convert('UTC')
    except (TypeError, ValueError):
        return None


def _number(value):
    try:
        return math.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def _close(a, b, *, atol=1e-7, rtol=1e-6):
    return _number(a) and _number(b) and math.isclose(float(a), float(b), abs_tol=atol, rel_tol=rtol)


def _metrics(rows):
    valid = [r for r in rows if r['status'] == 'scored']
    n = len(valid)
    result = {'n': n, 'price_mae_points': None, 'price_rmse_points': None,
              'price_bias_points': None, 'return_mae_percentage_points': None,
              'brier': None, 'log_loss': None, 'probability_direction_accuracy': None,
              'return_direction_accuracy': None, 'interval_coverage': None,
              'probability_n': 0, 'interval_n': 0}
    if not n:
        return result
    errors = [r['expected_index'] - r['actual_close'] for r in valid]
    result.update(price_mae_points=sum(map(abs, errors)) / n,
                  price_rmse_points=math.sqrt(sum(x*x for x in errors) / n),
                  price_bias_points=sum(errors) / n,
                  return_mae_percentage_points=100 * sum(abs(r['expected_return'] - r['actual_return']) for r in valid) / n,
                  return_direction_accuracy=sum((r['expected_return'] > 0) == (r['actual_return'] > 0) for r in valid) / n)
    probs = [r for r in valid if _number(r.get('p_up')) and 0 <= r['p_up'] <= 1]
    if probs:
        result['probability_n'] = len(probs)
        result['brier'] = sum((r['p_up'] - int(r['actual_return'] > 0))**2 for r in probs) / len(probs)
        result['log_loss'] = sum(-math.log(max(1e-12, min(1-1e-12, r['p_up'] if r['actual_return'] > 0 else 1-r['p_up']))) for r in probs) / len(probs)
        result['probability_direction_accuracy'] = sum((r['p_up'] >= .5) == (r['actual_return'] > 0) for r in probs) / len(probs)
    intervals = [r for r in valid if _number(r.get('range_low')) and _number(r.get('range_high')) and r['range_low'] <= r['range_high']]
    if intervals:
        result['interval_n'] = len(intervals)
        result['interval_coverage'] = sum(r['range_low'] <= r['actual_return'] <= r['range_high'] for r in intervals) / len(intervals)
    return result


def audit_recent_accuracy(prices: pd.DataFrame, archive: dict, ledger: dict, *, sessions: pd.DataFrame,
                          as_of, legacy_rows: pd.DataFrame | None = None, days: int = 30) -> dict:
    now = _time(as_of)
    if now is None or days < 1:
        raise ValueError('as_of must be timezone-aware and days positive')
    payload = {k: v for k, v in ledger.items() if k != 'history_hash'}
    if ledger.get('history_hash') != _hash(payload) or ledger.get('schema_version') != 'forward_history_v1':
        raise ValueError('invalid ledger history_hash or schema')
    schedule = {}
    for row in sessions.to_dict('records'):
        date = str(row['date'])[:10]
        op, cl = _time(row['open_at']), _time(row['close_at'])
        if not op or not cl or op >= cl or date in schedule:
            raise ValueError('invalid or duplicate session schedule')
        schedule[date] = (op, cl)
    dates = sorted(schedule)
    completed = [d for d in dates if schedule[d][1] <= now][-days:]
    preceding = {d: dates[i-1] if i else None for i, d in enumerate(dates)}
    price_map = defaultdict(list)
    for row in prices.to_dict('records'):
        price_map[(str(row['index_code']).upper(), str(row['session_date'])[:10])].append(row)

    def get_price(market, date):
        rows = price_map.get((market, date), [])
        if not rows:
            return None, 'missing_price'
        for r in rows:
            if r.get('conflict') is True or r.get('is_conflict') is True or r.get('status') == 'conflict':
                return None, 'conflicting_price'
            if r.get('status') == 'price_not_observed_as_of':
                return None, 'price_not_observed_as_of'
            if ('is_final' in r and pd.notna(r['is_final']) and not r['is_final']) or ('final' in r and pd.notna(r['final']) and not r['final']):
                return None, 'nonfinal_price'
            for clock in ('observed_at', 'published_at'):
                if clock in r and pd.notna(r[clock]) and (_time(r[clock]) is None or _time(r[clock]) > now):
                    return None, 'price_not_observed_as_of'
            if not _number(r['close']) or float(r['close']) <= 0:
                return None, 'invalid_price'
        if any(not _close(rows[0]['close'], r['close']) for r in rows[1:]):
            return None, 'conflicting_price'
        return float(rows[0]['close']), None

    def base_row(market, target):
        prior = preceding[target]
        baseline, err1 = get_price(market, prior)
        actual, err2 = get_price(market, target)
        return {'market': market, 'asof_date': prior, 'target_date': target,
                'official_open_at': schedule[target][0].isoformat(), 'official_close_at': schedule[target][1].isoformat(),
                'baseline_close': baseline, 'actual_close': actual, 'status': err1 or err2 or 'no_forecast', 'flags': []}

    def evaluate(base, forecast, actual_return, *, live=False):
        result = dict(base, flags=list(base['flags']))
        if result['status'] != 'no_forecast':
            return result
        if forecast.get('asof_date') != result['asof_date']:
            result['status'] = 'non_adjacent_asof'
            return result
        cutoff = _time(forecast.get('prediction_cutoff'))
        if cutoff is None or result['asof_date'] not in schedule or not schedule[result['asof_date']][1] <= cutoff < schedule[result['target_date']][0]:
            result['status'] = 'invalid_prediction_cutoff'
            return result
        if not live:
            fit_cutoff = _time(forecast.get('fit_cutoff'))
            if 'fit_cutoff' in forecast and (fit_cutoff is None or fit_cutoff > cutoff):
                result['status'] = 'invalid_fit_evidence'
                return result
            if 'fit_asof' in forecast:
                try:
                    fit_date = calendar_date.fromisoformat(forecast['fit_asof'])
                    asof_date = calendar_date.fromisoformat(result['asof_date'])
                except (ValueError, TypeError):
                    result['status'] = 'invalid_fit_evidence'
                    return result
                if fit_date > asof_date:
                    result['status'] = 'invalid_fit_evidence'
                    return result
        close_stamp = _time(forecast.get('target_close_at'))
        if close_stamp != schedule[result['target_date']][1]:
            result['flags'].append('stored_target_close_mismatch')
        result['stored_label_available_at'] = forecast.get('label_available_at')
        label = _time(forecast.get('label_available_at'))
        if label is None:
            result['status'] = 'label_not_available_as_of'
            return result
        official_close = schedule[result['target_date']][1]
        if label < official_close:
            result['flags'].append('stored_label_before_official_close')
        effective_label = max(label, official_close)
        result['effective_label_available_at'] = effective_label.isoformat()
        if effective_label > now:
            result['status'] = 'label_not_available_as_of'
            return result
        expected = forecast.get('expected_return')
        if not _number(expected) or float(expected) <= -1 or not _number(actual_return) or float(actual_return) <= -1:
            result['status'] = 'invalid_return'
            return result
        if live:
            result['independent_baseline_close'] = result['baseline_close']
            result['baseline_close'] = float(forecast['issued_baseline_close'])
        expected_actual = result['actual_close'] / result['baseline_close'] - 1
        return_tolerance = 1e-6 + (.005 / result['baseline_close'] if live else 0)
        if not _close(actual_return, expected_actual, atol=return_tolerance, rtol=1e-5):
            result['status'] = 'actual_return_mismatch'
            return result
        result.update(status='scored', expected_return=float(expected), actual_return=float(actual_return),
                      expected_index=result['baseline_close'] * (1 + float(expected)),
                      p_up=float(forecast['p_up']) if _number(forecast.get('p_up')) else None,
                      range_low=float(forecast['range_low']) if _number(forecast.get('range_low')) else None,
                      range_high=float(forecast['range_high']) if _number(forecast.get('range_high')) else None,
                      prediction_cutoff=forecast.get('prediction_cutoff'))
        for key in ('issued_at', 'fit_asof', 'forecast_id', 'fit_artifact_hash', 'fit_snapshot_hash', 'run_id'):
            if forecast.get(key) is not None:
                result[key] = forecast[key]
        return result

    markets = {}
    for market in ('KOSPI', 'KOSDAQ'):
        daily = [base_row(market, d) for d in completed]
        archived = next((x for x in archive.get('markets', []) if x.get('market') == market), {})
        evaluation = archived.get('evaluation') or {}
        sources = {'selected': evaluation.get('observations', [])}
        sources.update(evaluation.get('candidate_observations') or {})
        historical = []
        for name, records in sources.items():
            by_target = defaultdict(list)
            for r in records:
                by_target[r.get('target_date')].append(r)
            rows = []
            for base in daily:
                found = by_target.get(base['target_date'], [])
                if len(found) > 1 and any(x != found[0] for x in found[1:]):
                    rows.append(dict(base, status='conflicting_historical_observations'))
                elif found:
                    rows.append(evaluate(base, found[0], found[0].get('actual_return')))
                else:
                    rows.append(dict(base))
            baseline_rows = {}
            for name2 in ('flat', 'mean'):
                converted = []
                for base, scored in zip(daily, rows, strict=True):
                    found = by_target.get(base['target_date'], [])
                    if scored['status'] != 'scored' or not found or (len(found) > 1 and any(x != found[0] for x in found[1:])):
                        continue
                    r = found[0]
                    pred = {'expected_return': 0., 'p_up': None, 'range_low': None, 'range_high': None} if name2 == 'flat' else r.get('baselines', {}).get('mean', {})
                    if _number(pred.get('expected_return')):
                        values = {key: pred.get(key) for key in ('expected_return', 'p_up', 'range_low', 'range_high')}
                        converted.append(evaluate(base, {**r, **values}, r.get('actual_return')))
                baseline_rows[name2] = _metrics(converted)
            historical.append({'model': name, 'metrics': _metrics(rows), 'baselines': baseline_rows, 'daily': rows})
        cohorts = []
        groups = defaultdict(list)
        for f in ledger.get('forecasts', []):
            if f.get('market') == market:
                groups[(f.get('role'), f.get('protocol_hash'))].append(f)
        for (role, protocol), forecasts in sorted(groups.items(), key=lambda x: str(x[0])):
            by_target = defaultdict(list)
            for f in forecasts:
                by_target[f.get('target_date')].append(f)
            rows = []
            for base in daily:
                candidates = by_target.get(base['target_date'], [])
                if not candidates:
                    rows.append(dict(base))
                    continue
                known = [f for f in candidates if _time(f.get('issued_at')) is None or _time(f['issued_at']) <= now]
                if known:
                    candidates = known
                r = dict(base, flags=list(base['flags']))
                f = candidates[0]
                if any(x != f for x in candidates[1:]):
                    r['status'] = 'conflicting_forecast_key'
                    rows.append(r)
                    continue
                if len(candidates) > 1:
                    r['flags'].append('duplicate_forecast_deduplicated')
                issue, cutoff = _time(f.get('issued_at')), _time(f.get('prediction_cutoff'))
                key = _hash({k: f.get(k) for k in ('market', 'target_date', 'protocol_hash', 'role')})
                registrations = [p for p in ledger.get('protocols', []) if p.get('protocol_hash') == protocol and _hash(p.get('protocol')) == protocol and _time(p.get('registered_at')) is not None and issue is not None and _time(p['registered_at']) < issue]
                attempts = [a for a in ledger.get('attempts', []) if a.get('run_id') == f.get('run_id') and a.get('protocol_hash') == protocol and a.get('status') == 'verified' and f'{market}:{role}' in a.get('accepted_roles', []) and _time(a.get('attempted_at')) is not None and issue is not None and issue <= _time(a['attempted_at']) <= now]
                if f.get('replay') or f.get('carried_forward'):
                    r['status'] = 'replay_forecast' if f.get('replay') else 'carried_forward_forecast'
                elif role != 'incumbent_reference' and f.get('fit_provenance') != 'live_captured':
                    r['status'] = 'non_live_fit_provenance'
                elif f.get('forecast_key') != key or f.get('outcome_basis') != 'issued_close_v1':
                    r['status'] = 'invalid_forecast_identity'
                elif not issue or not cutoff or not cutoff <= issue < schedule[base['target_date']][0] or issue > now:
                    r['status'] = 'invalid_issuance_time'
                elif not registrations or not attempts:
                    r['status'] = 'unverified_publication_or_protocol'
                elif not _close(f.get('issued_baseline_close'), base['baseline_close'], atol=.005, rtol=0):
                    r['status'] = 'issued_baseline_price_mismatch' if base['baseline_close'] is not None else base['status']
                else:
                    outcomes = [o for o in ledger.get('outcomes', []) if o.get('forecast_key') == key]
                    known_outcomes = [o for o in outcomes if _time(o.get('observed_at')) is None or _time(o['observed_at']) <= now]
                    if known_outcomes:
                        outcomes = known_outcomes
                    if not outcomes:
                        r['status'] = 'missing_mature_outcome'
                    elif len(outcomes) > 1 and any(o != outcomes[0] for o in outcomes[1:]):
                        r['status'] = 'conflicting_outcome_key'
                    else:
                        o = outcomes[0]
                        if any(o.get(k) != f.get(k) for k in ('market', 'asof_date', 'target_date', 'protocol_hash', 'role', 'forecast_key', 'outcome_basis')) or not _close(o.get('issued_baseline_close'), f['issued_baseline_close']):
                            r['status'] = 'outcome_identity_mismatch'
                        elif o.get('mature') is not True:
                            r['status'] = 'immature_outcome'
                        elif not _time(o.get('observed_at')) or _time(o['observed_at']) > now:
                            r['status'] = 'outcome_not_observed_as_of'
                        elif not _time(o.get('target_observed_at')) or _time(o['target_observed_at']) < schedule[base['target_date']][1]:
                            r['status'] = 'target_observed_before_official_close'
                        elif _time(o['target_observed_at']) > _time(o['observed_at']):
                            r['status'] = 'target_received_after_outcome_observation'
                        elif (_time(o.get('label_available_at')) is not None and
                              _time(o['observed_at']) < max(_time(o['label_available_at']), schedule[base['target_date']][1])):
                            r['status'] = 'outcome_observed_before_effective_label'
                        elif not _close(o.get('target_close'), base['actual_close'], atol=.005, rtol=0):
                            r['status'] = 'target_price_mismatch' if base['actual_close'] is not None else base['status']
                        elif not _close(o.get('actual_return'), float(o['target_close']) / float(f['issued_baseline_close']) - 1):
                            r['status'] = 'outcome_return_mismatch'
                        else:
                            if _time(o.get('target_close_at')) != schedule[base['target_date']][1] and 'stored_target_close_mismatch' not in r['flags']:
                                r['flags'].append('stored_outcome_close_mismatch')
                            r = evaluate(r, {**f, 'label_available_at': o.get('label_available_at')}, o.get('actual_return'), live=True)
                rows.append(r)
            paired_baselines = {}
            for baseline_name in ('flat', 'mean'):
                pairs = []
                for row in rows:
                    if row['status'] != 'scored':
                        continue
                    forecast = next(f for f in by_target[row['target_date']] if _time(f.get('issued_at')) is not None and _time(f['issued_at']) <= now)
                    baseline = {'expected_return': 0.} if baseline_name == 'flat' else (forecast.get('baselines') or {}).get('mean', {})
                    if not _number(baseline.get('expected_return')):
                        continue
                    pred = float(baseline['expected_return'])
                    pairs.append({**row, 'expected_return': pred, 'expected_index': row['baseline_close'] * (1 + pred),
                                  'p_up': float(baseline['p_up']) if _number(baseline.get('p_up')) else None,
                                  'range_low': float(baseline['range_low']) if _number(baseline.get('range_low')) else None,
                                  'range_high': float(baseline['range_high']) if _number(baseline.get('range_high')) else None})
                paired_baselines[baseline_name] = _metrics(pairs)
            cohorts.append({'role': role, 'protocol_hash': protocol, 'metrics': _metrics(rows),
                            'baselines': paired_baselines, 'daily': rows})
        legacy_n = 0
        if legacy_rows is not None and not legacy_rows.empty and 'target_date' in legacy_rows:
            mcol = 'market' if 'market' in legacy_rows else 'index_code' if 'index_code' in legacy_rows else None
            if mcol:
                legacy_n = int(((legacy_rows[mcol].astype(str).str.upper() == market) & legacy_rows['target_date'].astype(str).str[:10].isin(completed)).sum())
        markets[market] = {'daily': daily, 'historical': historical, 'cohorts': cohorts,
                           'legacy_recent_rows': legacy_n}
    return {'schema_version': 'recent_accuracy_audit_v1', 'as_of': now.isoformat(),
            'window': {'requested_sessions': days, 'completed_sessions': len(completed),
                       'start_date': completed[0] if completed else None, 'end_date': completed[-1] if completed else None},
            'markets': markets,
            'limitations': ['Historical walk-forward results are retrospective, not live publications.',
                'Protocol and role cohorts are reported separately; hashes establish integrity, not timestamp authenticity.',
                'Price bias is predicted minus actual; return errors are percentage points.']}

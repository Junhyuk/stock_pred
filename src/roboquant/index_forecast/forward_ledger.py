"""Append-only, protocol-separated evidence from verified actual publications.

Hashes establish content integrity, not historic timestamp authenticity. Restoring
this ledger is permitted only from our previously recorded trusted repository.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from pathlib import Path

import pandas as pd

TABLES = ('protocols', 'forecasts', 'attempts', 'outcomes', 'outcome_attempts')


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('aware evidence timestamp required')
    return stamp.tz_convert('UTC')


def _finite(value):
    return isinstance(value, (float, int)) and math.isfinite(value)


class ForwardLedger:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            for table in TABLES:
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
                for operation in ('UPDATE', 'DELETE'):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_{operation} BEFORE {operation} ON {table} "
                               "BEGIN SELECT RAISE(ABORT, 'immutable forward evidence'); END")

    def _connection(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _insert(db, table, key, row):
        db.execute(f'INSERT OR IGNORE INTO {table} VALUES (?, ?)', (key, _json(row)))

    def _rows(self, table):
        with self._connection() as db:
            return [json.loads(row[0]) for row in db.execute(f'SELECT payload FROM {table} ORDER BY rowid')]

    def register_protocol(self, protocol, *, registered_at):
        key = _hash(protocol)
        row = {'protocol_hash': key, 'protocol': protocol, 'registered_at': _utc(registered_at).isoformat()}
        with self._connection() as db:
            self._insert(db, 'protocols', key, row)
        return key

    def record_publication(self, output_dir, *, protocol, attempted_at):
        from roboquant.index_forecast.reliable_pipeline import resolve_reliable_publication

        attempted = _utc(attempted_at)
        key = _hash(protocol)
        attempt = {'attempted_at': attempted.isoformat(), 'protocol_hash': key,
                   'status': 'excluded', 'reason': 'unverified_publication', 'accepted_roles': []}
        try:
            payload = resolve_reliable_publication(output_dir)
            attempt['run_id'] = payload.get('run_id')
            manifest = json.loads((Path(output_dir) / 'reliable_publication.json').read_text())
            issued = _utc(manifest['publication_checked_at'])
            if payload.get('status') != 'research_only' or payload.get('replay') or manifest.get('replay'):
                raise ValueError('replay_or_unpublished')
            if issued > attempted:
                raise ValueError('publication_after_observation')
            registrations = [r for r in self._rows('protocols') if r['protocol_hash'] == key]
            if not registrations or _utc(registrations[0]['registered_at']) >= issued:
                raise ValueError('protocol_not_registered_before_issue')
            with self._connection() as db:
                for market in payload.get('markets', []):
                    context = market['context']
                    if not (_utc(context['prediction_cutoff']) <= issued < _utc(context['issuance_deadline'])
                            and issued < _utc(context['target_close_at'])):
                        attempt['reason'] = 'outside_issuance_window'
                        continue
                    if context.get('stage') not in ('premarket_final', 'us_after_close_preopen_research'):
                        attempt['reason'] = 'nonfinal_stage'
                        continue
                    for role, forecast in (('primary', market.get('forecast')),
                                           ('incumbent_reference', market.get('incumbent_reference')),
                                           ('numeric_shadow', market.get('numeric_shadow', {}).get('forecast'))):
                        if not forecast or forecast.get('status') not in ('rejected', 'research_candidate'):
                            continue
                        if not all(_finite(forecast.get(k)) for k in ('expected_return', 'p_up', 'range_low', 'range_high')):
                            continue
                        if not 0 <= forecast['p_up'] <= 1 or forecast['range_low'] > forecast['range_high']:
                            continue
                        if role != 'incumbent_reference' and forecast.get('fit_provenance') != 'live_captured':
                            continue
                        row = {k: forecast.get(k) for k in ('expected_return', 'p_up', 'range_low', 'range_high',
                            'forecast_id', 'fit_provenance', 'fit_snapshot_hash', 'fit_artifact_hash',
                            'scale_at_prediction', 'volatility_regime', 'interval_shadows', 'baselines')}
                        row.update(market=market['market'], role=role, protocol_hash=key,
                            asof_date=context['asof_date'], target_date=context['target_date'],
                            target_close_at=context['target_close_at'], prediction_cutoff=context['prediction_cutoff'],
                            issued_at=issued.isoformat(), run_id=payload['run_id'], production_weight=0.)
                        identity = _hash({k: row[k] for k in ('market', 'target_date', 'protocol_hash', 'role')})
                        self._insert(db, 'forecasts', identity, row)
                        attempt['accepted_roles'].append(f"{market['market']}:{role}")
                if attempt['accepted_roles']:
                    attempt.update(status='verified', reason=None)
        except (ValueError, KeyError, TypeError, OSError) as error:
            attempt['reason'] = str(error)
        with self._connection() as db:
            self._insert(db, 'attempts', _hash(attempt), attempt)
        return attempt

    def record_outcomes(self, observations, *, observed_at):
        stamp = _utc(observed_at)
        forecasts = self._rows('forecasts')
        with self._connection() as db:
            for observation in observations:
                row = {key: observation[key] for key in ('market', 'asof_date', 'target_date',
                    'actual_return', 'target_close_at', 'label_available_at')}
                matched = [f for f in forecasts if all(f[k] == row[k] for k in ('market', 'asof_date', 'target_date'))]
                authoritative = bool(matched) and all(_utc(f['target_close_at']) == _utc(row['target_close_at']) for f in matched)
                mature = (authoritative and _utc(row['target_close_at']) <= stamp and _utc(row['label_available_at']) <= stamp
                          and _finite(row['actual_return']))
                row.update(observed_at=stamp.isoformat(), mature=mature)
                self._insert(db, 'outcome_attempts', _hash(row), row)
                if mature:
                    identity = _hash({key: row[key] for key in ('market', 'asof_date', 'target_date')})
                    self._insert(db, 'outcomes', identity, row)

    def export_history(self):
        data = {'schema_version': 'forward_history_v1', **{table: self._rows(table) for table in TABLES}}
        return {**data, 'history_hash': _hash(data)}

    def import_history(self, history, *, trusted_archive=False):
        data = {k: v for k, v in history.items() if k != 'history_hash'}
        if not trusted_archive or history.get('history_hash') != _hash(data) or data.get('schema_version') != 'forward_history_v1':
            raise ValueError('trusted intact forward archive required')
        identities = {'protocols': ('protocol_hash',), 'forecasts': ('market', 'target_date', 'protocol_hash', 'role'),
                      'outcomes': ('market', 'asof_date', 'target_date')}
        with self._connection() as db:
            for table in TABLES:
                for row in data[table]:
                    key = (row['protocol_hash'] if table == 'protocols' else
                           _hash({k: row[k] for k in identities[table]}) if table in identities else _hash(row))
                    self._insert(db, table, key, row)

    def report(self):
        outcomes = {(r['market'], r['asof_date'], r['target_date']): r for r in self._rows('outcomes')}
        groups = {}
        for row in self._rows('forecasts'):
            groups.setdefault((row['market'], row['role'], row['protocol_hash']), []).append(row)
        markets, cohorts = {}, []
        for (market, role, protocol), rows in groups.items():
            pairs = [(r, outcomes[(market, r['asof_date'], r['target_date'])]) for r in rows
                     if (market, r['asof_date'], r['target_date']) in outcomes]
            metrics = {}
            if pairs:
                n = len(pairs)
                metrics = {'brier': sum((r['p_up'] - int(o['actual_return'] > 0)) ** 2 for r, o in pairs) / n,
                    'mae': sum(abs(r['expected_return'] - o['actual_return']) for r, o in pairs) / n,
                    'coverage80': sum(r['range_low'] <= o['actual_return'] <= r['range_high'] for r, o in pairs) / n,
                    'lower_breach': sum(o['actual_return'] < r['range_low'] for r, o in pairs) / n,
                    'upper_breach': sum(o['actual_return'] > r['range_high'] for r, o in pairs) / n,
                    'interval_score80': sum(r['range_high'] - r['range_low'] + 10 * max(r['range_low'] - o['actual_return'], 0)
                                            + 10 * max(o['actual_return'] - r['range_high'], 0) for r, o in pairs) / n,
                    'mean_width': sum(r['range_high'] - r['range_low'] for r, _ in pairs) / n}
            metrics.update(_probability_metrics(pairs))
            diagnostics = _diagnostics(pairs)
            diagnostics['regimes'] = {}
            for regime in ('high', 'low', 'unknown'):
                subset = [(r, o) for r, o in pairs if (r.get('volatility_regime') or 'unknown') == regime]
                diagnostics['regimes'][regime] = {**_interval_metrics(subset), **_probability_metrics(subset),
                                                **_diagnostics(subset)}
            report = {**diagnostics, 'market': market, 'role': role, 'protocol_hash': protocol, 'issued_n': len(rows),
                      'mature_n': len(pairs), 'metrics': metrics, 'production_weight': 0.,
                      'status': 'collecting_forward_evidence' if len(pairs) < 252 else 'independent_review_required',
                      'minimum_promotion_observations': 252}
            cohorts.append(report)
            markets.setdefault(market, {})[role] = report
        return {'schema_version': 'forward_report_v1', 'markets': markets, 'cohorts': cohorts,
                'attempt_n': len(self._rows('attempts')), 'production_weight': 0.,
                'limitations': 'Protocol cohorts never pooled. 252 is a review minimum, not approval. First observed outcomes are immutable.'}


def _loss(p, y):
    p = min(max(p, 1e-12), 1 - 1e-12)
    return -math.log(p if y > 0 else 1 - p)


def _probability_metrics(pairs):
    if not pairs:
        return {}
    return {'brier': sum((r['p_up'] - int(o['actual_return'] > 0)) ** 2 for r, o in pairs) / len(pairs),
            'mae': sum(abs(r['expected_return'] - o['actual_return']) for r, o in pairs) / len(pairs),
            'log_loss': sum(_loss(r['p_up'], o['actual_return']) for r, o in pairs) / len(pairs),
            'probability_direction_hit': sum((r['p_up'] >= .5) == (o['actual_return'] > 0) for r, o in pairs) / len(pairs),
            'return_direction_hit': sum((r['expected_return'] > 0) == (o['actual_return'] > 0) for r, o in pairs) / len(pairs)}


def _interval_metrics(pairs):
    n = len(pairs)
    if not n:
        return {'mature_n': 0}
    return {'mature_n': n,
        'interval_score80': sum(_interval_loss(r, o['actual_return']) for r, o in pairs) / n,
        'mean_width': sum(r['range_high'] - r['range_low'] for r, _ in pairs) / n,
        'coverage80': sum(r['range_low'] <= o['actual_return'] <= r['range_high'] for r, o in pairs) / n,
        'lower_breach': sum(o['actual_return'] < r['range_low'] for r, o in pairs) / n,
        'upper_breach': sum(o['actual_return'] > r['range_high'] for r, o in pairs) / n}


def _interval_loss(row, actual):
    return row['range_high'] - row['range_low'] + 10 * max(row['range_low'] - actual, 0) + 10 * max(actual - row['range_high'], 0)


def _paired_ci(values):
    if len(values) < 60:
        return {'n': len(values), 'ci95': None, 'reason': 'fewer_than_60_pairs'}
    import numpy as np
    samples = np.asarray(values)
    block = min(20, len(values))
    rng = np.random.default_rng(42)
    starts = rng.integers(0, len(values) - block + 1, size=(2000, math.ceil(len(values) / block)))
    indices = (starts[:, :, None] + np.arange(block)).reshape(2000, -1)[:, :len(values)]
    return {'n': len(values), 'ci95': np.quantile(samples[indices].mean(axis=1), [.025, .975]).tolist(),
            'method': 'paired_moving_block_bootstrap_20_2000_seed42', 'promotion_evidence': len(values) >= 252}


def _diagnostics(pairs):
    bins = []
    for index in range(10):
        selected = [(r, o) for r, o in pairs if min(int(r['p_up'] * 10), 9) == index]
        bins.append({'lower': index / 10, 'upper': (index + 1) / 10, 'n': len(selected),
                     'mean_probability': sum(r['p_up'] for r, _ in selected) / len(selected) if selected else None,
                     'observed_up_rate': sum(o['actual_return'] > 0 for _, o in selected) / len(selected) if selected else None})
    baselines = {}
    names = {name for r, _ in pairs for name in (r.get('baselines') or {})}
    for name in sorted(names):
        selected = [(r, o, r['baselines'][name]) for r, o in pairs
                    if _finite((r.get('baselines') or {}).get(name, {}).get('p_up'))]
        if not selected:
            baselines[name] = {'mature_n': 0}
            continue
        differences = [(r['p_up'] - (o['actual_return'] > 0)) ** 2 - (b['p_up'] - (o['actual_return'] > 0)) ** 2 for r, o, b in selected]
        baselines[name] = {'mature_n': len(selected),
            'brier': sum((b['p_up'] - (o['actual_return'] > 0)) ** 2 for _, o, b in selected) / len(selected),
            'log_loss': sum(_loss(b['p_up'], o['actual_return']) for _, o, b in selected) / len(selected),
            'candidate_minus_baseline_brier': sum(differences) / len(differences),
            'paired_brier_ci': _paired_ci(differences)}
    shadows = {}
    names = {name for r, _ in pairs for name in (r.get('interval_shadows') or {})}
    for name in sorted(names):
        selected = [(r, o, r['interval_shadows'][name]) for r, o in pairs
                    if (r.get('interval_shadows') or {}).get(name, {}).get('status') == 'ready']
        selected = [(r, o, s) for r, o, s in selected if _finite(s.get('range_low')) and _finite(s.get('range_high'))]
        scored = _interval_metrics([(s, o) for _, o, s in selected])
        incumbent = _interval_metrics([(r, o) for r, o, _ in selected])
        differences = [_interval_loss(s, o['actual_return']) - _interval_loss(r, o['actual_return']) for r, o, s in selected]
        scored.update(incumbent_interval_score80=incumbent.get('interval_score80'),
            incumbent_mean_width=incumbent.get('mean_width'), eligible_mature_n=len(pairs),
            paired_interval_score_difference=sum(differences) / len(differences) if differences else None,
            paired_interval_score_ci=_paired_ci(differences))
        shadows[name] = scored
    return {'baselines': baselines, 'interval_shadows': shadows, 'calibration_bins': bins}

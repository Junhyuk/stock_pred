"""Fixed exchange-session fits and data-free, verifiable prediction artifacts.

Full training snapshots are in-memory/private only. Public artifacts contain
fitted parameters and hashes, never training features or labels, and are JSON
data rather than executable pickle objects.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from roboquant.index_forecast.market_calendar import exchange_schedule

PROTOCOL_VERSION = 'exchange_anchor_v1'


def content_hash(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def runtime_fingerprint() -> dict:
    packages = {}
    for name in ('numpy', 'scikit-learn', 'lightgbm', 'exchange_calendars'):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    sources = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
               for name in ('fit_protocol.py', 'reliable.py', 'reliable_intervals.py', 'market_calendar.py',
                            'reliable_inputs.py', 'reliable_pipeline.py', 'economic_calendar.py',
                            'economic_forecast.py', 'economic_sources.py', 'forward_ledger.py',
                            'us_market_inputs.py', 'us_market_pipeline.py')}
    registry = Path(__file__).resolve().parents[3] / 'configs/economic_calendar.yaml'
    sources['economic_registry'] = hashlib.sha256(registry.read_bytes()).hexdigest() if registry.is_file() else None
    return {'packages': packages, 'source_hash': content_hash(sources)}


@lru_cache(maxsize=32)
def _sessions(calendar_name: str, origin: str, year: int) -> tuple[str, ...]:
    _, labels = exchange_schedule(calendar_name, origin, f'{year + 2}-12-31')
    return tuple(labels.strftime('%Y-%m-%d'))


def session_anchor(asof: Any, calendar_name: str = 'XKRX', origin: str = '2019-01-02',
                   step: int = 21) -> dict | None:
    """Anchor on scheduled sessions, independent of input row availability."""
    if step < 1:
        raise ValueError('refit step must be positive')
    day = pd.Timestamp(asof).date().isoformat()
    if day < origin:
        return None
    sessions = _sessions(calendar_name, origin, pd.Timestamp(asof).year)
    position = int(np.searchsorted(sessions, day, side='right')) - 1
    if position < 0:
        return None
    anchor_position = position // step * step
    return {'protocol': PROTOCOL_VERSION, 'calendar_name': calendar_name, 'origin': origin,
            'step': step, 'session_ordinal': position, 'anchor_ordinal': anchor_position,
            'anchor_asof': sessions[anchor_position],
            'next_refit_asof': sessions[anchor_position + step],
            'calendar_prefix_hash': hashlib.sha256('|'.join(sessions[:anchor_position + 1]).encode()).hexdigest(),
            'asof_is_session': sessions[position] == day}


def make_fit_snapshot(data: pd.DataFrame, features: list[str], anchor: dict,
                      settings: dict, *, provenance: str = 'retrospective') -> dict:
    """Freeze anchor-time eligibility before any model preprocessing is fitted."""
    day = anchor['anchor_asof']
    current = data.loc[data['_asof_label'] == day]
    cutoff = anchor.get('prediction_cutoff')
    if anchor.get('grid_cutoff_missing'):
        cutoff = None
        current = current.iloc[0:0]
    if cutoff is None and not current.empty:
        cutoff = current.iloc[0]['_cutoff'].isoformat()
    metadata = {k: anchor[k] for k in ('protocol', 'calendar_name', 'origin', 'step',
                                      'anchor_ordinal', 'anchor_asof', 'next_refit_asof', 'calendar_prefix_hash')}
    metadata.update(features=list(features), settings_hash=content_hash(settings),
                    provenance=provenance, runtime=runtime_fingerprint(),
                    training_data_provenance='retrospective_input_reconstruction; original_publications_unverified')
    if cutoff is None:
        return {**metadata, 'status': 'not_ready', 'reason': 'anchor_row_missing'}
    cutoff = pd.Timestamp(cutoff)
    sessions = _sessions(anchor['calendar_name'], anchor['origin'], pd.Timestamp(day).year)
    purge = int(settings['purge_days'])
    boundary_position = anchor['anchor_ordinal'] - purge
    if boundary_position < 0:
        _, earlier = exchange_schedule(anchor['calendar_name'],
            pd.Timestamp(day) - pd.Timedelta(days=max(366, 4 * (purge + 10))), day)
        if len(earlier) <= purge:
            return {**metadata, 'status': 'not_ready', 'reason': 'insufficient_anchor_history'}
        boundary = earlier[-purge - 1].date().isoformat()
    else:
        boundary = sessions[boundary_position]
    eligible = data.loc[(data['_asof_label'] < boundary) & (data['_available'] <= cutoff)
                        & (data['_target'] <= cutoff) & (data['_cutoff'] <= cutoff)].copy()
    if 'feature_available_at' in eligible:
        receipt = pd.to_datetime(eligible['feature_available_at'], utc=True, errors='coerce')
        eligible = eligible.loc[receipt.notna() & (receipt <= eligible['_cutoff'])]
    values = eligible[features].apply(pd.to_numeric, errors='coerce').to_numpy(dtype=float)
    eligible = eligible.loc[np.isfinite(values).all(axis=1)]
    metadata.update(fit_cutoff=cutoff.isoformat(), purge_boundary=boundary, train_n=len(eligible))
    if len(eligible) < settings['initial_train_days']:
        return {**metadata, 'status': 'not_ready', 'reason': 'insufficient_anchor_training_rows'}
    metadata['train_last_asof'] = eligible['_asof'].iloc[-1].isoformat()
    columns = list(dict.fromkeys(['_asof_label', '_asof', '_cutoff', '_available', '_target',
                                  'label_return', *features,
                                  *(['feature_available_at'] if 'feature_available_at' in eligible else [])]))
    frozen = eligible[columns].copy()
    for column in ('_asof', '_cutoff', '_available', '_target'):
        frozen[column] = frozen[column].map(lambda value: value.isoformat())
    if 'feature_available_at' in frozen:
        frozen['feature_available_at'] = pd.to_datetime(frozen['feature_available_at'], utc=True).map(
            lambda value: value.isoformat())
    payload = {**metadata, 'status': 'ready', 'rows': frozen.to_dict(orient='records')}
    payload['snapshot_hash'] = content_hash(payload)
    return payload


def restore_fit_snapshot(snapshot: dict, features: list[str], settings: dict) -> pd.DataFrame:
    payload = {k: v for k, v in snapshot.items() if k != 'snapshot_hash'}
    if content_hash(payload) != snapshot.get('snapshot_hash'):
        raise ValueError('fit snapshot hash mismatch')
    if snapshot.get('settings_hash') != content_hash(settings):
        raise ValueError('fit snapshot settings mismatch')
    if snapshot.get('runtime') != runtime_fingerprint():
        raise ValueError('fit snapshot runtime/source mismatch')
    if snapshot.get('features') != list(features) or snapshot.get('status') != 'ready':
        raise ValueError('fit snapshot feature/status mismatch')
    frame = pd.DataFrame(snapshot['rows'])
    for column in ('_asof', '_cutoff', '_available', '_target'):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame


def _export_estimator(pipeline) -> dict | None:
    if pipeline is None:
        return None
    imputer = pipeline.steps[0][1]
    result = {'imputer': imputer.statistics_.tolist()}
    estimator = pipeline.steps[-1][1]
    if hasattr(estimator, 'booster_'):
        result.update(kind='lightgbm', model=estimator.booster_.model_to_string())
    else:
        scaler = pipeline.steps[1][1]
        result.update(kind='linear', mean=scaler.mean_.tolist(), scale=scaler.scale_.tolist(),
                      coef=estimator.coef_.tolist(), intercept=np.asarray(estimator.intercept_).tolist())
    return result


def make_model_artifact(snapshot: dict, models: dict) -> dict:
    """Export only fitted parameters; deliberately exclude snapshot training rows."""
    metadata = {k: v for k, v in snapshot.items() if k != 'rows'}
    payload = {'schema_version': 'fit_model_artifact_v1', 'snapshot': metadata,
               'models': {family: {'regressor': _export_estimator(model['regressor']),
                                   'classifier': _export_estimator(model['classifier']),
                                   'up_rate': model['up_rate']} for family, model in models.items()}}
    return {**payload, 'artifact_hash': content_hash(payload)}


def validate_model_artifact(artifact: dict, features: list[str], settings: dict,
                            anchor_asof: str, *, require_live: bool = False,
                            prediction_cutoff=None) -> None:
    if content_hash({k: v for k, v in artifact.items() if k != 'artifact_hash'}) != artifact.get('artifact_hash'):
        raise ValueError('fit model artifact hash mismatch')
    snapshot = artifact['snapshot']
    if prediction_cutoff is not None and pd.Timestamp(snapshot['fit_cutoff']) > pd.Timestamp(prediction_cutoff):
        raise ValueError('fit model artifact unavailable at prediction cutoff')
    if (snapshot['features'] != list(features) or snapshot['settings_hash'] != content_hash(settings)
            or snapshot['anchor_asof'] != anchor_asof):
        raise ValueError('fit model artifact protocol mismatch')
    if snapshot.get('runtime') != runtime_fingerprint():
        raise ValueError('fit model artifact runtime/source mismatch')
    current_anchor = session_anchor(anchor_asof, snapshot['calendar_name'], snapshot['origin'], snapshot['step'])
    if (current_anchor is None or current_anchor['calendar_prefix_hash'] != snapshot['calendar_prefix_hash']
            or current_anchor['anchor_asof'] != anchor_asof):
        raise ValueError('fit model artifact calendar mismatch')
    if require_live and snapshot['provenance'] != 'live_captured':
        raise ValueError('missing_live_anchor_snapshot')


def _predict_estimator(state: dict, x: np.ndarray, *, probability: bool = False) -> float:
    values = np.where(np.isnan(x), np.asarray(state['imputer']), x)
    if state['kind'] == 'lightgbm':
        from lightgbm import Booster
        return float(Booster(model_str=state['model']).predict(values)[0])
    standardized = (values - np.asarray(state['mean'])) / np.asarray(state['scale'])
    raw = float((standardized @ np.asarray(state['coef']).reshape(-1))
                .reshape(-1)[0] + np.asarray(state['intercept']).reshape(-1)[0])
    if probability:
        return float(1 / (1 + np.exp(-np.clip(raw, -700, 700))))
    return raw


def predict_model_artifact(artifact: dict, family: str, x: np.ndarray) -> tuple[float, float]:
    bases = ('linear', 'lightgbm') if family == 'ensemble' else (family,)
    predictions = []
    for base in bases:
        model = artifact['models'][base]
        predictions.append((_predict_estimator(model['regressor'], x),
                            model['up_rate'] if model['classifier'] is None else
                            _predict_estimator(model['classifier'], x, probability=True)))
    return tuple(map(float, np.mean(predictions, axis=0)))


def artifact_path(directory: str | Path, market: str, anchor_asof: str) -> Path:
    if not re.fullmatch(r'[A-Z0-9_]+', market):
        raise ValueError('invalid artifact market identifier')
    day = pd.Timestamp(anchor_asof).date().isoformat()
    return Path(directory) / market / f'{day}.json'


def save_model_artifact(directory: str | Path, market: str, artifact: dict) -> dict:
    """First writer wins; no overwrite, including when a later capture differs."""
    path = artifact_path(directory, market, artifact['snapshot']['anchor_asof'])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(artifact, sort_keys=True, separators=(',', ':'), allow_nan=False)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return json.loads(path.read_text())
    finally:
        temporary.unlink(missing_ok=True)

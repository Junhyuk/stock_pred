"""Separate after-close research issuance clocks and completion identities."""
from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

PROTOCOL = 'after_close_daily_v1'
LEGACY_PROTOCOL = 'premarket_v1'


def historical_cutoff(close, calendar_name='XKRX') -> pd.Timestamp:
    """First scheduled KST weekday18:00 after this verified exchange close."""
    stamp = pd.Timestamp(close).tz_convert('Asia/Seoul')
    from datetime import timedelta
    candidate = stamp.to_pydatetime().replace(hour=18, minute=0, second=0, microsecond=0)
    if candidate < stamp:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return pd.Timestamp(candidate).tz_convert('UTC')


def morning_cutoff(close) -> pd.Timestamp:
    """First scheduled Mon–Sat 08:00 KST after the completed market close."""
    from datetime import timedelta
    stamp = pd.Timestamp(close)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError('completed close must have an explicit timezone')
    local = stamp.tz_convert('Asia/Seoul')
    candidate = local.to_pydatetime().replace(hour=8, minute=0, second=0, microsecond=0)
    if candidate < local:
        candidate += timedelta(days=1)
    while candidate.weekday() == 6:
        candidate += timedelta(days=1)
    return pd.Timestamp(candidate).tz_convert('UTC')


def morning_window_allowed(close, cutoff) -> bool:
    stamp = pd.Timestamp(cutoff)
    if pd.isna(stamp) or stamp.tzinfo is None:
        return False
    local = stamp.tz_convert('Asia/Seoul')
    return local.weekday() != 6 and local.hour >= 8 and stamp >= morning_cutoff(close)


def morning_fit_capture_allowed(close, cutoff) -> bool:
    if not morning_window_allowed(close, cutoff):
        return False
    return pd.Timestamp(cutoff).tz_convert('Asia/Seoul').date() == morning_cutoff(close).tz_convert('Asia/Seoul').date()


def pending_markets(targets, completed, *, operation_mode=None):
    keys = {(row.get('issuance_protocol'), row.get('market'), row.get('target_date')) for row in completed
            if operation_mode is None or row.get('operation_mode') == operation_mode}
    return [market for market, target in targets.items() if (PROTOCOL, market, target) not in keys]


def model_directory(output, protocol):
    from pathlib import Path
    return Path(output) / ('fit_models_postclose' if protocol == PROTOCOL else 'fit_models')


def reference_context(reference, context, close):
    """Recomputed retrospective output remains visibly distinct from a live fit."""
    expected = reference.get('expected_return')
    return {**reference, 'role': 'retrospective_reference_not_live_model', 'production_weight': 0.,
            'asof_date': context['asof_date'], 'target_date': context['target_date'],
            'prediction_cutoff': context['prediction_cutoff'], 'latest_close': close,
            'expected_index': close * (1 + expected) if expected is not None else None}


def verified_completion_records(directory):
    """Only verified live receipts complete a market+target; research gates stay distinct."""
    from pathlib import Path
    records = []
    for root in (Path(directory), Path(directory) / 'us'):
        payload = verified_latest(root)
        if payload.get('issuance_protocol') != PROTOCOL:
            continue
        for row in payload.get('markets', []):
            if row.get('carried_forward'):
                continue
            import math
            def usable(forecast):
                return (forecast.get('status') in {'rejected', 'research_candidate'} and forecast.get('calibration_ready') is True
                    and all(isinstance(forecast.get(k), (int, float)) and math.isfinite(forecast[k]) for k in ('expected_return', 'p_up'))
                    and 0 <= forecast['p_up'] <= 1)
            if not any(usable(row.get(key, {})) for key in ('forecast', 'incumbent_reference')):
                continue
            record = {'issuance_protocol': PROTOCOL, 'market': row['market'],
                'target_date': row['context']['target_date'], 'run_id': payload['run_id'],
                'forecast_id': row['forecast_id'], 'publication_checked_at': payload['issued_at']}
            if row['context'].get('stage') == 'morning':
                record['operation_mode'] = 'morning'
            records.append(record)
    return records


def prepared_config(args, config):
    """Revalidate immutable source/bytes before accepting a supplied CLI manifest."""
    import json
    from pathlib import Path
    from roboquant.index_forecast.automation_state import restore_inputs, source_fingerprint
    protocol = getattr(args, 'issuance_protocol', None) or config.get('issuance_protocol', LEGACY_PROTOCOL)
    config['issuance_protocol'] = protocol
    if getattr(args, 'markets', None):
        config['markets'] = args.markets.split(',')
    if args.prepared_manifest:
        directory = Path(args.prepared_manifest).parent
        restored = restore_inputs(directory, now=datetime.now(UTC), code_hash=source_fingerprint(), issuance_protocol=protocol)
        if restored['status'] != 'ready':
            raise ValueError('prepared input verification failed: ' + restored.get('reason', 'unknown'))
        for argument, name in (('sqlite', 'sqlite'), ('us_observations', 'us_csv')):
            supplied = getattr(args, argument, None)
            if supplied and Path(supplied).resolve() != restored['paths'][name].resolve():
                raise ValueError('CLI input differs from frozen preparation: ' + name)
        import hashlib
        economic = getattr(args, 'economic_sqlite', None)
        if economic and hashlib.sha256(Path(economic).read_bytes()).hexdigest() != restored['manifest']['files']['economic_sqlite']['sha256']:
            raise ValueError('prepared economic runtime hash mismatch')
        models = getattr(args, 'fit_state_dir', None)
        if models:
            name = 'fit_models_us' if not hasattr(args, 'sqlite') else 'fit_models_kr'
            expected = json.loads(restored['paths'][name].read_text())['files']
            actual = {p.relative_to(models).as_posix(): json.loads(p.read_text()) for p in Path(models).glob('*/*.json')}
            if actual != expected:
                raise ValueError('prepared model runtime differs from frozen bundle')
        manifest = restored['manifest']
        config['prepared_inputs'] = {key: manifest[key] for key in ('manifest_hash', 'prepared_at', 'kst_date', 'code_hash')}
        config.pop('operation_mode', None)
        if 'operation_mode' in manifest:
            config['operation_mode'] = manifest['operation_mode']
            config['prepared_inputs']['operation_mode'] = manifest['operation_mode']
        if protocol == PROTOCOL:
            # Unpinned local files cannot enrich an immutable prepared snapshot.
            for option in ('news_context', 'participant_sqlite', 'participant_bridge', 'position_evidence'):
                if getattr(args, option, None):
                    raise ValueError('postclose prepared run cannot use unfrozen external source: ' + option)
            config.update(event_context={}, international_news={}, participant_impact={})
            config['market_closes'] = verify_market_closes(restored['paths']['market_closes'], restored['paths']['market_closes_receipt'])
    return config


def carry_verified_markets(run, previous):
    """Retain already published cards without issuing them again on a partial retry."""
    from copy import deepcopy
    if previous.get('status') != 'research_only' or previous.get('issuance_protocol') != PROTOCOL:
        return run
    present = {row['market'] for row in run['markets']}
    for row in previous.get('markets', []):
        if row['market'] not in present:
            carried = deepcopy(row)
            carried.update(carried_forward=True, source_run_id=row.get('source_run_id', previous['run_id']))
            run['markets'].append(carried)
    return run


def verified_latest(directory):
    import hashlib
    import json
    from pathlib import Path
    from roboquant.index_forecast.reliable_pipeline import _resolved_run
    root = Path(directory)
    try:
        receipt = json.loads((root / 'reliable_publication.json').read_text())
        payload = None
        for name in ('reliable_index_forecast.json', 'reliable_validation_report.json', 'reliable_model_card.json'):
            raw = (root / name).read_bytes()
            document = json.loads(raw)
            if (receipt.get('payload_checksums', {}).get(name) != hashlib.sha256(raw).hexdigest()
                or document.get('run_id') != receipt.get('run_id')):
                return {}
            if name == 'reliable_index_forecast.json':
                payload = document
        if (receipt.get('schema_version') != 'reliable_publication_v1' or receipt.get('status') != 'research_only'
            or not receipt.get('publication_checked_at') or receipt.get('replay') is not False or payload.get('replay') is not False):
            return {}
        issued = pd.Timestamp(receipt['publication_checked_at'])
        if issued.tzinfo is None:
            return {}
        for row in payload.get('markets', []):
            if row.get('carried_forward'):
                continue
            context = row['context']
            if not (pd.Timestamp(context['prediction_cutoff']) <= issued < pd.Timestamp(context['issuance_deadline'])):
                return {}
        return _resolved_run(payload, receipt)
    except (OSError, KeyError, ValueError, TypeError):
        return {}


def verify_market_closes(payload_path, receipt_path=None):
    import hashlib
    import json
    from pathlib import Path
    path = Path(payload_path)
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
        receipt = json.loads(Path(receipt_path or path.with_name('market_closes_publication.json')).read_text())
        if (data.get('schema_version') != 'market_closes_v1' or not isinstance(data.get('markets'), dict)
            or receipt.get('schema_version') != 'market_closes_publication_v1'
            or receipt.get('run_id') != data.get('run_id') or not receipt.get('publication_checked_at')
            or receipt.get('payload_checksums', {}).get('market_closes.json') != hashlib.sha256(raw).hexdigest()):
            raise ValueError('market close publication verification mismatch')
        return data
    except (OSError, ValueError, TypeError) as error:
        raise ValueError('market close publication verification failed') from error

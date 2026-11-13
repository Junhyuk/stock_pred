"""Bounded collection, immutable preparation and reviewed public payload paths."""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
ROOT = Path(__file__).resolve().parents[3]
PUBLIC_NAMES = frozenset({'automation_status.json', 'reliable_index_forecast.json',
    'reliable_validation_report.json', 'reliable_model_card.json', 'reliable_publication.json',
    'economic_calendar.json', 'economic_history.json', 'economic_collection_status.json',
    'forward_history.json', 'forward_validation.json', 'actions_budget.json'})
PREPARED_NAMES = frozenset({'sqlite', 'us_csv', 'economic_sqlite', 'fit_models_kr', 'fit_models_us'})


def _utc(value):
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('aware operational clock required')
    return stamp.astimezone(UTC)


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def operation_decision(now, mode):
    local = _utc(now).astimezone(KST)
    return {'allowed': local.weekday() != 6 or mode == 'validate', 'kst_date': local.date().isoformat(),
            'mode': mode, 'reason': 'kst_sunday_excluded' if local.weekday() == 6 and mode != 'validate' else 'allowed'}


def source_fingerprint():
    paths = sorted([*ROOT.joinpath('src/roboquant/index_forecast').glob('*.py'),
                    *ROOT.joinpath('configs').glob('*.yaml'), *ROOT.joinpath('scripts').glob('*.py')])
    return _hash({str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})


def sqlite_metadata(path):
    import sqlite3
    with sqlite3.connect(f'file:{Path(path).resolve()}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite snapshot integrity failure')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_eod'").fetchone()
        return {'quick_check': 'ok', 'latest_index_dates': dict(db.execute(
            'SELECT index_code, MAX(session_date) FROM index_eod GROUP BY index_code').fetchall()) if exists else {}}


def sqlite_snapshot(source, destination):
    """Read-only backup includes committed WAL rows without altering the source."""
    import sqlite3
    with sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro', uri=True) as original:
        with sqlite3.connect(destination) as snapshot:
            original.backup(snapshot)
            snapshot.execute('PRAGMA journal_mode=DELETE')
    return sqlite_metadata(destination)


@contextmanager
def _state_lock(directory, *, exclusive):
    import fcntl
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.manifest.lock').open('a') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def freeze_inputs(files, directory, *, prepared_at=None, code_hash):
    with _state_lock(directory, exclusive=True):
        return _freeze_inputs(files, directory, prepared_at=prepared_at, code_hash=code_hash)


def _freeze_inputs(files, directory, *, prepared_at, code_hash):
    clock = _utc(prepared_at or datetime.now(UTC))
    local = clock.astimezone(KST)
    if local.hour >= 8:
        raise ValueError('prepared inputs must finish before 08:00 KST')
    if not operation_decision(clock, 'prepare')['allowed']:
        raise ValueError('KST Sunday excluded')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    manifest = {'schema_version': 'prepared_inputs_v1', 'prepared_at': clock.isoformat(),
                'kst_date': local.date().isoformat(), 'code_hash': code_hash, 'files': {}}
    for name, original in files.items():
        if name not in PREPARED_NAMES:
            raise ValueError('unknown prepared input')
        metadata = None
        if name.endswith('sqlite'):
            with tempfile.TemporaryDirectory(dir=directory) as temporary:
                snapshot = Path(temporary) / 'snapshot.sqlite'
                metadata = sqlite_snapshot(original, snapshot)
                raw = snapshot.read_bytes()
        elif name.startswith('fit_models_'):
            model_root = Path(original)
            bundle = {path.relative_to(model_root).as_posix(): json.loads(path.read_text())
                      for path in sorted(model_root.glob('*/*.json')) if not path.is_symlink()}
            raw = json.dumps({'schema_version': 'prepared_models_v1', 'files': bundle},
                             sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        else:
            raw = Path(original).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        relative = digest + ('.sqlite' if name.endswith('sqlite') else '.json' if name.startswith('fit_models_') else '.csv')
        target = directory / relative
        try:
            with target.open('xb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
                raise ValueError('immutable prepared input conflict') from None
        manifest['files'][name] = {'path': relative, 'sha256': digest, 'bytes': len(raw)}
        if metadata is not None:
            manifest['files'][name]['sqlite_metadata'] = metadata
    # Production callers omit the explicit clock; stamp completion after all
    # consistent snapshots and hashes, never the collection/backup start time.
    if prepared_at is None:
        finished = datetime.now(UTC).astimezone(KST)
        if finished.hour >= 8 or finished.date() != local.date():
            raise ValueError('prepared inputs must finish before 08:00 KST')
        manifest['prepared_at'] = finished.astimezone(UTC).isoformat()
    manifest['manifest_hash'] = _hash(manifest)
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(manifest, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    created = False
    try:
        try:
            os.link(temporary, directory / 'manifest.json')
            created = True
        except FileExistsError:
            pass
        completed = _utc(prepared_at) if prepared_at is not None else datetime.now(UTC)
        if prepared_at is None:
            completed_local = completed.astimezone(KST)
            if completed_local.hour >= 8 or completed_local.date() != local.date():
                # Readers take the same lock, so they cannot observe a late
                # first writer between the link and this final completion guard.
                if created:
                    (directory / 'manifest.json').unlink(missing_ok=True)
                raise ValueError('prepared manifest must complete before 08:00 KST')
        committed = json.loads((directory / 'manifest.json').read_text())
        if not (directory / 'completion.json').exists():
            receipt = {'schema_version': 'prepared_completion_v1',
                       'manifest_hash': committed['manifest_hash'], 'completed_at': completed.isoformat()}
            atomic_json(directory / 'completion.json', {**receipt, 'receipt_hash': _hash(receipt)})
    finally:
        temporary.unlink(missing_ok=True)
    return json.loads((directory / 'manifest.json').read_text())


def restore_inputs(directory, *, now, code_hash):
    with _state_lock(directory, exclusive=False):
        return _restore_inputs(directory, now=now, code_hash=code_hash)


def _restore_inputs(directory, *, now, code_hash):
    directory = Path(directory)
    try:
        current = _utc(now).astimezone(KST)
        manifest = json.loads((directory / 'manifest.json').read_text())
        receipt = json.loads((directory / 'completion.json').read_text())
        if (receipt.get('receipt_hash') != _hash({k: v for k, v in receipt.items() if k != 'receipt_hash'})
            or receipt.get('schema_version') != 'prepared_completion_v1'
            or receipt.get('manifest_hash') != manifest.get('manifest_hash')):
            raise ValueError('prepared completion receipt mismatch')
        completed = _utc(receipt['completed_at']).astimezone(KST)
        if manifest.get('manifest_hash') != _hash({k: v for k, v in manifest.items() if k != 'manifest_hash'}):
            raise ValueError('prepared manifest hash mismatch')
        prepared = _utc(manifest['prepared_at']).astimezone(KST)
        if (manifest['schema_version'] != 'prepared_inputs_v1' or manifest['code_hash'] != code_hash
            or manifest['kst_date'] != current.date().isoformat() or prepared.date() != current.date()
            or prepared.hour >= 8 or current.hour < 8 or prepared > current
            or completed.hour >= 8 or completed.date() != prepared.date() or not prepared <= completed <= current
            or not operation_decision(current, 'predict')['allowed']):
            raise ValueError('prepared clock/date/source protocol mismatch')
        paths = {}
        for name, item in manifest['files'].items():
            path = directory / item['path']
            if path.parent.resolve() != directory.resolve() or name not in PREPARED_NAMES:
                raise ValueError('prepared path outside immutable directory')
            if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
                raise ValueError('prepared input hash mismatch')
            if name.endswith('sqlite') and sqlite_metadata(path) != item.get('sqlite_metadata'):
                raise ValueError('prepared SQLite integrity/latest-session mismatch')
            paths[name] = path
        if not paths:
            raise ValueError('prepared inputs empty')
        return {'status': 'ready', 'paths': paths, 'manifest': manifest}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {'status': 'not_ready', 'reason': str(error), 'paths': {}}


def stage_prepared_runtime(restored, destination):
    """Use only verified preparation bytes; current Git data is not a fallback."""
    import shutil
    if restored.get('status') != 'ready':
        raise ValueError('verified prepared inputs required')
    destination = Path(destination)
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('prepared runtime destination must be empty')
    destination.mkdir(parents=True, exist_ok=True)
    result = dict(restored['paths'])
    for name, path in restored['paths'].items():
        if name == 'economic_sqlite':
            target = destination / 'economic.sqlite'
            shutil.copyfile(path, target)
            result[name] = target
        elif name.startswith('fit_models_'):
            bundle = json.loads(path.read_text())
            if bundle.get('schema_version') != 'prepared_models_v1':
                raise ValueError('prepared model bundle schema mismatch')
            root = destination / name
            root.mkdir(parents=True, exist_ok=True)
            for relative, payload in bundle['files'].items():
                if not re.fullmatch(r'[A-Z0-9_]+/\d{4}-\d{2}-\d{2}\.json', relative):
                    raise ValueError('invalid prepared model path')
                atomic_json(root / relative, payload)
            result[name] = root
    return result


def publish_prepared_models(runtime, output):
    from roboquant.index_forecast.fit_protocol import save_model_artifact
    for name, root in (('fit_models_kr', Path(output)), ('fit_models_us', Path(output) / 'us')):
        if not runtime.get(name):
            continue
        for path in Path(runtime[name]).glob('*/*.json'):
            artifact = json.loads(path.read_text())
            if (_contains_rows(artifact) or artifact.get('schema_version') not in {'fit_model_artifact_v1', 'fit_failure_v1'}
                or artifact.get('artifact_hash') != _hash({k: v for k, v in artifact.items() if k != 'artifact_hash'})):
                raise ValueError('invalid prepared model artifact')
            persisted = save_model_artifact(root / 'fit_models', path.parent.name, artifact)
            if persisted != artifact:
                raise ValueError('immutable public model conflict with prepared state')


def run_calendar_collection(output, private_dir, *, budget_seconds=120, worker_command=None):
    """An outer process-group deadline bounds even blocked sockets/parser children."""
    output, private_dir = Path(output), Path(private_dir)
    private_dir.mkdir(parents=True, exist_ok=True)
    started = datetime.now(UTC)
    status = {'status': 'unavailable', 'started_at': started.isoformat(), 'reason': None,
              'budget_seconds': min(float(budget_seconds), 120.), 'production_weight': 0.}
    with tempfile.TemporaryDirectory(prefix='calendar-', dir=private_dir) as temporary:
        result = Path(temporary) / 'result.json'
        command = worker_command or [sys.executable, str(ROOT / 'scripts/collect_economic_calendar.py'),
                                     '--worker-result', str(result)]
        command = [str(part).replace('{result}', str(result)) for part in command]
        process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=ROOT, start_new_session=True)
        try:
            process.wait(timeout=status['budget_seconds'])
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=2)
            status['reason'] = 'hard_collection_deadline'
        else:
            try:
                if process.returncode:
                    raise ValueError('collector_worker_failed')
                collection = json.loads(result.read_text())
                from roboquant.index_forecast.economic_calendar import EconomicCalendarStore
                store = EconomicCalendarStore(private_dir / 'economic.sqlite')
                history = output / 'economic_history.json'
                if history.is_file():
                    store.import_history(json.loads(history.read_text()), trusted_archive=True)
                completed = datetime.now(UTC)
                # Preserve strict, atomic validation within each source. A bad
                # source cannot roll back independent facts from another one.
                # Trusted-history integrity above remains a whole-ledger gate.
                sources_by_id = {source['source_id']: dict(source)
                    for source in collection.get('sources', []) if source.get('source_id')}
                grouped = {source_id: {'events': [], 'releases': []} for source_id in sources_by_id}
                for kind in ('events', 'releases'):
                    for row in collection.get(kind, []):
                        source_id = row.get('source_id') if isinstance(row, dict) else None
                        source_id = source_id if isinstance(source_id, str) and source_id else '_unidentified_source'
                        grouped.setdefault(source_id, {'events': [], 'releases': []})[kind].append(row)
                accepted = {'events': [], 'releases': [], 'sources': [],
                            'budget_exhausted': bool(collection.get('budget_exhausted'))}
                validation_errors = []
                for source_id, facts_by_kind in grouped.items():
                    source = sources_by_id.get(source_id, {'source_id': source_id, 'status': 'unavailable',
                                                          'reason': 'missing_source_receipt'})
                    try:
                        if source_id not in sources_by_id:
                            raise ValueError('missing_source_receipt')
                        store.ingest({**facts_by_kind, 'sources': [source]}, collected_at=completed)
                    except (ValueError, KeyError, TypeError, AttributeError) as error:
                        reason = f'source_validation_failed:{type(error).__name__}:{error}'
                        source = {**source, 'status': 'unavailable', 'reason': reason}
                        validation_errors.append({'source_id': source_id, 'reason': reason})
                        store.ingest({'events': [], 'releases': [], 'sources': [source]}, collected_at=completed)
                    else:
                        for kind in ('events', 'releases'):
                            accepted[kind].extend(facts_by_kind[kind])
                    accepted['sources'].append(source)
                # The final attempt has all source statuses/counts and receipts;
                # semantic versions deduplicate the already committed good facts.
                completed = datetime.now(UTC)
                store.ingest(accepted, collected_at=completed)
                atomic_json(history, store.export_history())
                facts = len(accepted['events']) + len(accepted['releases'])
                sources = accepted['sources']
                complete = bool(sources) and all(source.get('status') == 'ok' for source in sources)
                if facts:
                    atomic_json(output / 'economic_calendar.json', store.public_view(now=completed))
                status.update(status=('unavailable' if not facts else 'partial'
                    if collection.get('budget_exhausted') or not complete else 'collected'),
                              reason='no_parsed_economic_facts' if not facts else None,
                              validation_errors=validation_errors, source_count=len(sources),
                              event_count=len(accepted['events']), release_count=len(accepted['releases']))
            except (OSError, ValueError, KeyError, TypeError) as error:
                status['reason'] = str(error)
    status['finished_at'] = datetime.now(UTC).isoformat()
    status['elapsed_seconds'] = (datetime.now(UTC) - started).total_seconds()
    atomic_json(output / 'economic_collection_status.json', status)
    return status


def _contains_rows(value):
    if isinstance(value, dict):
        return any(k in {'rows', 'training_rows', 'X_train', 'y_train', 'raw_response'} or _contains_rows(v) for k, v in value.items())
    return isinstance(value, list) and any(_contains_rows(item) for item in value)


def public_payload_paths(directory):
    directory = Path(directory)
    result = []
    for root in (directory, directory / 'us'):
        result.extend(path for name in sorted(PUBLIC_NAMES) if (path := root / name).is_file())
        for path in sorted((root / 'fit_models').glob('*/*.json')):
            payload = json.loads(path.read_text())
            if (payload.get('schema_version') not in {'fit_model_artifact_v1', 'fit_failure_v1'}
                or _contains_rows(payload) or path.stat().st_size > 20_000_000
                or payload.get('artifact_hash') != _hash({k: v for k, v in payload.items() if k != 'artifact_hash'})):
                raise ValueError(f'invalid public model artifact: {path}')
            result.append(path)
    return result


def _decode_cache_key(key):
    import base64
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9+/_-]{43}=?', key):
        raise ValueError('authenticated cache requires a strict 256-bit Base64 key')
    decoded = base64.b64decode(key + '=' * (-len(key) % 4), altchars=b'-_', validate=True)
    canonical = base64.urlsafe_b64encode(decoded).decode().rstrip('=')
    if len(decoded) != 32 or canonical != key.replace('+', '-').replace('/', '_').rstrip('='):
        raise ValueError('authenticated cache requires a canonical 256-bit key')
    return decoded


def seal_private_cache(source, destination, *, key):
    import io
    import tarfile

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    decoded = _decode_cache_key(key)
    source, destination = Path(source), Path(destination)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for path in sorted(source.rglob('*')):
            if path.is_file() and not path.is_symlink():
                archive.add(path, arcname=str(path.relative_to(source)), recursive=False)
    nonce = os.urandom(12)
    encrypted = b'SPFC1' + nonce + AESGCM(decoded).encrypt(nonce, buffer.getvalue(), b'stock_pred_private_cache_v1')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(encrypted)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)
    return {'encrypted_bytes': len(encrypted), 'format': 'AES-256-GCM-v1'}


def open_private_cache(source, destination, *, key):
    import io
    import tarfile

    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        raw = Path(source).read_bytes()
        if raw[:5] != b'SPFC1':
            raise ValueError('cache format mismatch')
        decoded = _decode_cache_key(key)
        plaintext = AESGCM(decoded).decrypt(raw[5:17], raw[17:], b'stock_pred_private_cache_v1')
        destination = Path(destination)
        with tarfile.open(fileobj=io.BytesIO(plaintext), mode='r:gz') as archive:
            members = archive.getmembers()
            if any(not member.isfile() or Path(member.name).is_absolute() or '..' in Path(member.name).parts for member in members):
                raise ValueError('unsafe cache member')
            destination.mkdir(parents=True, exist_ok=True)
            archive.extractall(destination, members=members, filter='data')
    except (ValueError, OSError, InvalidTag, tarfile.TarError) as error:
        raise ValueError('authenticated cache unavailable') from error

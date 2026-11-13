"""Immutable normalized economic facts with conservative knowledge timestamps.

Normal ingestion never backdates first receipt. Trusted history restoration is
only for our already recorded ledger, not authentication of external histories.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

COUNTRIES = ('US', 'KR', 'JP', 'EA')
MACRO_FEATURES = tuple(
    [f'{country.lower()}_policy_change_bp' for country in COUNTRIES]
    + [f'{country.lower()}_cpi_yoy_change_pp' for country in COUNTRIES]
    + [f'{country.lower()}_unemployment_change_pp' for country in COUNTRIES]
    + ['us_nfp_change_thousands', 'us_core_pce_yoy_change_pp'])
REQUIRED_COVERAGE = frozenset((country, kind) for country in COUNTRIES
                            for kind in ('policy_rate', 'inflation', 'employment')) | {('US', 'pce')}


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _utc(value) -> datetime:
    if isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires an explicit timezone')
    return stamp.astimezone(UTC)


def _text(value) -> str:
    return _utc(value).isoformat()


def _normalize(record: dict, collected_at: datetime, *, release: bool) -> dict:
    row = dict(record)
    if row.get('country') not in COUNTRIES or not row.get('event_id'):
        raise ValueError('event identity/country is required')
    source = urlsplit(row.get('source_url', ''))
    if source.scheme != 'https' or not source.hostname:
        raise ValueError('HTTPS source provenance is required')
    supplied = _utc(row.get('first_seen_at', collected_at))
    if supplied > collected_at:
        raise ValueError('first receipt cannot be after collection completion')
    # A source claiming an old receipt does not establish that we knew it then.
    row['first_seen_at'] = collected_at.isoformat()
    row['receipt_policy'] = 'actual_collection_completion; no historical_backdating'
    publication = row.get('actual_release_at' if release else 'published_at')
    published = _utc(publication) if publication is not None else None
    if publication is not None:
        row['actual_release_at' if release else 'published_at'] = published.isoformat()
    row['available_at'] = max(collected_at, published or collected_at).isoformat()
    if release:
        if row.get('vintage') not in {'first_release', 'revision', 'current_snapshot'}:
            raise ValueError('explicit release vintage is required')
        if not row.get('measure') or not row.get('reference_period') or not row.get('unit'):
            raise ValueError('measure, reference period and unit are required')
        if isinstance(row.get('value'), bool) or not math.isfinite(float(row['value'])):
            raise ValueError('release value must be finite')
        row['value'] = float(row['value'])
        row.setdefault('seasonal_adjustment', 'unknown')
        row.setdefault('definition_version', 'unspecified')
        row['original_publication_verified'] = False
        row['publication_time_status'] = 'provided_aware_timestamp' if published else 'unknown'
    else:
        zone = ZoneInfo(row['timezone'])
        if row.get('time_precision') not in {'datetime', 'date', 'tba'}:
            raise ValueError('explicit schedule precision is required')
        scheduled = row.get('scheduled_at')
        if row['time_precision'] == 'datetime':
            if scheduled is None:
                raise ValueError('precise schedule requires a timestamp')
            stamp = _utc(scheduled)
            row['scheduled_at_original'] = scheduled
            local_day = stamp.astimezone(zone).date().isoformat()
            if row.get('scheduled_date') and row['scheduled_date'] != local_day:
                raise ValueError('schedule timezone/date mismatch')
            row['scheduled_date'] = local_day
            row['scheduled_at'] = stamp.isoformat()
        elif scheduled is not None:
            raise ValueError('date-only/TBA schedule cannot carry an invented hour')
        if row.get('status') not in {'confirmed', 'tentative', 'cancelled', 'postponed'}:
            raise ValueError('explicit schedule status is required')
    semantic = {key: value for key, value in row.items()
                if key not in {'first_seen_at', 'available_at', 'receipt_policy', 'version_id', 'source_hash'}}
    row['version_id'] = _hash(semantic)
    return row


class EconomicCalendarStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            for name in ('economic_event_versions', 'macro_release_versions', 'economic_collection_runs'):
                connection.execute(f'CREATE TABLE IF NOT EXISTS {name} (id TEXT PRIMARY KEY, available_at TEXT NOT NULL, payload TEXT NOT NULL)')
                for action in ('UPDATE', 'DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}_{action.lower()}_immutable
                        BEFORE {action} ON {name} BEGIN SELECT RAISE(ABORT, 'immutable economic evidence'); END''')

    def _connection(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def _insert(connection, table: str, rows: list[dict]) -> int:
        before = connection.total_changes
        for row in rows:
            connection.execute(f'INSERT OR IGNORE INTO {table} VALUES (?, ?, ?)',
                (row['version_id'], row['available_at'], json.dumps(row, sort_keys=True, allow_nan=False)))
        return connection.total_changes - before

    def ingest(self, collection: dict, *, collected_at) -> dict:
        received = _utc(collected_at)
        events = [_normalize(row, received, release=False) for row in collection.get('events', [])]
        releases = [_normalize(row, received, release=True) for row in collection.get('releases', [])]
        run = {'available_at': received.isoformat(), 'sources': collection.get('sources', []),
               'budget_exhausted': bool(collection.get('budget_exhausted', False)),
               'event_count': len(events), 'release_count': len(releases),
               'receipts': [{'version_id': row['version_id'], 'source_hash': row.get('source_hash')}
                            for row in [*events, *releases]]}
        run['version_id'] = _hash(run)
        with self._connection() as connection:
            counts = {'events_inserted': self._insert(connection, 'economic_event_versions', events),
                      'releases_inserted': self._insert(connection, 'macro_release_versions', releases)}
            self._insert(connection, 'economic_collection_runs', [run])
        return {**counts, 'collected_at': received.isoformat()}

    def _rows(self, table: str, cutoff=None) -> list[dict]:
        query = f'SELECT payload FROM {table}'
        parameters = ()
        if cutoff is not None:
            query += ' WHERE available_at <= ?'
            parameters = (_text(cutoff),)
        query += ' ORDER BY available_at, id'
        with self._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(query, parameters)]

    def events_asof(self, cutoff) -> list[dict]:
        latest = {}
        for row in self._rows('economic_event_versions', cutoff):
            latest[row['event_id']] = row
        return sorted(latest.values(), key=lambda row: (row.get('scheduled_date') or '9999', row['event_id']))

    def releases_asof(self, cutoff) -> list[dict]:
        return self._rows('macro_release_versions', cutoff)

    def export_history(self) -> dict:
        data = {'schema_version': 'economic_history_v1',
                'event_versions': self._rows('economic_event_versions'),
                'release_versions': self._rows('macro_release_versions'),
                'collection_runs': self._rows('economic_collection_runs')}
        return {**data, 'history_hash': _hash(data)}

    def import_history(self, history: dict, *, trusted_archive: bool = False) -> None:
        if not trusted_archive:
            raise ValueError('only a previously recorded trusted archive may preserve historical receipt times')
        data = {key: value for key, value in history.items() if key != 'history_hash'}
        if history.get('history_hash') != _hash(data) or data.get('schema_version') != 'economic_history_v1':
            raise ValueError('economic history hash/schema mismatch')
        with self._connection() as connection:
            for field, table in (('event_versions', 'economic_event_versions'),
                                 ('release_versions', 'macro_release_versions'),
                                 ('collection_runs', 'economic_collection_runs')):
                self._insert(connection, table, data[field])

    def numeric_features(self, cutoff, *, policy: str = 'verified_first_release') -> dict:
        if policy not in {'verified_first_release', 'observed_current_receipt_v1'}:
            raise ValueError('unsupported macro feature policy')
        stamp = _utc(cutoff)
        all_rows = self.releases_asof(stamp)
        features, reasons, counts = {}, {}, {}

        def value(country, measure, *, difference=True, multiplier=1., unit='percent', max_age=62):
            rows = [row for row in all_rows if row['country'] == country and row['measure'] == measure]
            eligible = ([row for row in rows if row['vintage'] == 'first_release'
                         and row.get('actual_release_at') and row.get('original_publication_verified') is True]
                        if policy == 'verified_first_release' else rows)
            periods = sorted({row['reference_period'] for row in eligible})
            count = len(periods)
            if not periods or (difference and count < 2):
                return None, 'insufficient_verified_vintages' if policy == 'verified_first_release' else 'insufficient_observed_periods', count
            selected = []
            for period in periods[-2 if difference else -1:]:
                versions = [row for row in eligible if row['reference_period'] == period]
                if policy == 'observed_current_receipt_v1':
                    latest_receipt = max(row['available_at'] for row in versions)
                    versions = [row for row in versions if row['available_at'] == latest_receipt]
                identities = {(row['value'], row['unit'], row['seasonal_adjustment'], row['definition_version']) for row in versions}
                if len(identities) != 1:
                    return None, 'conflicting_vintage_values', count
                selected.append(min(versions, key=lambda row: row['first_seen_at']))
            newest = selected[-1]
            for observed in selected:
                try:
                    year, month = int(observed['reference_period'][:4]), int(observed['reference_period'][5:7])
                    reference_month = year * 12 + month
                    if not 1 <= month <= 12:
                        raise ValueError('invalid month')
                    receipt = _utc(observed['available_at'])
                    receipt_month = receipt.year * 12 + receipt.month
                    current_month = stamp.year * 12 + stamp.month
                    if reference_month > current_month or reference_month > receipt_month:
                        return None, 'future_reference_period', count
                    if measure != 'policy_rate' and reference_month >= receipt_month:
                        return None, 'monthly_reference_period_not_completed_at_receipt', count
                    if measure == 'policy_rate' and len(observed['reference_period']) >= 10:
                        if datetime.fromisoformat(observed['reference_period'][:10]).date() > receipt.date():
                            return None, 'future_policy_reference_date', count
                except (ValueError, IndexError):
                    return None, 'invalid_reference_period', count
            if rows and max(row['reference_period'] for row in rows) > newest['reference_period']:
                return None, 'latest_period_has_no_verified_first_vintage', count
            if (stamp - _utc(newest['available_at'])).total_seconds() > max_age * 86400:
                return None, 'stale_release_state', count
            if any(row['unit'] != unit or row['seasonal_adjustment'] == 'unknown'
                   or row['definition_version'] == 'unspecified' for row in selected):
                return None, 'incompatible_or_unknown_units_adjustment', count
            try:
                reference = datetime.fromisoformat(newest['reference_period'][:7] + '-01').replace(tzinfo=UTC)
            except ValueError:
                return None, 'invalid_reference_period', count
            if (stamp - reference).days > max_age + 31:
                return None, 'stale_reference_period', count
            if difference and any(newest[key] != selected[0][key] for key in ('unit', 'seasonal_adjustment', 'definition_version')):
                return None, 'series_definition_break', count
            if difference and measure != 'policy_rate':
                try:
                    previous_month = int(selected[0]['reference_period'][:4]) * 12 + int(selected[0]['reference_period'][5:7])
                    current_month = int(newest['reference_period'][:4]) * 12 + int(newest['reference_period'][5:7])
                except (ValueError, IndexError):
                    return None, 'invalid_monthly_reference_period', count
                if current_month != previous_month + 1:
                    return None, 'missing_previous_reference_period', count
            result = newest['value'] - selected[0]['value'] if difference else newest['value']
            return float(result * multiplier), None, count

        for country in COUNTRIES:
            for suffix, measure, multiplier, age in (
                ('policy_change_bp', 'policy_rate', 100., 120),
                ('cpi_yoy_change_pp', 'cpi_headline_yoy', 1., 62),
                ('unemployment_change_pp', 'unemployment_rate', 1., 62)):
                key = f'{country.lower()}_{suffix}'
                features[key], reasons[key], counts[key] = value(country, measure, multiplier=multiplier, max_age=age)
        features['us_nfp_change_thousands'], reasons['us_nfp_change_thousands'], counts['us_nfp_change_thousands'] = value(
            'US', 'nfp_monthly_change', difference=False, unit='thousand_persons')
        features['us_core_pce_yoy_change_pp'], reasons['us_core_pce_yoy_change_pp'], counts['us_core_pce_yoy_change_pp'] = value(
            'US', 'core_pce_yoy')
        missing = [key for key in MACRO_FEATURES if features[key] is None]
        return {'features': features, 'ready': not missing, 'missing_features': missing,
                'feature_policy': policy,
                'reasons': {key: reason for key, reason in reasons.items() if reason},
                'distinct_release_counts': counts, 'cutoff': stamp.isoformat(),
                'consensus_status': 'unavailable; no surprise feature generated', 'production_weight': 0.}

    def forecast_context(self, *, asof_close, cutoff, target_close) -> dict:
        start, stamp, end = map(_utc, (asof_close, cutoff, target_close))
        scheduled, released, unknown = [], [], []
        for event in self.events_asof(stamp):
            if event['status'] == 'cancelled':
                continue
            when = event.get('scheduled_at')
            if when is None:
                day = event.get('scheduled_date')
                zone = ZoneInfo(event['timezone'])
                if day is None or start.astimezone(zone).date().isoformat() <= day <= end.astimezone(zone).date().isoformat():
                    unknown.append(event)
            elif stamp < _utc(when) <= end:
                (scheduled if event['status'] == 'confirmed' else unknown).append(event)
            elif start < _utc(when) <= stamp:
                actuals = [row for row in self.releases_asof(stamp) if row['event_id'] == event['event_id']]
                if actuals:
                    released.append({**event, 'actuals': actuals})
                else:
                    unknown.append({**event, 'reason': 'scheduled_time_passed_actual_not_received'})
        coverage = set()
        runs = self._rows('economic_collection_runs', stamp)
        latest_sources = {}
        for run in runs:
            for source in run['sources']:
                latest_sources[source.get('source_id', '')] = (source, run['available_at'])
        for source, seen in latest_sources.values():
            if (stamp - _utc(seen)).total_seconds() > 72 * 3600 or source.get('status') != 'ok':
                continue
            if not (source.get('coverage_start', '9999') <= start.date().isoformat()
                    and source.get('coverage_end', '0000') >= end.date().isoformat()):
                continue
            coverage.update((source.get('country'), kind) for kind in source.get('event_types', []))
        complete = REQUIRED_COVERAGE.issubset(coverage)
        return {'cutoff': stamp.isoformat(), 'scheduled_before_target_close': scheduled,
                'released_since_asof': released, 'pending_or_unknown': unknown,
                'calendar_complete': complete, 'event_bucket': bool(scheduled or released) if complete and not unknown else None,
                'coverage_missing': [f'{country}:{kind}' for country, kind in sorted(REQUIRED_COVERAGE - coverage)],
                'production_weight': 0.}

    def public_view(self, *, now=None, days_forward: int = 30, days_back: int = 7) -> dict:
        stamp = _utc(now or datetime.now(UTC))
        lower, upper = (stamp - timedelta(days=days_back)).date().isoformat(), (stamp + timedelta(days=days_forward)).date().isoformat()
        events = [row for row in self.events_asof(stamp) if row.get('scheduled_date') is None
                  or lower <= row['scheduled_date'] <= upper]
        releases = [row for row in self.releases_asof(stamp) if _utc(row['available_at']) >= stamp - timedelta(days=days_back)]
        runs = self._rows('economic_collection_runs', stamp)
        return {'schema_version': 'economic_calendar_v1', 'asof': stamp.isoformat(), 'events': events,
                'recent_releases': releases,
                'numeric_state': self.numeric_features(stamp, policy='observed_current_receipt_v1'),
                'first_release_numeric_state': self.numeric_features(stamp),
                'last_collection': runs[-1] if runs else None, 'production_weight': 0.,
                'limitations': ['First receipt is not an independently authenticated original publication timestamp.',
                                'Current API snapshots never establish historical first-release availability.',
                                'Missing consensus is not a zero surprise.']}

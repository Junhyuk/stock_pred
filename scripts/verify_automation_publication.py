#!/usr/bin/env python3
"""Fail the daily job unless this CLI run produced a verified live publication.

Forecast CLIs also return exit zero for intentional blocked research results.
Their final stdout JSON and the written receipt must agree before daily status,
Git commits, or Pages deployment can proceed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

COMPACT_PAYLOADS = (
    'reliable_index_forecast.json',
    'reliable_validation_report.json',
    'reliable_model_card.json',
)


def json_object(raw: str | bytes) -> dict:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('expected a JSON object')
    return value


def verify_publication(result_log: Path, data_dir: Path) -> str:
    lines = [line for line in result_log.read_text(encoding='utf-8').splitlines() if line.strip()]
    if not lines:
        raise ValueError('forecast CLI produced no result')
    result = json_object(lines[-1])
    if result.get('status') != 'research_only':
        raise ValueError(f"forecast was not published: {result.get('status')!r}")
    run_id = result.get('run_id')
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError('forecast result has no run ID')
    issued_at = result.get('issued_at')
    if not isinstance(issued_at, str) or datetime.fromisoformat(issued_at).utcoffset() is None:
        raise ValueError('forecast result has no timezone-aware publication time')

    receipt = json_object((data_dir / 'reliable_publication.json').read_bytes())
    if (receipt.get('schema_version') != 'reliable_publication_v1'
            or receipt.get('status') != 'research_only'
            or receipt.get('replay') is not False
            or receipt.get('run_id') != run_id
            or receipt.get('publication_checked_at') != issued_at):
        raise ValueError('publication receipt does not match this live CLI result')
    checksums = receipt.get('payload_checksums')
    if not isinstance(checksums, dict):
        raise ValueError('publication receipt has no payload checksums')
    for name in COMPACT_PAYLOADS:
        raw = (data_dir / name).read_bytes()
        if checksums.get(name) != hashlib.sha256(raw).hexdigest():
            raise ValueError(f'publication checksum mismatch: {name}')
        payload = json_object(raw)
        if payload.get('run_id') != run_id:
            raise ValueError(f'publication run ID mismatch: {name}')
        if name == 'reliable_index_forecast.json':
            if (payload.get('replay') is not False
                    or payload.get('status') != 'unpublished'
                    or not isinstance(payload.get('markets'), list)
                    or not payload['markets']):
                raise ValueError('forecast payload is not a live staged research run')
    return run_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--result', type=Path, required=True)
    parser.add_argument('--data-dir', type=Path, required=True)
    args = parser.parse_args()
    try:
        run_id = verify_publication(args.result, args.data_dir)
    except (OSError, ValueError, TypeError) as exc:
        print(f'Automation publication verification failed: {exc}', file=sys.stderr)
        return 1
    print(f'Verified live publication: {run_id}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

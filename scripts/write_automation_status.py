#!/usr/bin/env python3
"""Record the daily calendar decision and verified latest publication metadata."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def verified_publication(directory: Path) -> dict | None:
    try:
        raw = (directory / 'reliable_index_forecast.json').read_bytes()
        forecast = json.loads(raw)
        receipt = json.loads((directory / 'reliable_publication.json').read_text(encoding='utf-8'))
        if (receipt.get('schema_version') != 'reliable_publication_v1'
            or not receipt.get('publication_checked_at')
            or not forecast.get('run_id')
            or receipt.get('run_id') != forecast['run_id']
            or receipt.get('payload_checksums', {}).get('reliable_index_forecast.json') != hashlib.sha256(raw).hexdigest()):
            return None
        return {
            'publication_checked_at': receipt['publication_checked_at'],
            'asof_dates': {item['market']: item.get('context', {}).get('asof_date')
                           for item in forecast.get('markets', []) if item.get('market')},
        }
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=Path('docs/data'))
    parser.add_argument('--date', required=True)
    parser.add_argument('--reason', required=True, choices=('xkrx_session', 'not_xkrx_session'))
    args = parser.parse_args()
    status = {
        'checked_at': datetime.now(timezone.utc).isoformat(),
        'kst_date': args.date,
        'reason': args.reason,
        'forecast_attempted': args.reason == 'xkrx_session',
        'korea': verified_publication(args.data_dir),
        'us': verified_publication(args.data_dir / 'us'),
        'news_context': 'not_collected',
    }
    args.data_dir.mkdir(parents=True, exist_ok=True)
    (args.data_dir / 'automation_status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()

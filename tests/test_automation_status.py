from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'write_automation_status.py'


def test_holiday_status_reports_only_verified_previous_forecast(tmp_path):
    docs = tmp_path / 'data'
    docs.mkdir()
    raw = json.dumps({'run_id': 'r1', 'markets': [{'market': 'KOSPI', 'context': {'asof_date': '2026-11-13'}}]}).encode()
    (docs / 'reliable_index_forecast.json').write_bytes(raw)
    (docs / 'reliable_publication.json').write_text(json.dumps({
        'schema_version': 'reliable_publication_v1', 'run_id': 'r1',
        'publication_checked_at': '2026-11-13T00:00:00Z',
        'payload_checksums': {'reliable_index_forecast.json': hashlib.sha256(raw).hexdigest()},
    }))
    subprocess.run([sys.executable, str(SCRIPT), '--data-dir', str(docs), '--date', '2026-11-14', '--reason', 'not_xkrx_session'], check=True)
    status = json.loads((docs / 'automation_status.json').read_text())
    assert status['reason'] == 'not_xkrx_session'
    assert status['forecast_attempted'] is False
    assert status['korea']['publication_checked_at'] == '2026-11-13T00:00:00Z'
    assert status['korea']['asof_dates'] == {'KOSPI': '2026-11-13'}
    assert status['us'] is None


def test_invalid_manifest_is_not_reported_as_verified(tmp_path):
    (tmp_path / 'reliable_index_forecast.json').write_text('{"run_id":"r1"}')
    (tmp_path / 'reliable_publication.json').write_text('{"run_id":"r1"}')
    subprocess.run([sys.executable, str(SCRIPT), '--data-dir', str(tmp_path), '--date', '2026-11-16', '--reason', 'xkrx_session'], check=True)
    status = json.loads((tmp_path / 'automation_status.json').read_text())
    assert status['forecast_attempted'] is True
    assert status['korea'] is None

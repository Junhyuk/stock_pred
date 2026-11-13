from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'verify_automation_publication.py'


def publication(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    result = {'status': 'research_only', 'run_id': 'current-run',
              'issued_at': '2026-11-13T22:55:00+00:00'}
    checksums = {}
    for name in ('reliable_index_forecast.json', 'reliable_validation_report.json', 'reliable_model_card.json'):
        payload = {'run_id': 'current-run', 'status': 'unpublished', 'replay': False,
                   'markets': [{'market': 'KOSPI', 'forecast': {'status': 'rejected'}},
                               {'market': 'KOSDAQ', 'forecast': {'status': 'rejected'}}]}
        raw = json.dumps(payload).encode()
        (data / name).write_bytes(raw)
        checksums[name] = hashlib.sha256(raw).hexdigest()
    receipt = {'schema_version': 'reliable_publication_v1', 'status': 'research_only',
               'run_id': 'current-run', 'publication_checked_at': result['issued_at'],
               'replay': False, 'payload_checksums': checksums}
    (data / 'reliable_publication.json').write_text(json.dumps(receipt))
    log = tmp_path / 'result.log'
    log.write_text('model training log\n' + json.dumps(result) + '\n')
    return data, log, result, receipt


def verify(data, log):
    return subprocess.run([sys.executable, str(SCRIPT), '--data-dir', str(data), '--result', str(log)],
                          capture_output=True, text=True)


def test_current_live_publication_passes_with_training_log(tmp_path):
    data, log, _, _ = publication(tmp_path)
    checked = verify(data, log)
    assert checked.returncode == 0, checked.stderr


@pytest.mark.parametrize('status', ['blocked_stale_input', 'blocked_issuance_deadline',
                                   'blocked_us_input_or_deadline', 'unpublished', None, ''])
def test_blocked_or_unknown_result_cannot_publish_old_valid_receipt(tmp_path, status):
    data, log, result, _ = publication(tmp_path)
    result['status'] = status
    log.write_text(json.dumps(result))
    checked = verify(data, log)
    assert checked.returncode != 0


@pytest.mark.parametrize('body', ['', ' \n\t', 'not json', 'null', '[]', '{}'])
def test_missing_or_malformed_result_is_rejected(tmp_path, body):
    data, log, _, _ = publication(tmp_path)
    log.write_text(body)
    assert verify(data, log).returncode != 0


@pytest.mark.parametrize('change', ['old_run', 'wrong_time', 'bad_time', 'replay', 'missing_receipt',
                                   'missing_payload', 'corrupt_payload', 'missing_checksum'])
def test_mismatched_incomplete_or_replay_publication_is_rejected(tmp_path, change):
    data, log, result, receipt = publication(tmp_path)
    if change == 'old_run':
        result['run_id'] = 'another-run'
        log.write_text(json.dumps(result))
    elif change == 'wrong_time':
        receipt['publication_checked_at'] = '2026-11-12T22:55:00+00:00'
    elif change == 'bad_time':
        result['issued_at'] = receipt['publication_checked_at'] = ' '
        log.write_text(json.dumps(result))
    elif change == 'replay':
        receipt['replay'] = True
    elif change == 'missing_payload':
        (data / 'reliable_model_card.json').unlink()
    elif change == 'corrupt_payload':
        (data / 'reliable_validation_report.json').write_text('{}')
    elif change == 'missing_checksum':
        receipt['payload_checksums'].pop('reliable_model_card.json')
    (data / 'reliable_publication.json').write_text(json.dumps(receipt))
    if change == 'missing_receipt':
        (data / 'reliable_publication.json').unlink()
    assert verify(data, log).returncode != 0


@pytest.mark.parametrize('step_name,cli_name', [
    ('Korea research forecast', 'run_reliable_index_forecast.py'),
    ('US research forecast', 'run_us_index_forecast.py'),
])
def test_workflow_rejects_exit_zero_blocked_cli(tmp_path, step_name, cli_name):
    workflow = yaml.safe_load((SCRIPT.parents[1] / '.github/workflows/premarket-research-forecast.yml').read_text())
    step = next(item for item in workflow['jobs']['forecast']['steps'] if item['name'] == step_name)
    scripts = tmp_path / 'scripts'
    scripts.mkdir()
    (scripts / cli_name).write_text('print(\'{"status":"blocked_stale_input","markets":[]}\')\n')
    shutil.copy2(SCRIPT, scripts / SCRIPT.name)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    (bin_dir / 'python').symlink_to(sys.executable)
    command = step['run'].replace('${{ steps.prices.outputs.sqlite }}', 'unused.sqlite').replace(
        '${{ steps.prices.outputs.us_csv }}', 'unused.csv')
    command += '\necho unsafe-publication-continued\n'
    result = subprocess.run(['bash', '-c', command], cwd=tmp_path,
                            env={**os.environ, 'RUNNER_TEMP': str(tmp_path),
                                 'PATH': str(bin_dir) + os.pathsep + os.environ.get('PATH', '')},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert 'unsafe-publication-continued' not in result.stdout
    assert 'forecast was not published' in result.stderr


def test_automation_publication_time_renders_in_korea_timezone():
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js is required to exercise the static page formatter')
    html = (SCRIPT.parents[1] / 'docs/reliable-index-forecast.html').read_text()
    functions = html.split('function formatAutomationTime(value) {', 1)[1].split("fetch('data/automation_status.json'", 1)[0]
    code = 'function formatAutomationTime(value) {' + functions + '''
const assert = require('node:assert/strict');
const text = renderAutomationStatus({checked_at: '2026-11-13T22:50:00Z', reason: 'not_xkrx_session',
  korea: {publication_checked_at: '2026-11-13T22:55:00Z', asof_dates: {KOSPI: '2026-11-13'}}});
assert.ok(text.includes('2026. 11. 14. 오전 7:55:00 (한국 시각)'), text);
assert.ok(!text.includes('2026-11-13T22:55:00Z'));
assert.equal(formatAutomationTime('invalid'), '확인 불가');
assert.equal(formatAutomationTime(null), '확인 불가');
'''
    result = subprocess.run([node, '-e', code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

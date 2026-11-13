"""Clock, frozen-input and public-storage boundaries for daily automation."""
import importlib
import json
import sys
import time

import pytest


def api():
    spec = importlib.util.find_spec('roboquant.index_forecast.automation_state')
    assert spec is not None, 'split preparation/publication state protocol is missing'
    return importlib.import_module('roboquant.index_forecast.automation_state')


def test_frozen_input_roundtrip_rejects_late_changed_or_wrong_day(tmp_path):
    original = tmp_path / 'input.sqlite'
    import sqlite3
    with sqlite3.connect(original) as db:
        db.execute('CREATE TABLE evidence (value TEXT)')
        db.execute("INSERT INTO evidence VALUES ('private-price-data')")
    directory = tmp_path / 'prepared'
    api().freeze_inputs({'sqlite': original}, directory, prepared_at='2026-11-15T22:58:00Z', code_hash='code1')
    with sqlite3.connect(original) as db:
        db.execute("UPDATE evidence SET value='new data after preparation'")
    restored = api().restore_inputs(directory, now='2026-11-15T23:01:00Z', code_hash='code1')
    assert restored['status'] == 'ready'
    with sqlite3.connect(restored['paths']['sqlite']) as db:
        assert db.execute('SELECT value FROM evidence').fetchone()[0] == 'private-price-data'
    assert api().restore_inputs(directory, now='2026-11-16T23:01:00Z', code_hash='code1')['status'] == 'not_ready'
    assert api().restore_inputs(directory, now='2026-11-15T23:01:00Z', code_hash='changed')['status'] == 'not_ready'
    restored['paths']['sqlite'].write_bytes(b'tamper')
    assert api().restore_inputs(directory, now='2026-11-15T23:01:00Z', code_hash='code1')['status'] == 'not_ready'
    with pytest.raises(ValueError, match='08:00'):
        api().freeze_inputs({'sqlite': original}, tmp_path / 'late', prepared_at='2026-11-15T23:01:00Z', code_hash='code1')


@pytest.mark.parametrize('utc,allowed', [('2026-11-14T22:50:00Z', False), ('2026-11-15T22:50:00Z', True), ('2026-11-13T22:50:00Z', True)])
def test_kst_sunday_exclusion_applies_to_prepare_predict_latest(utc, allowed):
    for mode in ('prepare', 'predict', 'latest'):
        assert api().operation_decision(utc, mode)['allowed'] is allowed


def test_collection_hard_deadline_preserves_previous_public_snapshot(tmp_path):
    output = tmp_path / 'public'
    output.mkdir()
    old = output / 'economic_calendar.json'
    old.write_text('{"prior":true}')
    start = time.monotonic()
    result = api().run_calendar_collection(output, tmp_path / 'private', budget_seconds=.2,
        worker_command=[sys.executable, '-c', 'import time; time.sleep(10)'])
    assert time.monotonic() - start < 3
    assert result['status'] == 'unavailable'
    assert result['reason'] == 'hard_collection_deadline'
    assert json.loads(old.read_text()) == {'prior': True}


def test_public_allowlist_rejects_private_rows_and_unknown_model_payload(tmp_path):
    directory = tmp_path / 'docs' / 'data'
    directory.mkdir(parents=True)
    (directory / 'economic_calendar.json').write_text('{}')
    (directory / 'raw.sqlite').write_bytes(b'private')
    model = directory / 'fit_models' / 'KOSPI' / '2026-11-17.json'
    model.parent.mkdir(parents=True)
    model.write_text('{"rows":[{"private":1}]}')
    with pytest.raises(ValueError, match='model'):
        api().public_payload_paths(directory)
    model.unlink()
    paths = api().public_payload_paths(directory)
    assert directory / 'economic_calendar.json' in paths
    assert directory / 'raw.sqlite' not in paths


def test_only_authenticated_encrypted_private_cache_can_restore(tmp_path):
    import base64
    key = base64.b64encode(b'k' * 32).decode()
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'input.sqlite').write_bytes(b'private training price rows')
    encrypted = tmp_path / 'cache.enc'
    api().seal_private_cache(source, encrypted, key=key)
    assert b'private training price rows' not in encrypted.read_bytes()
    restored = tmp_path / 'restored'
    api().open_private_cache(encrypted, restored, key=key)
    assert (restored / 'input.sqlite').read_bytes() == b'private training price rows'
    with pytest.raises(ValueError, match='authenticated'):
        api().open_private_cache(encrypted, tmp_path / 'wrong', key=base64.b64encode(b'x' * 32).decode())
    encrypted.write_bytes(encrypted.read_bytes()[:-1] + b'x')
    with pytest.raises(ValueError, match='authenticated'):
        api().open_private_cache(encrypted, tmp_path / 'tampered', key=key)


def test_workflow_has_split_kst_schedule_validation_and_encrypted_only_cache():
    from pathlib import Path

    import yaml
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / '.github/workflows/premarket-research-forecast.yml').read_text())
    trigger = workflow.get('on', workflow.get(True))
    assert {item['cron'] for item in trigger['schedule']} == {'50 22 * * 0-5', '0 23 * * 0-5'}
    assert 'validate' in trigger['workflow_dispatch']['inputs']['mode']['options']
    for job in workflow['jobs'].values():
        assert job['runs-on'] == 'ubuntu-latest'
        cap = job.get('timeout-minutes', 999)
        if isinstance(cap, str):
            import re
            limits = [int(value) for pair in re.findall(r'&& (\d+)|\|\| (\d+)', cap) for value in pair if value]
            assert sorted(limits) == [6, 10, 25]
            assert "inputs.mode == 'prepare'" in cap and "inputs.mode == 'validate'" in cap
        else:
            assert cap <= 25
        for step in job['steps']:
            if step.get('uses', '').startswith('actions/cache'):
                assert step['with']['path'] == '.forecast-cache/state.enc'
            if step.get('uses', '').startswith('actions/upload-artifact'):
                assert step['with']['retention-days'] <= 14
    text = (root / '.github/workflows/premarket-research-forecast.yml').read_text()
    assert 'FORECAST_CACHE_KEY' in text
    assert 'accuracy_operation.py' in text
    assert '--phase freeze' in text and '--phase inputs' in text


def test_calendar_ui_distinguishes_unknown_times_and_observed_values():
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which('node')
    if node is None:
        pytest.skip('node required for static renderer contract')
    script = Path(__file__).resolve().parents[1] / 'docs/assets/economic-calendar.js'
    assert script.is_file(), 'economic calendar UI missing'
    code = '''const assert=require('node:assert/strict');
const ui=require(process.argv[1]);
const html=ui.renderEconomicCalendar({generated_at:'2026-11-14T00:00:00Z',events:[
 {country:'JP',title:'BOJ <script>',scheduled_date:'2026-11-20',scheduled_at:null,time_precision:'date',timezone:'Asia/Tokyo',status:'tentative',source_url:'https://www.boj.or.jp/',first_seen_at:'2026-11-14T00:00:00Z'}],
 recent_releases:[{country:'US',measure:'nfp_monthly_change',value:180,unit:'thousand_persons',reference_period:'2026-10',vintage:'current_snapshot',available_at:'2026-11-14T00:00:00Z',source_url:'https://www.bls.gov/'}],
 numeric_state:{ready:false,missing_features:['kr_policy_change_bp']}});
assert.ok(html.includes('시간 미정'));
assert.ok(html.includes('&lt;script&gt;'));
assert.ok(html.includes('180'));
assert.ok(html.includes('thousand_persons'));
assert.ok(html.includes('예상치 없음'));
assert.ok(html.includes('현재 관측값'));
assert.ok(!html.includes('NaN'));
'''
    result = subprocess.run([node, '-e', code, str(script)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_preparation_without_post_manifest_completion_receipt_is_not_ready(tmp_path):
    import sqlite3
    source = tmp_path / 'prices.sqlite'
    with sqlite3.connect(source) as db:
        db.execute('CREATE TABLE evidence (value REAL)')
    directory = tmp_path / 'prepared'
    api().freeze_inputs({'sqlite': source}, directory, prepared_at='2026-11-15T22:58:00Z', code_hash='test')
    (directory / 'completion.json').unlink(missing_ok=True)
    assert api().restore_inputs(directory, now='2026-11-15T23:01:00Z', code_hash='test')['status'] == 'not_ready'


def test_urlsafe_unpadded_and_standard_cache_keys_are_strictly_supported(tmp_path):
    import base64
    raw = b'\xff' * 32
    keys = [base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(),
            base64.urlsafe_b64encode(raw).decode().rstrip('=')]
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'value.json').write_text('{"known":true}')
    for index, key in enumerate(keys):
        sealed = tmp_path / f'{index}.enc'
        api().seal_private_cache(source, sealed, key=key)
        api().open_private_cache(sealed, tmp_path / f'restored-{index}', key=keys[0])
    for bad in ('', keys[-1] + '=', keys[-1] + 'x', keys[-1] + '===', ' ' + keys[-1], keys[-1][:-1] + '!'):
        if bad == keys[1]:
            continue
        with pytest.raises(ValueError):
            api().seal_private_cache(source, tmp_path / 'invalid.enc', key=bad)


def test_economic_and_model_inputs_are_pinned_with_the_price_preparation(tmp_path):
    import sqlite3
    original = tmp_path / 'economic.sqlite'
    with sqlite3.connect(original) as db:
        db.execute('CREATE TABLE observed (value REAL)')
        db.execute('INSERT INTO observed VALUES (3.2)')
    models = tmp_path / 'git-models'
    (models / 'KOSPI').mkdir(parents=True)
    model = models / 'KOSPI' / '2026-11-17.json'
    model.write_text('{"frozen_coefficient":1}')
    frozen = tmp_path / 'prepared'
    api().freeze_inputs({'economic_sqlite': original, 'fit_models_kr': models, 'fit_models_us': tmp_path / 'absent'},
        frozen, prepared_at='2026-11-15T22:58:00Z', code_hash='code')
    with sqlite3.connect(original) as db:
        db.execute('UPDATE observed SET value=99')
    model.write_text('{"frozen_coefficient":99}')
    restored = api().restore_inputs(frozen, now='2026-11-15T23:01:00Z', code_hash='code')
    assert restored['status'] == 'ready'
    runtime = api().stage_prepared_runtime(restored, tmp_path / 'runtime')
    with sqlite3.connect(runtime['economic_sqlite']) as db:
        assert db.execute('SELECT value FROM observed').fetchone()[0] == 3.2
    assert json.loads((runtime['fit_models_kr'] / 'KOSPI' / '2026-11-17.json').read_text())['frozen_coefficient'] == 1
    assert list(runtime['fit_models_us'].glob('*')) == []


def test_new_anchor_model_from_prepared_state_is_durable_for_next_day(tmp_path):
    from dataclasses import replace

    from test_reliable_accuracy_protocol import config, sample_frame

    from roboquant.index_forecast.dataset import PRICE_FEATURES
    from roboquant.index_forecast.reliable import resolve_live_fit

    frame = sample_frame()
    settings = replace(config(), calendar_name='XNYS')
    selected = {'family': 'linear', 'group': 'price', 'features': PRICE_FEATURES}
    prepared = tmp_path / 'prepare-one'
    api().freeze_inputs({'fit_models_us': tmp_path / 'absent'}, prepared,
        prepared_at='2019-03-05T22:58:00Z', code_hash='protocol')
    restored = api().restore_inputs(prepared, now='2019-03-05T23:01:00Z', code_hash='protocol')
    runtime = api().stage_prepared_runtime(restored, tmp_path / 'runtime-one')
    latest = frame.loc[frame.asof_date == '2019-03-05'].copy()
    latest['prediction_cutoff'] = '2019-03-05T23:00:00Z'
    captured = resolve_live_fit(frame.loc[frame.asof_date < '2019-03-05'], latest, selected,
        settings=settings, state_dir=runtime['fit_models_us'], market='US_SPX')
    assert captured['status'] == 'ready'
    public = tmp_path / 'public'
    api().publish_prepared_models(runtime, public)
    second = tmp_path / 'prepare-two'
    api().freeze_inputs({'fit_models_us': public / 'us/fit_models'}, second,
        prepared_at='2019-03-06T22:58:00Z', code_hash='protocol')
    next_runtime = api().stage_prepared_runtime(api().restore_inputs(second, now='2019-03-06T23:01:00Z',
        code_hash='protocol'), tmp_path / 'runtime-two')
    next_row = frame.loc[frame.asof_date == '2019-03-06'].copy()
    next_row['prediction_cutoff'] = '2019-03-06T23:00:00Z'
    reused = resolve_live_fit(frame, next_row, selected, settings=settings,
        state_dir=next_runtime['fit_models_us'], market='US_SPX', allow_capture=False)
    assert reused['status'] == 'ready'
    assert reused['artifact']['artifact_hash'] == captured['artifact']['artifact_hash']

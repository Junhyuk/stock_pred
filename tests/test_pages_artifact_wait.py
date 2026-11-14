"""Run deployment readiness behavior and both upload/wait/deploy wiring checks."""
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / '.github/actions/wait-for-pages-artifact'


def test_pages_artifact_wait_behavior():
    assert (ACTION / 'index.cjs').is_file(), 'Pages artifact visibility gate is missing'
    node = shutil.which('node')
    assert node is not None, 'Node is required to verify the Pages deployment gate'
    completed = subprocess.run([node, '--test', 'tests/pages_artifact_wait.test.cjs'],
                               cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_both_pages_workflows_gate_the_uploaded_artifact_before_deploy():
    for workflow in ('premarket-research-forecast.yml', 'github-pages.yml'):
        config = yaml.safe_load((ROOT / '.github/workflows' / workflow).read_text())
        steps = config['jobs']['deploy']['steps']
        upload = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('actions/upload-pages-artifact@'))
        waits = [i for i, step in enumerate(steps) if step.get('uses') == './.github/actions/wait-for-pages-artifact']
        assert len(waits) == 1, f'{workflow} must wait for artifact visibility'
        deploy = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('actions/deploy-pages@'))
        assert upload < waits[0] < deploy
        assert steps[waits[0]]['with']['artifact_id'] == '${{ steps.' + steps[upload]['id'] + '.outputs.artifact_id }}'
        name = 'github-pages-${{ github.run_attempt }}'
        assert steps[upload].get('with', {}).get('name') == name
        assert steps[waits[0]]['with'].get('artifact_name') == name
        assert steps[deploy].get('with', {}).get('artifact_name') == name
        assert not steps[waits[0]].get('continue-on-error', False)
        assert not steps[deploy].get('if')  # default success(): failed wait blocks deploy

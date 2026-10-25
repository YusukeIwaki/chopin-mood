"""Production boundary contracts: trusted code, owner execution and no model repair."""
from pathlib import Path
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    return yaml.safe_load((ROOT / '.github/workflows' / name).read_text())


def test_candidate_uses_only_manual_owner_main_and_api_inputs():
    wf = workflow('studio-candidate.yml')
    triggers = wf.get('on', wf.get(True))
    assert set(triggers) == {'workflow_dispatch'}
    assert set(triggers['workflow_dispatch']['inputs']) == {'pr_number'}
    job = wf['jobs']['reproduce']
    assert "github.actor == 'YusukeIwaki'" in job['if']
    assert "github.triggering_actor == 'YusukeIwaki'" in job['if']
    assert "github.ref == 'refs/heads/main'" in job['if']
    checkout = [s for s in job['steps'] if s.get('uses', '').startswith('actions/checkout@')]
    assert len(checkout) == 1 and checkout[0]['with'] == {'ref': '${{ github.sha }}', 'persist-credentials': False}
    assert job['permissions']['contents'] == 'read'
    steps = job['steps']
    intake = next(i for i,s in enumerate(steps) if s.get('id') == 'prepare')
    restore = next(i for i,s in enumerate(steps) if s.get('id') == 'restore_codex')
    assert intake < restore
    visual = next(s for s in steps if s.get('id') == 'visual')
    assert '--candidate-context' in visual['with']['script'] and '--codex-led' in visual['with']['script']
    assert not any('muse' in str(s).lower() for s in steps)
    assert not any('commit_bundle' in str(s) for s in steps)


def test_release_serializes_selection_separately_from_model_auth():
    release = workflow('studio-release.yml')
    candidate = workflow('studio-candidate.yml')
    assert release['concurrency']['group'] != candidate['concurrency']['group']
    assert release['concurrency']['cancel-in-progress'] is False
    assert release['concurrency']['queue'] == 'max'
    assert release['jobs']['release']['permissions']['pull-requests'] == 'write'
    assert not any('secrets.' in str(s) for s in release['jobs']['release']['steps'])


def test_new_workflow_shell_blocks_have_valid_syntax():
    for name in ('studio-candidate.yml', 'studio-release.yml'):
        for job in workflow(name)['jobs'].values():
            for step in job['steps']:
                if 'run' in step:
                    result = subprocess.run(['bash', '-n'], input=step['run'], capture_output=True, text=True)
                    assert result.returncode == 0, (name,step.get('name'),result.stderr)

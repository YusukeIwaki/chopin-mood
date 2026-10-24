"""Independent regressions for failures found after the implementation review."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

from studio import artifacts, repair
from test_studio_repair import HEAD1, _bundle_files, make_review, _write_fake_docker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('name', ['receipt.json', 'review.json', 'hashes.json',
                                  'brief.json', 'provenance.json', 'qa.json'])
def test_null_json_never_bypasses_bundle_validation(name):
    files, meta = _bundle_files(HEAD1, make_review(HEAD1))
    files[name] = b'null'
    if name != 'hashes.json':
        files['hashes.json'] = json.dumps(artifacts.build_hash_manifest(
            {k: v for k, v in files.items() if k != 'hashes.json'})).encode()
    result = repair.verify_bundle_files(
        files, expected_head_sha=HEAD1, pr_number=meta['pr'],
        child=meta['child'], parent=meta['parent'],
        production_files=meta['production_files'])
    assert not result['ok'], name
    assert any(name in failure and 'object' in failure for failure in result['failures'])


def test_timeout_stops_term_ignoring_descendant_after_parent_exits(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child = tmp_path / 'child.py'
    child.write_text('import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n')
    parent = tmp_path / 'parent.py'
    parent.write_text(
        'import subprocess,sys,time,pathlib\n'
        'p=subprocess.Popen([sys.executable,sys.argv[1]])\n'
        'pathlib.Path(sys.argv[2]).write_text(str(p.pid))\n'
        'time.sleep(60)\n')
    pid = None
    try:
        with pytest.raises(repair.RepairInfrastructureError, match='timed out'):
            repair.run_agent_command(
                [sys.executable, str(parent), str(child), str(pidfile)],
                stdout_path=tmp_path / 'stdout', stderr_path=tmp_path / 'stderr', timeout=1)
        pid = int(pidfile.read_text())
        deadline = time.monotonic() + 2
        while True:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)],
                                   capture_output=True, text=True).stdout.strip()
            if not state or state.startswith('Z'):
                break
            assert time.monotonic() < deadline, f'descendant still running: {state}'
            time.sleep(.02)
    finally:
        if pid is None and pidfile.exists():
            pid = int(pidfile.read_text())
        if pid is not None:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('cleanup_fails', [False, True])
def test_muse_cleanup_state_controls_auth_persistence(tmp_path, cleanup_fails):
    _write_fake_docker(tmp_path / 'bin', tmp_path / 'docker.log', fail_rm=cleanup_fails)
    state_file = tmp_path / 'github-env'
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               GITHUB_ENV=str(state_file))
    call = lambda: repair.run_agent_command(
        [sys.executable, '-c', 'pass'], stdout_path=tmp_path / 'o',
        stderr_path=tmp_path / 'e', env=env, container_name='studio-repair-owned', timeout=5)
    if cleanup_fails:
        with pytest.raises(repair.RepairInfrastructureError, match='cleanup unconfirmed'):
            call()
    else:
        call()
    lines = state_file.read_text().splitlines()
    assert lines[0] == 'STUDIO_MUSE_CLEANUP_SAFE=false'
    assert lines[-1] == f"STUDIO_MUSE_CLEANUP_SAFE={'false' if cleanup_fails else 'true'}"
    for workflow in ('studio-production.yml', 'studio-auth-smoke.yml'):
        text = (ROOT / '.github/workflows' / workflow).read_text()
        assert 'persist_muse' not in text  # retired worker is never called by Actions


def test_initial_muse_wrapper_cleanup_failure_is_nonzero_and_blocks_persistence(tmp_path):
    _write_fake_docker(tmp_path / 'bin', tmp_path / 'docker.log', fail_rm=True)
    work, auth = tmp_path / 'work', tmp_path / 'auth'
    work.mkdir(); auth.mkdir()
    envfile = tmp_path / 'github-env'
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               MUSE_RUNTIME_HOME=str(auth), GITHUB_ENV=str(envfile))
    result = subprocess.run(['bash', '.github/studio/scripts/muse-run.sh', str(work)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'credential persistence is disabled' in result.stderr
    assert envfile.read_text().splitlines()[-1] == 'STUDIO_MUSE_CLEANUP_SAFE=false'

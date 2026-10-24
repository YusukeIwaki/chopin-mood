"""Workflow/runtime static integration + fake CLI subprocess tests.

Inspect the actual workflow YAML/scripts and execute fake `muse`/`codex`
binaries with the exact production flags. No real models, no network.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROD = (ROOT / ".github/workflows/studio-production.yml").read_text()
CI = (ROOT / ".github/workflows/studio-ci.yml").read_text()
SMOKE_WF = (ROOT / ".github/workflows/studio-auth-smoke.yml").read_text()






def test_ci_checks_out_event_revision_and_smoke_size():
    checkout = CI.split("actions/checkout@v7")[1].split("setup-python")[0]
    assert "ref: main" not in checkout
    smoke = (ROOT / "scripts/studio_smoke.py").read_text()
    assert "default=288" in smoke and "default=512" in smoke


def test_auth_smoke_workflow_contract():
    assert "workflow_dispatch" in SMOKE_WF
    assert "gpt-6.1-sol" in SMOKE_WF and "muse" not in SMOKE_WF.lower()
    assert "${{ github.sha }}" in SMOKE_WF
    for forbidden in ("issues: write", "pull-requests: write"):
        assert forbidden not in SMOKE_WF


def test_all_workflow_referenced_scripts_exist():
    import re
    for wf in (ROOT / ".github/workflows").glob("*.yml"):
        text = wf.read_text()
        for m in re.finditer(r"(scripts/studio_\w+\.py|\.github/studio/scripts/\w+\.sh|studio/[a-z_]+\.py)", text):
            assert (ROOT / m.group(1)).exists(), (wf.name, m.group(1))
        for m in re.finditer(r"\.github/studio/(schemas|prompts)/([\w.-]+)", text):
            assert (ROOT / ".github/studio" / m.group(1) / m.group(2)).exists()


def test_schemas_strict_required_covers_properties():
    for name in ("planner-output.schema.json", "review.schema.json",
                 "storyboard.schema.json"):
        schema = json.loads((ROOT / ".github/studio/schemas" / name).read_text())
        assert set(schema.get("required", [])) == set(schema.get("properties", {})), name


def test_actual_muse_wrapper_mounts_only_work_and_provider_auth(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                      "with open(os.environ['DOCKER_CALLS'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
    docker.chmod(0o755)
    work, auth = tmp_path / "work", tmp_path / "auth"
    work.mkdir(); auth.mkdir()
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               MUSE_RUNTIME_HOME=str(auth), DOCKER_CALLS=str(calls))
    subprocess.run(["bash", str(ROOT / ".github/studio/scripts/muse-run.sh"), str(work)],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    run_invocations = [a for a in invocations if "--mount" in a]
    assert run_invocations, invocations
    args = run_invocations[-1]
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert mounts == [f"type=bind,source={work},target=/workspace",
                      f"type=bind,source={auth},target=/agent-config"]
    assert "--user" in args and "--cap-drop=ALL" in args
    for flag in ("--yolo", "muse-wonder-1.0-contributor", "--max-model-steps", "80",
                 "--prompt-file", "prompt.md", "--reasoning-effort", "max"):
        assert flag in args
    assert "GH_TOKEN" not in " ".join(args)


def test_script_bootstrap_from_foreign_cwd(tmp_path):
    r = subprocess.run([sys.executable, str(ROOT / "scripts/studio_download.py"), "--help"],
                       capture_output=True, text=True, cwd=str(tmp_path), timeout=60)
    assert r.returncode == 0, r.stderr[-500:]
    r2 = subprocess.run([sys.executable, "-m", "studio", "fingerprint",
                         "--source-url", "https://github.com/user-attachments/files/1/x",
                         "--source-sha256", "a" * 64, "--art-notes", "line1\nline2"],
                        capture_output=True, text=True, cwd=str(ROOT), timeout=60)
    assert r2.returncode == 0, r2.stderr[-500:]
    assert json.loads(r2.stdout)["fingerprint"]


def test_fingerprint_includes_multiline_notes():
    from studio import fingerprint
    a = fingerprint.canonical_fingerprint(source_url="u", source_sha256="a" * 64,
                                          art_notes="one\ntwo")
    b = fingerprint.canonical_fingerprint(source_url="u", source_sha256="a" * 64,
                                          art_notes="one")
    assert a != b


def test_codex_cli_is_not_version_pinned_and_version_is_logged():
    for text in (PROD, SMOKE_WF):
        assert "CODEX_VERSION" not in text
        assert "0.144.6" not in text
        assert "@openai/codex@" not in text.replace("@openai/codex@latest", "")
        assert 'npm install --global "@openai/codex@latest"' in text
        assert "codex --version" in text
        assert "Codex pinned" not in text
    # Production installs latest in both the plan and build jobs.
    assert PROD.count('npm install --global "@openai/codex@latest"') == 1
    assert SMOKE_WF.count('npm install --global "@openai/codex@latest"') == 1


def test_exact_models_preserved_after_unpinning():
    assert PROD.count("--model gpt-6.1-sol") == 1
    assert SMOKE_WF.count("--model gpt-6.1-sol") == 1
    assert PROD.count('model_reasoning_effort="xhigh"') == 1
    assert SMOKE_WF.count('model_reasoning_effort="xhigh"') == 1


def _step_block(name_fragment):
    start = PROD.index(name_fragment)
    nxt = PROD.find("\n      - name:", start + 1)
    return PROD[start:] if nxt == -1 else PROD[start:nxt]








def test_agent_failure_diagnostics_are_guarded_and_preserve_exit():
    line = next(ln for ln in PROD.splitlines() if 'Codex reviewer failed with exit' in ln)
    assert 'report-agent-error.py' in line and '|| true' in line
    assert 'exit "$code"' in line

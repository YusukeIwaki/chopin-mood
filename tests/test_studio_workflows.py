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


def test_production_uses_shared_isolated_muse_runner():
    assert "scripts/muse-run.sh" in PROD and "scripts/muse-run.sh" in SMOKE_WF
    assert "muse exec --yolo" not in PROD  # no unconfined host exec
    assert "Muse failed with exit" in PROD


def test_production_workflow_ordering_and_gates():
    # Source download happens AFTER the Muse step (no full audio during --yolo).
    assert PROD.index("Run Muse in isolated container") < PROD.index("Download source AFTER Muse")
    # Compact matrix JSON (single-line GITHUB_OUTPUT safe).
    assert 'separators=(",", ":")' in PROD
    # Changed-gated write tokens for both agents.
    assert PROD.count("outputs.changed == 'true'") >= 4
    # No swallowed model exit codes: planner/reviewer propagate.
    assert 'Codex planner failed with exit' in PROD
    assert 'Codex reviewer failed with exit' in PROD
    assert 'Muse failed with exit' in PROD
    # ffmpeg installed, python module CLI used, both hashes verified.
    assert "apt-get install -y -qq ffmpeg" in PROD
    assert "python3 -m studio" in PROD
    assert "attachment hash changed since planning" in PROD
    assert "decoded WAV hash does not match plan context" in PROD
    # Labelled review sections.
    for label in ("## 1 SOURCE BRIEF", "## 2 STORYBOARD",
                  "## 3 PRODUCTION FILES", "## 4 PLAN", "## 5 HASHES"):
        assert label in PROD, label


def test_ci_checks_out_event_revision_and_smoke_size():
    checkout = CI.split("actions/checkout@v7")[1].split("setup-python")[0]
    assert "ref: main" not in checkout
    smoke = (ROOT / "scripts/studio_smoke.py").read_text()
    assert "default=288" in smoke and "default=512" in smoke


def test_auth_smoke_workflow_contract():
    assert "workflow_dispatch" in SMOKE_WF
    assert "gpt-6.1-sol" in SMOKE_WF and "scripts/muse-run.sh" in SMOKE_WF
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
    assert PROD.count('npm install --global "@openai/codex@latest"') == 2
    assert SMOKE_WF.count('npm install --global "@openai/codex@latest"') == 1


def test_exact_models_preserved_after_unpinning():
    assert PROD.count("--model gpt-6.1-sol") == 2
    assert SMOKE_WF.count("--model gpt-6.1-sol") == 1
    assert PROD.count('model_reasoning_effort="xhigh"') == 2
    assert SMOKE_WF.count('model_reasoning_effort="xhigh"') == 1


def _step_block(name_fragment):
    start = PROD.index(name_fragment)
    nxt = PROD.find("\n      - name:", start + 1)
    return PROD[start:] if nxt == -1 else PROD[start:nxt]


def test_repair_wiring_order_args_and_final_refs():
    # Sequence preserved: initial Muse -> download -> render -> commit
    # -> receipt -> initial Codex review -> repair -> final upload/publish.
    order = [
        "Run Muse in isolated container",
        "Download source AFTER Muse",
        "Validate storyboard and render",
        "Commit production bundle and create or reuse PR",
        "Generate artifact receipt and hash manifest",
        "Run Codex evidence review",
        "Bounded aesthetic repair",
        "Validate bundle allowlist before upload",
        "Upload production artifact",
        "Publish review (ping only on pass",
    ]
    idx = [PROD.index(s) for s in order]
    assert idx == sorted(idx), list(zip(order, idx))
    # Repair CLI: actual entrypoint + inputs from the task contract.
    repair_block = _step_block("Bounded aesthetic repair")
    assert "id: repair" in repair_block
    assert "timeout-minutes: 75" in repair_block
    assert "GH_TOKEN: ${{ github.token }}" in repair_block
    assert "GITHUB_REPOSITORY: ${{ github.repository }}" in repair_block
    for arg in (
        "python3 scripts/studio_repair.py",
        "--work output/studio/work",
        '--muse-work "$RUNNER_TEMP/muse-work"',
        "--wav output/studio/source/source.wav",
        "--parent ${{ needs.preflight.outputs.issue_number }}",
        "--child ${{ matrix.child_issue }}",
        "--engine-sha ${{ github.sha }}",
        "--run-id ${{ github.run_id }}",
        "--run-attempt ${{ github.run_attempt }}",
    ):
        assert arg in repair_block, arg
    # Repair agents are inside the Python module, not new shell Codex calls.
    assert PROD.count("--model gpt-6.1-sol") == 2
    # Final artifact + publish use the LAST repair outputs, never initial.
    upload_block = _step_block("Upload production artifact")
    assert "steps.repair.outputs.pr_number" in upload_block
    assert "steps.repair.outputs.head_sha" in upload_block
    assert "steps.commit.outputs" not in upload_block
    publish_block = _step_block("Publish review (ping only on pass")
    assert "steps.repair.outputs.pr_number" in publish_block
    assert "steps.repair.outputs.head_sha" in publish_block
    assert "steps.commit.outputs" not in publish_block
    assert "--repair-result output/studio/work/repair-result.json" in publish_block
    # Initial receipt/review keep the initial-commit binding.
    receipt_block = _step_block("Generate artifact receipt and hash manifest")
    assert "steps.commit.outputs.head_sha" in receipt_block
    review_block = _step_block("Run Codex evidence review")
    assert "steps.commit.outputs.head_sha" in review_block
    # Fail-closed allowlist still guards the final (12-file) bundle.
    assert "python3 -m studio check-bundle --dir output/studio/work/bundle" in PROD
    # No recursive dispatch / second concurrency group / new permissions.
    assert PROD.count("concurrency:") == 1
    assert PROD.count("group: studio-agent-auth") == 1
    assert "gh workflow run" not in PROD and "workflow_dispatch" in PROD


def test_repair_history_artifact_is_validated_and_not_release_eligible():
    validate_block = _step_block("Validate review-attempt history bundles")
    assert "id: validate_history" in validate_block
    assert "validate_attempt_history" in validate_block
    assert "review-attempts" in validate_block
    history_block = _step_block("Upload review-attempt history")
    assert ("name: studio-review-attempts-child-${{ matrix.child_issue }}"
            "-run-${{ github.run_id }}-a${{ github.run_attempt }}") in history_block
    assert "path: output/studio/work/review-attempts/" in history_block
    assert "retention-days: 90" in history_block
    assert "steps.validate_history.outcome == 'success'" in history_block
    # History name must not match the release-eligible production pattern.
    assert not history_block.split("name: ", 1)[1].splitlines()[0].strip().startswith(
        "studio-child-")
    # Validation happens before the history upload.
    assert PROD.index("Validate review-attempt history bundles") < PROD.index(
        "Upload review-attempt history")
    # Final summary distinguishes quality status from workflow completion.
    summary_block = _step_block("Summarize final quality status")
    assert "Studio final quality status" in summary_block
    assert "owner-ready: NO" in summary_block
    assert "attempt_count" in summary_block


def test_muse_auth_persist_moved_after_repair_and_timeouts_bounded():
    # Build + initial-Muse timeouts bound the total; repair step covers
    # max 2 repairs x (Muse 900s + Codex 900s).
    import yaml
    wf = yaml.safe_load(PROD)
    assert wf["jobs"]["build"]["timeout-minutes"] == 120
    muse_block = _step_block("Run Muse in isolated container")
    assert "timeout-minutes: 15" in muse_block
    assert "timeout --kill-after=10s 900 bash .github/studio/scripts/muse-run.sh" in muse_block
    repair_block = _step_block("Bounded aesthetic repair")
    assert int(repair_block.split("timeout-minutes:")[1].split()[0]) >= 60
    # The whole Muse persist block moved AFTER the last model call
    # (the repair step) and before cleanup; exactly one copy exists.
    assert PROD.count("id: persist_muse") == 1
    assert PROD.count("id: muse_write") == 1
    repair_at = PROD.index("Bounded aesthetic repair")
    persist_at = PROD.index("Persist refreshed Muse auth")
    codex_review_at = PROD.index("Run Codex evidence review")
    assert persist_at > repair_at > codex_review_at
    assert persist_at > PROD.index("Download source AFTER Muse")
    assert persist_at < PROD.rindex("Cleanup credentials")
    persist_block = _step_block("Persist refreshed Muse auth")
    assert "always() && steps.restore_muse.outcome == 'success'" in persist_block
    write_block = _step_block("Create Muse write token")
    assert "steps.persist_muse.outputs.changed == 'true'" in write_block
    # Codex persist/cleanup stay always-gated after repair as well.
    assert PROD.rindex("Persist refreshed Codex auth") > repair_at


def test_agent_failure_diagnostics_are_guarded_and_preserve_exit():
    script = ".github/studio/scripts/report-agent-error.py"
    assert (ROOT / script).exists()
    # Planner, reviewer and Muse failures in production.
    assert PROD.count(script) >= 3
    assert PROD.count(f'python3 {script} "$RUNNER_TEMP/codex.stderr" || true') == 2
    assert PROD.count(f'python3 {script} "$RUNNER_TEMP/muse.stderr" || true') == 1
    # The original nonzero agent exit is still propagated after diagnostics.
    assert 'Codex planner failed with exit $code' in PROD
    assert 'Codex reviewer failed with exit $code' in PROD
    assert 'Muse failed with exit $code' in PROD
    for label in ("Codex planner failed", "Codex reviewer failed", "Muse failed"):
        line = next(ln for ln in PROD.splitlines() if label in ln)
        assert "report-agent-error.py" in line and "|| true" in line
        assert 'exit "$code"' in line

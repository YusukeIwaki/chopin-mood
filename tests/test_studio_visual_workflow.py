"""Static wiring tests for the staged visual production workflow.

Parses the real workflow YAML and checks gates, exact JS argv, env
passing, timeouts, credential persistence and history artifacts.
No network, models or credentials.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PROD_TEXT = (ROOT / ".github/workflows/studio-production.yml").read_text()
WF = yaml.safe_load(PROD_TEXT)
BUILD = WF["jobs"]["build"]
STEPS = BUILD["steps"]


def _step(name):
    for s in STEPS:
        if s.get("name") == name:
            return s
    raise AssertionError(f"step not found: {name}")


def _index(name):
    return [s.get("name") for s in STEPS].index(name)


def test_timeouts_bounded_and_agent_unchanged():
    assert BUILD["timeout-minutes"] == 300
    assert _step("Staged visual preflight (still to motion)")["timeout-minutes"] == 150
    repair = _step("Bounded aesthetic repair (max 2 Muse + render + Codex re-review)")
    assert repair["timeout-minutes"] == 180
    muse = _step("Run Muse in isolated container (no source or trusted checkout mounted)")
    assert muse["timeout-minutes"] == 15
    assert "timeout --kill-after=10s 900 bash .github/studio/scripts/muse-run.sh" in muse["run"]


def test_visual_step_is_github_script_with_exact_env_argv():
    step = _step("Staged visual preflight (still to motion)")
    assert step["id"] == "visual"
    assert step["uses"] == "actions/github-script@v8"
    script = step["with"]["script"]
    assert "${{" not in script
    assert "exec.exec('python3'" in script
    for token in ("'scripts/studio_visual_preflight.py'", "'--work'",
                  "process.env.VISUAL_WORK", "'--muse-work'",
                  "process.env.VISUAL_MUSE_WORK", "'--wav'",
                  "process.env.VISUAL_WAV", "'--parent'",
                  "process.env.PARENT_ISSUE", "'--child'",
                  "process.env.CHILD_ISSUE", "'--engine-sha'",
                  "process.env.ENGINE_SHA", "'--run-id'",
                  "process.env.RUN_ID", "'--run-attempt'",
                  "process.env.RUN_ATTEMPT"):
        assert token in script, token
    env = step["env"]
    assert env["VISUAL_WORK"] == "output/studio/work"
    assert env["VISUAL_WAV"] == "output/studio/source/source.wav"
    assert "muse-work" in str(env["VISUAL_MUSE_WORK"])
    assert env["PARENT_ISSUE"] == "${{ needs.preflight.outputs.issue_number }}"
    assert env["CHILD_ISSUE"] == "${{ matrix.child_issue }}"
    assert env["ENGINE_SHA"] == "${{ github.sha }}"
    assert env["RUN_ID"] == "${{ github.run_id }}"
    assert env["RUN_ATTEMPT"] == "${{ github.run_attempt }}"


def test_final_repair_step_is_github_script_and_requires_visual():
    step = _step("Bounded aesthetic repair (max 2 Muse + render + Codex re-review)")
    assert step["id"] == "repair"
    assert step["uses"] == "actions/github-script@v8"
    script = step["with"]["script"]
    assert "${{" not in script
    assert "exec.exec('python3'" in script
    assert "'scripts/studio_repair.py'" in script
    assert "'--require-visual-preflight'" in script
    for token in ("process.env.REPAIR_WORK", "process.env.REPAIR_MUSE_WORK",
                  "process.env.REPAIR_WAV", "process.env.PARENT_ISSUE",
                  "process.env.CHILD_ISSUE", "process.env.ENGINE_SHA",
                  "process.env.RUN_ID", "process.env.RUN_ATTEMPT"):
        assert token in script, token
    env = step["env"]
    assert env["REPAIR_WORK"] == "output/studio/work"
    assert env["REPAIR_WAV"] == "output/studio/source/source.wav"


def test_codex_auth_block_moved_after_source_before_visual_single_copy():
    names = [s.get("name") for s in STEPS]
    assert _index("Download source AFTER Muse and verify both hashes") < _index("Create Codex read token (review)")
    assert _index("Create Codex read token (review)") < _index("Staged visual preflight (still to motion)")
    assert _index("Restore Codex auth (review)") < _index("Staged visual preflight (still to motion)")
    build_text = PROD_TEXT.split("  build:", 1)[1]
    assert build_text.count("id: codex_read") == 1
    assert build_text.count("id: restore_codex") == 1
    # Direct shell Codex calls remain only planner + final review.
    assert PROD_TEXT.count("--model gpt-6.1-sol") == 2


def test_artifact_tools_install_exports_path_not_tokens():
    install = _step("Install ffmpeg, tools (Codex latest; Muse official launcher)")
    run = install["run"]
    assert 'npm install --prefix "$RUNNER_TEMP/studio-artifact-tools" --ignore-scripts --no-audit --no-fund @actions/artifact@2.3.2' in run
    assert 'echo "STUDIO_ARTIFACT_TOOLS=$RUNNER_TEMP/studio-artifact-tools" >> "$GITHUB_ENV"' in run
    assert "ACTIONS_RUNTIME_TOKEN" not in run
    assert "GH_TOKEN" not in run


def test_all_full_stages_gated_on_visual_proceed():
    gated = [
        "Validate storyboard and render (trusted renderer + encoded QA)",
        "Commit production bundle and create or reuse PR",
        "Generate artifact receipt and hash manifest (receipt not committed)",
        "Run Codex evidence review (labelled brief/storyboard/files/images/QA)",
        "Bounded aesthetic repair (max 2 Muse + render + Codex re-review)",
        "Validate bundle allowlist before upload",
        "Upload production artifact (child/head/run/attempt in name)",
        "Publish review (ping only on pass + mechanical pass; retry-safe)",
    ]
    for name in gated:
        assert _step(name).get("if") == "steps.visual.outputs.proceed == 'true'", name


def test_full_render_receives_visual_approval_and_exact_engine_sha():
    step = _step("Validate storyboard and render (trusted renderer + encoded QA)")
    assert "--visual-approval output/studio/work/visual-approval.json" in step["run"]
    assert "--engine-sha ${{ github.sha }}" in step["run"]


def test_persist_both_credentials_and_cleanup_always():
    for name in ("Persist refreshed Muse auth", "Persist refreshed Codex auth",
                 "Cleanup credentials"):
        cond = str(_step(name).get("if", ""))
        assert "always()" in cond, name
    # Persist/cleanup are NOT gated on visual proceed.
    for name in ("Persist refreshed Muse auth", "Persist refreshed Codex auth",
                 "Cleanup credentials"):
        assert "steps.visual" not in str(_step(name).get("if", ""))


def test_histories_existence_guarded_and_visual_aggregate():
    validate = _step("Validate review-attempt history bundles (fully reviewed only)")
    assert "hashFiles('output/studio/work/review-attempts/**')" in str(validate["if"])
    build_hist = _step("Build aggregate visual history")
    assert build_hist["id"] == "validate_visual_history"
    assert "always()" in str(build_hist["if"])
    assert "hashFiles('output/studio/work/visual-state.json')" in str(build_hist["if"])
    assert "scripts/studio_visual_preflight.py --work output/studio/work --validate-history" in build_hist["run"]
    upload = _step("Upload aggregate visual history (not release-eligible)")
    assert "steps.validate_visual_history.outcome == 'success'" in str(upload["if"])
    name = upload["with"]["name"]
    assert name == "studio-visual-reviews-child-${{ matrix.child_issue }}-run-${{ github.run_id }}-a${{ github.run_attempt }}"
    assert not name.startswith("studio-child-")
    assert upload["with"]["retention-days"] == 90
    assert upload["with"]["path"] == "output/studio/work/visual-history/"


def test_early_visual_block_summary_owner_not_ready_no_head():
    step = _step("Summarize early visual block")
    assert "always()" in str(step["if"])
    assert "steps.visual.outputs.proceed == 'false'" in str(step["if"])
    assert "owner-ready: NO" in step["run"]
    assert "no new production head" in step["run"]


def test_single_global_concurrency_and_no_midstage_human_gate():
    assert PROD_TEXT.count("concurrency:") == 1
    assert PROD_TEXT.count("group: studio-agent-auth") == 1
    assert "issue_comment" not in PROD_TEXT
    visual = _step("Staged visual preflight (still to motion)")
    assert "environment" not in visual

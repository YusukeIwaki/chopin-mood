#!/usr/bin/env python3
"""Bounded aesthetic repair entrypoint (AFTER initial Codex review).

Contract (called by the production workflow after its initial review
and BEFORE final upload/publish):

    studio_repair.py --work output/studio/work \
        --muse-work "$RUNNER_TEMP/muse-work" \
        --wav output/studio/source/source.wav \
        --parent N --child N --engine-sha SHA --run-id N --run-attempt N

Validates the initial 12-file bundle (hashes/receipt/mechanical QA/
strictly-bound NEW review), snapshots it as ``review-attempts/
attempt-01``, then: pass -> finish; route engine -> needs_engine with
no Muse call; route storyboard -> at most MAX_REPAIRS (2) fresh
Muse -> render -> commit -> receipt -> Codex re-review cycles via
``studio.repair.run_repair_loop``. Ends with ONLY the final reviewed
allowlisted bundle at ``work/bundle``, matching ``work/pr.json`` /
``work/storyboard.json``, an operational ``work/repair-result.json``
(outside the bundle) and ``head_sha``/``pr_number``/``status`` in
GITHUB_OUTPUT. Malformed/stale/closed/source-drift/mechanical
failures exit non-zero (fail closed) before any publication. No raw
provider logs, prompts, secrets or source audio are printed or copied
into attempt snapshots.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
import shutil
from pathlib import Path

from studio import artifacts, config, controller, gating, repair
from studio import visual_pipeline as vpipe
from studio.github_client import GitHubClient
from studio.render_context import prepare_context

ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - malformed work input is infra
        raise repair.RepairInfrastructureError(
            f"invalid JSON input: {Path(path).name}") from e


def _read_json_bytes(data: bytes, name: str):
    try:
        return json.loads(data)
    except Exception as e:  # noqa: BLE001
        raise repair.RepairInfrastructureError(
            f"invalid JSON in bundle: {name}") from e


def _fresh_dir(path: Path) -> Path:
    if Path(path).exists():
        shutil.rmtree(path)
    Path(path).mkdir(parents=True)
    return Path(path)


def _verify_pr_and_source(gh, *, pr_number: int, expected_head: str,
                          parent: int, child: int) -> None:
    pr = gh.request("GET", f"/pulls/{pr_number}")
    failures = []
    if pr.get("state") != "open":
        failures.append("PR is not open")
    if (pr.get("head") or {}).get("sha") != expected_head:
        failures.append("PR head moved since review (stale head)")
    if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != gh.repo:
        failures.append("PR head repository mismatch")
    if (pr.get("base") or {}).get("ref") != config.DEFAULT_BRANCH:
        failures.append("PR base must be main")
    source = gh.get_issue(int(parent))
    if source.get("state") != "open":
        failures.append("source issue is not open")
    if (source.get("user") or {}).get("login") != config.OWNER:
        failures.append("source issue author must be the repository owner")
    child_issue = gh.get_issue(int(child))
    if child_issue.get("state") != "open":
        failures.append("child issue is not open")
    if failures:
        raise repair.RepairInfrastructureError(
            "PR/source guard failed: " + "; ".join(failures))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--muse-work", required=True)
    ap.add_argument("--wav", required=True)
    ap.add_argument("--parent", required=True, type=int)
    ap.add_argument("--child", required=True, type=int)
    ap.add_argument("--engine-sha", required=True)
    ap.add_argument("--run-id", required=True, type=int)
    ap.add_argument("--run-attempt", required=True, type=int)
    ap.add_argument("--require-visual-preflight", action="store_true",
                    help="require an approved visual-state for the initial "
                         "storyboard and a fresh still+motion pass for "
                         "every repaired storyboard (production sets this)")
    ap.add_argument("--visual-state", default="",
                    help="visual-state.json path (default: <work>/visual-state.json)")
    a = ap.parse_args()
    try:
        return _run(a)
    except repair.RepairInfrastructureError as e:
        print(json.dumps({"ok": False, "status": "infrastructure_failure",
                          "failures": [str(e)]}))
        repair.append_step_summary(
            {"status": "infrastructure_failure", "attempt_count": 0,
             "max_repairs": repair.MAX_REPAIRS, "final_head_sha": "",
             "pr_number": "", "next_action": "fix infrastructure failure"})
        return 2
    except Exception as e:  # noqa: BLE001 - fail closed, no raw details
        print(json.dumps({"ok": False, "status": "infrastructure_failure",
                          "failures": [f"unexpected {type(e).__name__}"]}))
        return 2


def _require_obj(value, name: str):
    if not isinstance(value, dict):
        raise repair.RepairInfrastructureError(f"{name} must be an object")
    return value


def _check_source_and_candidate(*, wav_path, brief, candidate) -> None:
    """Cheap drift checks that must precede any Muse call."""
    _require_obj(brief, "brief.json")
    if brief.get("candidate") != candidate:
        raise repair.RepairInfrastructureError(
            "candidate does not match brief candidate (source drift)")
    try:
        wav_sha = repair.sha256_file(wav_path)
    except Exception as e:  # noqa: BLE001 - missing source is infra, no detail
        raise repair.RepairInfrastructureError(
            "source WAV unavailable for drift check") from e
    src_check = repair.verify_source_against_brief(wav_sha, brief)
    if not src_check["ok"]:
        raise repair.RepairInfrastructureError("; ".join(src_check["failures"]))


def _run(a) -> int:
    work = Path(a.work)
    bundle_dir = work / "bundle"
    attempts_dir = work / "review-attempts"
    brief = _require_obj(_load_json(work / "brief.json"), "brief.json")
    candidate = _load_json(work / "candidate.json")
    scenes_obj = _load_json(work / "scenes.json")
    deterministic_scenes = scenes_obj.get("scenes", []) \
        if isinstance(scenes_obj, dict) else scenes_obj
    if not isinstance(deterministic_scenes, list):
        raise repair.RepairInfrastructureError("scenes.json scenes must be a list")
    pr_data = _require_obj(_load_json(work / "pr.json"), "pr.json")
    initial_head = pr_data.get("head_sha")
    try:
        pr_number = int(pr_data.get("pr_number"))
    except Exception as e:  # noqa: BLE001
        raise repair.RepairInfrastructureError("pr.json missing pr_number") from e
    if not initial_head or not isinstance(initial_head, str):
        raise repair.RepairInfrastructureError("pr.json missing head_sha")
    # 1. Validate initial bundle/hash/receipt/mechanical/review binding.
    files = repair.read_bundle_files(bundle_dir)
    check = repair.verify_bundle_files(
        files, expected_head_sha=initial_head, pr_number=pr_number,
        child=a.child, parent=a.parent, run_id=a.run_id,
        run_attempt=a.run_attempt, engine_sha=a.engine_sha,
        production_files=pr_data.get("blob_sha256"), require_repair=True)
    if not check["ok"]:
        raise repair.RepairInfrastructureError(
            "initial bundle validation failed: " + "; ".join(check["failures"]))
    initial_review = check["review"]
    initial_mechanical = check["mechanical_pass"]
    initial_storyboard = _read_json_bytes(files["storyboard.json"], "storyboard.json")
    work_storyboard = _load_json(work / "storyboard.json")
    if repair.canonical_storyboard(work_storyboard) != \
            repair.canonical_storyboard(initial_storyboard):
        raise repair.RepairInfrastructureError(
            "work storyboard does not match reviewed bundle storyboard")
    initial_plan = _read_json_bytes(files["plan.json"], "plan.json")
    initial_media = repair.media_sha_map(files)
    # Shared visual budget: load/validate prior visual state BEFORE any
    # Muse call. Production (--require-visual-preflight) fails closed
    # when it is missing or does not approve the initial storyboard.
    visual_state = None
    prior_visual_repairs = 0
    require_visual_preflight = bool(
        getattr(a, "require_visual_preflight", False))
    _visual_state_arg = getattr(a, "visual_state", "") or ""
    visual_state_path = Path(_visual_state_arg) if _visual_state_arg else work / vpipe.STATE_FILENAME
    if visual_state_path.is_file():
        try:
            visual_state = vpipe.load_visual_state(visual_state_path.parent)
        except vpipe.VisualInfrastructureError as e:
            raise repair.RepairInfrastructureError(str(e)) from e
        if visual_state.get("parent_issue") != a.parent \
                or visual_state.get("child_issue") != a.child \
                or visual_state.get("engine_sha") != a.engine_sha \
                or visual_state.get("run_id") != a.run_id \
                or visual_state.get("run_attempt") != a.run_attempt:
            raise repair.RepairInfrastructureError(
                "visual state identity does not match this run")
        if vpipe.canonical_hash(brief) != visual_state.get("brief_sha256") \
                or vpipe.canonical_hash(candidate) != visual_state.get("candidate_sha256"):
            raise repair.RepairInfrastructureError(
                "visual state brief/candidate does not match work inputs")
        prior_visual_repairs = int(visual_state.get("repairs_used", 0))
    elif require_visual_preflight:
        raise repair.RepairInfrastructureError(
            "visual preflight required but visual state missing")
    if require_visual_preflight:
        sb_hash = vpipe.canonical_hash(initial_storyboard)
        if not any(v.get("storyboard_sha256") == sb_hash
                   and v.get("outcome") == "passed"
                   for v in (visual_state or {}).get("versions", [])):
            raise repair.RepairInfrastructureError(
                "initial storyboard has no approved visual preflight")
    remaining_visual_repairs = max(0, repair.MAX_REPAIRS - prior_visual_repairs)
    # 2. Source/candidate drift BEFORE any snapshot or model call.
    _check_source_and_candidate(wav_path=a.wav, brief=brief, candidate=candidate)
    # 3. Fresh owner/open source+child and current PR/head guard BEFORE
    # the initial snapshot and any finish (including pass/engine).
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN")
    if not repo or not token:
        raise repair.RepairInfrastructureError(
            "GitHub repository/token unavailable for repair")
    gh = GitHubClient(repo, token)
    _verify_pr_and_source(gh, pr_number=pr_number,
                          expected_head=initial_head, parent=a.parent,
                          child=a.child)
    # Immutable history snapshot (allowlisted 12 only).
    repair.snapshot_bundle(bundle_dir, attempts_dir, 1)
    gate = gating.evaluate_review_gate(
        initial_review, mechanical_pass=initial_mechanical,
        expected_head_sha=initial_head, require_repair=True)
    route = gating.review_route(initial_review)
    state = {"head": initial_head, "storyboard": initial_storyboard,
             "media": initial_media, "review": initial_review,
             "commits": {1: pr_data}}

    def _finish(loop_result) -> int:
        result = repair.build_repair_result(
            loop_result, parent=a.parent, child=a.child)
        # Shared global budget: total = early visual repairs + this loop.
        _total = prior_visual_repairs + int(result.get("repairs_attempted") or 0)
        for _k in ("repairs_attempted", "repair_count",
                   "muse_repair_calls", "repairs_used"):
            result[_k] = _total
        result["visual_repairs_before_final"] = prior_visual_repairs
        result["max_repairs"] = repair.MAX_REPAIRS
        final_attempt = int(loop_result["attempt_count"])
        final_head = loop_result.get("final_head_sha") or loop_result.get("head_sha")
        final_pr = int(loop_result.get("final_pr_number")
                       or loop_result.get("pr_number") or pr_number)
        # Final fresh guard + strict final-bundle validation BEFORE
        # replacing the bundle or writing any publishable output.
        _verify_pr_and_source(gh, pr_number=final_pr,
                              expected_head=final_head, parent=a.parent,
                              child=a.child)
        _check_source_and_candidate(
            wav_path=a.wav, brief=brief, candidate=candidate)
        snapshot = attempts_dir / f"attempt-{final_attempt:02d}"
        final_files = repair.read_bundle_files(snapshot)
        commit_meta = state["commits"].get(final_attempt)
        pf_map = commit_meta.get("blob_sha256") if isinstance(commit_meta, dict) \
            else pr_data.get("blob_sha256")
        final_check = repair.verify_bundle_files(
            final_files, expected_head_sha=final_head, pr_number=final_pr,
            child=a.child, parent=a.parent, run_id=a.run_id,
            run_attempt=a.run_attempt, engine_sha=a.engine_sha,
            production_files=pf_map, require_repair=True)
        if not final_check["ok"]:
            raise repair.RepairInfrastructureError(
                "final bundle validation failed: "
                + "; ".join(final_check["failures"]))
        repair.replace_bundle_from_snapshot(snapshot, bundle_dir)
        if final_attempt > 1:
            if commit_meta is None:
                raise repair.RepairInfrastructureError(
                    "missing commit metadata for final attempt")
            (work / "pr.json").write_text(json.dumps(commit_meta, indent=2))
        (work / "storyboard.json").write_bytes(
            (snapshot / "storyboard.json").read_bytes())
        repair.write_repair_result(work, result)
        repair.append_github_output(result)
        repair.append_step_summary(result)
        print(json.dumps({"ok": True, "status": result["status"],
                          "attempt_count": result["attempt_count"],
                          "repairs_attempted": result.get("repairs_attempted"),
                          "head_sha": result["final_head_sha"],
                          "pr_number": result["pr_number"]}))
        return 0

    # 4. Pass / engine finish immediately (no expensive model call).
    if gate["ok"] or route == "engine":
        def _never_run(*_args):
            raise repair.RepairInfrastructureError("repair must not run")

        return _finish(repair.run_repair_loop(
            initial_review=initial_review,
            initial_mechanical_pass=initial_mechanical,
            initial_head_sha=initial_head, initial_pr_number=pr_number,
            attempt_runner=_never_run,
            initial_storyboard=initial_storyboard,
            initial_media_sha256=initial_media))
    if route != "storyboard":
        raise repair.RepairInfrastructureError(
            f"failing review has unroutable repair route: {route!r}")
    runner_temp = Path(os.environ.get("RUNNER_TEMP")
                       or Path(a.muse_work).parent)
    runner_temp.mkdir(parents=True, exist_ok=True)
    muse_run_script = ROOT / ".github/studio/scripts/muse-run.sh"
    report_script = ROOT / ".github/studio/scripts/report-agent-error.py"
    base_prompt = (ROOT / ".github/studio/prompts/muse-storyboard.md").read_text()
    review_prompt_base = (ROOT / ".github/studio/prompts/codex-review.md").read_text()
    schema_text = (ROOT / ".github/studio/schemas/storyboard.schema.json").read_text()
    review_schema = ROOT / ".github/studio/schemas/review.schema.json"

    def _snapshot_cb(attempt_number, outcome):
        repair.snapshot_bundle(outcome["bundle_dir"], attempts_dir,
                               attempt_number)

    # --- final-repair visual recheck helpers (shared budget, no nesting) ---
    def _visual_context(sb):
        digest = vpipe.canonical_hash(sb)[:16]
        sb_path = runner_temp / f"visual-final-storyboard-{digest}.json"
        sb_path.write_text(json.dumps(sb, indent=2), encoding="utf-8")
        ctx = prepare_context(a.wav, work / "candidate.json", sb_path,
                                work / "brief.json")
        return ctx

    def _consume_visual_budget() -> None:
        if visual_state is None:
            return
        used = int(visual_state.get("repairs_used", 0))
        if used >= int(repair.MAX_REPAIRS):
            raise repair.RepairInfrastructureError(
                "shared visual repair budget exhausted")
        visual_state["repairs_used"] = used + 1
        # Persist BEFORE the Muse call so a crash cannot reuse it.
        vpipe.save_visual_state(visual_state_path.parent, visual_state)

    def _run_visual_recheck(new_sb):
        reviewer = vpipe.make_codex_visual_reviewer(
            base_prompt=(ROOT / ".github/studio/prompts/codex-visual-review.md").read_text(),
            schema_path=ROOT / ".github/studio/schemas/visual-review.schema.json",
            runner_temp=runner_temp / "visual-review-final",
            report_script=report_script)
        uploader = vpipe.make_node_uploader(
            node_script=ROOT / ".github/studio/scripts/upload-visual-artifact.cjs",
            result_dir=runner_temp / "visual-uploads-final")
        try:
            return vpipe.run_visual_pipeline(
                work_dir=work, context_provider=_visual_context,
                storyboard=new_sb, brief=brief, candidate=candidate,
                scenes=deterministic_scenes, parent=a.parent,
                child=a.child, engine_sha=a.engine_sha, run_id=a.run_id,
                run_attempt=a.run_attempt, gh=gh, reviewer=reviewer,
                uploader=uploader, muse_repair=None, allow_repair=False,
                state=visual_state)
        except vpipe.VisualInfrastructureError as e:
            raise repair.RepairInfrastructureError(str(e)) from e

    def _visual_blocked_outcome(visual_result, new_sb):
        if not isinstance(visual_result, dict):
            raise repair.RepairInfrastructureError(
                "visual pipeline returned no result")
        review = visual_result.get("visual_review") \
            or visual_result.get("review")
        stage = visual_result.get("visual_stage") \
            or visual_result.get("stage")
        ev_sha = visual_result.get("visual_evidence_sha256") \
            or visual_result.get("evidence_sha256") or ""
        state_after = visual_result.get("state") \
            if isinstance(visual_result.get("state"), dict) else visual_state
        artifacts_list = state_after.get("artifacts") \
            if isinstance(state_after, dict) else []
        artifact = artifacts_list[-1] if artifacts_list else None
        visual_info = {
            "status": visual_result.get("status"), "stage": stage,
            "visual_stage": stage, "evidence_sha256": ev_sha,
            "visual_evidence_sha256": ev_sha,
            "version": visual_result.get("version"),
            "approval_path": visual_result.get("approval_path"),
            "artifact": artifact, "artifacts": artifacts_list,
            "review": review}
        return {"kind": "visual_blocked", "visual_review": review,
                "review": review, "visual_stage": stage, "stage": stage,
                "visual_evidence_sha256": ev_sha,
                "evidence_sha256": ev_sha, "visual": visual_info,
                "visual_artifact": artifact, "storyboard": new_sb}

    def _attempt_runner(repair_index, context):
        prior_review = context["previous_review"]
        prior_head = context["previous_head_sha"]
        _verify_pr_and_source(gh, pr_number=pr_number,
                              expected_head=prior_head, parent=a.parent,
                              child=a.child)
        # Source/candidate drift is rechecked BEFORE the Muse call.
        _check_source_and_candidate(
            wav_path=a.wav, brief=brief, candidate=candidate)
        # Shared budget: consume+persist BEFORE this Muse repair call.
        if require_visual_preflight:
            _consume_visual_budget()
        # 5. Fresh isolated Muse workspace (allowlisted data only).
        workspace = _fresh_dir(
            runner_temp / f"muse-repair-{a.run_attempt}-{repair_index}")
        prompt = repair.build_muse_repair_prompt(
            base_prompt, brief=brief, scenes=scenes_obj,
            prior_review=prior_review,
            previous_storyboard=state["storyboard"])
        repair.prepare_muse_workspace(
            workspace, brief=brief, scenes=scenes_obj,
            storyboard_schema=schema_text, prompt=prompt,
            prior_review=prior_review,
            previous_storyboard=state["storyboard"])
        repair.run_muse_worker(
            workspace, muse_run_script=muse_run_script,
            runner_temp=runner_temp / f"muse-repair-{repair_index}",
            report_script=report_script,
            timeout=repair.AGENT_TIMEOUT_SEC)
        sb_path = workspace / "storyboard.json"
        if not sb_path.is_file() or sb_path.stat().st_size == 0:
            raise repair.RepairInfrastructureError(
                "Muse repair produced no storyboard.json")
        new_storyboard = _load_json(sb_path)
        if repair.storyboard_is_unchanged(state["storyboard"], new_storyboard):
            return {"kind": "no_change", "storyboard": new_storyboard}
        valid = repair.validate_new_storyboard(
            new_storyboard, deterministic_scenes)
        if not valid["ok"]:
            raise repair.RepairInfrastructureError(
                "repaired storyboard invalid: " + "; ".join(valid["failures"]))
        # Visual recheck of the NEW storyboard in the SAME work dir /
        # visual-state, exactly once, with no nested repair loop. Any
        # non-pass stops as visual_blocked before any full render, so
        # the last fully reviewed bundle/head is preserved by the loop.
        render_extra: list = []
        if require_visual_preflight:
            visual_result = _run_visual_recheck(new_storyboard)
            if not isinstance(visual_result, dict):
                raise repair.RepairInfrastructureError(
                    "visual pipeline returned no result")
            if not visual_result.get("proceed"):
                return _visual_blocked_outcome(
                    visual_result, new_storyboard)
            approval_path = Path(
                visual_result.get("approval_path")
                or work / vpipe.APPROVAL_FILENAME)
            if not approval_path.is_file() \
                    and not isinstance(visual_result.get("approval"), dict):
                raise repair.RepairInfrastructureError(
                    "visual approval missing after visual pass")
            if not approval_path.is_file():
                raise repair.RepairInfrastructureError(
                    "visual approval file missing after visual pass")
            # The renderer re-validates this attestation against the
            # live context before encoding (studio_render.py).
            render_extra = ["--visual-approval", str(approval_path),
                            "--engine-sha", a.engine_sha]
        staging = _fresh_dir(
            runner_temp / f"repair-bundle-{a.run_attempt}-{repair_index}")
        repair.run_agent_command(
            [sys.executable, str(ROOT / "scripts/studio_render.py"),
             "--wav", str(a.wav), "--candidate", str(work / "candidate.json"),
             "--storyboard", str(sb_path), "--brief", str(work / "brief.json"),
             "--out", str(staging)] + render_extra,
            stdout_path=runner_temp / f"render-{repair_index}.stdout",
            stderr_path=runner_temp / f"render-{repair_index}.stderr",
            timeout=repair.AGENT_TIMEOUT_SEC, label="renderer")
        qa = _load_json(staging / "qa.json")
        if not isinstance(qa, dict) or qa.get("pass") is not True:
            raise repair.RepairInfrastructureError(
                "repair render mechanical QA did not pass")
        new_plan = _load_json(staging / "plan.json")
        plan_check = repair.verify_plan_preserved(initial_plan, new_plan)
        if not plan_check["ok"]:
            raise repair.RepairInfrastructureError(
                "; ".join(plan_check["failures"]))
        staging_prov = _require_obj(
            _load_json(staging / "provenance.json"), "provenance.json")
        new_media = staging_prov.get("media_sha256") or {}
        if new_media.get("video.mp4") == (state["media"] or {}).get("video.mp4"):
            return {"kind": "no_visual_change", "storyboard": new_storyboard,
                    "media_sha256": new_media}
        # 6. Recheck guard + source after render, before commit/publish.
        _verify_pr_and_source(gh, pr_number=pr_number,
                              expected_head=prior_head, parent=a.parent,
                              child=a.child)
        _check_source_and_candidate(
            wav_path=a.wav, brief=brief, candidate=candidate)
        prod_files = {
            "brief.json": (work / "brief.json").read_bytes(),
            "storyboard.json": (staging / "storyboard.json").read_bytes(),
            "plan.json": (staging / "plan.json").read_bytes(),
            "qa.json": (staging / "qa.json").read_bytes(),
            "provenance.json": (staging / "provenance.json").read_bytes(),
            "timeline.json": (staging / "timeline.json").read_bytes(),
        }
        commit = controller.commit_bundle_controller(
            gh, child=a.child, files=prod_files,
            repo_owner=repo.split("/")[0], engine_base_sha=a.engine_sha)
        if not commit.get("ok"):
            raise repair.RepairInfrastructureError(
                "repair commit failed: " + "; ".join(commit.get("failures", [])))
        new_head = commit["head_sha"]
        receipt = artifacts.build_receipt(
            pr=pr_number, child=a.child, parent=a.parent, head_sha=new_head,
            run_id=a.run_id, run_attempt=a.run_attempt,
            engine_sha=a.engine_sha,
            source_hashes=brief.get("source_hashes"),
            planning_fingerprint=brief.get("fingerprint"),
            production_files=commit["blob_sha256"])
        reviewed_dir = _fresh_dir(
            runner_temp / f"repair-reviewed-{a.run_attempt}-{repair_index}")
        for name in ("video.mp4", "plan.json", "qa.json", "storyboard.json",
                     "brief.json", "provenance.json", "timeline.json",
                     "timeline.html", "contact-sheet.jpg"):
            shutil.copyfile(staging / name, reviewed_dir / name)
        (reviewed_dir / "receipt.json").write_text(json.dumps(receipt, indent=2))
        prelim = {p.name: p.read_bytes() for p in reviewed_dir.iterdir()
                  if p.is_file()}
        prelim_manifest = artifacts.build_hash_manifest(prelim)
        (reviewed_dir / "hashes.json").write_text(
            json.dumps(prelim_manifest, indent=2))
        prompt_text = repair.build_codex_review_prompt(
            review_prompt_base, brief=brief, storyboard=new_storyboard,
            production_files=commit, expected_head_sha=new_head,
            plan=new_plan, qa=qa, hashes=prelim_manifest,
            provenance=staging_prov, receipt=receipt)
        prompt_path = runner_temp / f"repair-review-{repair_index}-prompt.md"
        prompt_path.write_text(prompt_text, encoding="utf-8")
        review_out = reviewed_dir / "review.json"
        repair.run_codex_review(
            contact_sheet=reviewed_dir / "contact-sheet.jpg",
            schema_path=review_schema, output_path=review_out,
            prompt_path=prompt_path,
            runner_temp=runner_temp / f"codex-repair-{repair_index}",
            report_script=report_script,
            timeout=repair.AGENT_TIMEOUT_SEC)
        if not review_out.is_file() or review_out.stat().st_size == 0:
            raise repair.RepairInfrastructureError(
                "Codex re-review produced no review.json")
        new_review = _load_json(review_out)
        # Rebuild hashes over the COMPLETE bundle (now including the
        # review) and validate exactly once against that manifest.
        all_files = {p.name: p.read_bytes() for p in reviewed_dir.iterdir()
                     if p.is_file() and p.name != "hashes.json"}
        (reviewed_dir / "hashes.json").write_text(json.dumps(
            artifacts.build_hash_manifest(all_files), indent=2))
        review_check = repair.verify_bundle_files(
            repair.read_bundle_files(reviewed_dir),
            expected_head_sha=new_head, pr_number=pr_number, child=a.child,
            parent=a.parent, run_id=a.run_id, run_attempt=a.run_attempt,
            engine_sha=a.engine_sha,
            production_files=commit["blob_sha256"], require_repair=True)
        if not review_check["ok"]:
            raise repair.RepairInfrastructureError(
                "repaired bundle validation failed: "
                + "; ".join(review_check["failures"]))
        attempt_number = int(context["attempt_number"])
        state["commits"][attempt_number] = commit
        state["head"] = new_head
        state["storyboard"] = new_storyboard
        state["media"] = new_media
        state["review"] = new_review
        return {"kind": "reviewed", "review": new_review,
                "head_sha": new_head, "pr_number": pr_number,
                "storyboard": new_storyboard, "media_sha256": new_media,
                "mechanical_pass": True, "bundle_dir": str(reviewed_dir)}

    loop_result = repair.run_repair_loop(
        initial_review=initial_review,
        initial_mechanical_pass=initial_mechanical,
        initial_head_sha=initial_head, initial_pr_number=pr_number,
        attempt_runner=_attempt_runner, snapshot_attempt=_snapshot_cb,
        initial_storyboard=initial_storyboard,
        initial_media_sha256=initial_media,
        max_repairs=remaining_visual_repairs)
    return _finish(loop_result)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Production CLI for the staged visual preflight (still -> motion).

Called by the production workflow (via actions/github-script so the
trusted child receives the artifact runtime environment) BEFORE any
full render, and with ``--validate-history`` at workflow end to build
the single aggregate visual-history artifact.

Writes ``status`` / ``proceed`` / ``approval_path`` to GITHUB_OUTPUT
and prints one safe JSON line. Legitimate aesthetic outcomes
(pass/needs_engine/attempts_exhausted/no_change/no_visual_change)
exit 0 with ``proceed`` set accordingly; malformed/stale/drift/
agent/upload failures exit 2 with a fixed safe category. No raw
provider output, source audio or tokens are printed.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import os
import shutil

from studio import config, repair
from studio import visual_pipeline as vpipe
from studio.github_client import GitHubClient
from studio.render_context import prepare_context

ROOT = Path(__file__).resolve().parents[1]
_AESTHETIC = ("passed", "needs_engine", "attempts_exhausted",
              "no_change", "no_visual_change", "visual_blocked")


def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _outputs(result: dict) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"status={result.get('status') or ''}\n")
        fh.write(f"proceed={'true' if result.get('proceed') else 'false'}\n")
        fh.write(f"approval_path={result.get('approval_path') or ''}\n")


def _summary(result: dict) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(
                f"Studio visual preflight: status={result.get('status')} "
                f"proceed={bool(result.get('proceed'))} "
                f"repairs_used={result.get('repairs_used')} "
                f"owner-ready: {'pending full render/final review' if result.get('proceed') else 'NO'}\n")
    except OSError:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--muse-work", default="")
    ap.add_argument("--wav", default="")
    ap.add_argument("--parent", type=int, default=0)
    ap.add_argument("--child", type=int, default=0)
    ap.add_argument("--engine-sha", default="")
    ap.add_argument("--run-id", type=int, default=0)
    ap.add_argument("--run-attempt", type=int, default=1)
    ap.add_argument("--validate-history", action="store_true")
    ap.add_argument("--codex-led", action="store_true",
                    help="Review the saved project once; no worker or new issue")
    ap.add_argument("--candidate-context", default="",
                    help="Trusted direct-candidate context JSON; requires --codex-led")
    a = ap.parse_args()
    if a.candidate_context and not a.codex_led:
        ap.error("--candidate-context requires --codex-led")
    work = Path(a.work)
    if a.validate_history:
        try:
            out = vpipe.build_visual_history(work)
        except vpipe.VisualInfrastructureError as e:
            print(json.dumps({"ok": False, "status": "infrastructure_failure",
                              "failures": [str(e)]}))
            return 2
        print(json.dumps({"ok": True, "status": "history_valid",
                          "history": str(out)}))
        return 0
    try:
        return _run(a, work)
    except vpipe.VisualInfrastructureError as e:
        result = {"status": "infrastructure_failure", "proceed": False,
                  "approval_path": "", "repairs_used": 0}
        print(json.dumps({"ok": False, "status": "infrastructure_failure",
                          "failures": [str(e)]}))
        _outputs(result)
        _summary(result)
        return 2
    except repair.RepairInfrastructureError as e:
        print(json.dumps({"ok": False, "status": "infrastructure_failure",
                          "failures": [str(e)]}))
        _outputs({"status": "infrastructure_failure", "proceed": False,
                  "approval_path": "", "repairs_used": 0})
        return 2


def _candidate_guard(a, work, gh, *, brief, candidate, scenes, storyboard):
    """Bind local work to the same raw inputs rechecked through the live API."""
    from studio import artifacts
    from studio.candidate_pipeline import verify_candidate_guard
    from studio.candidate_project import INPUT_NAMES

    if not getattr(a, "codex_led", False):
        raise vpipe.VisualInfrastructureError("candidate context requires Codex-led mode")
    try:
        path = Path(a.candidate_context)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > config.MAX_PRODUCTION_FILE_BYTES:
            raise ValueError("invalid context file")
        context = _load(path)
        if (context["source_issue"] != a.parent or context["pr"] != a.child
                or context["engine_sha"] != a.engine_sha):
            raise ValueError("candidate context identity mismatch")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise vpipe.VisualInfrastructureError("visual candidate context is invalid") from exc

    def guard():
        try:
            raw = {}
            for name in INPUT_NAMES:
                path = work / name
                if (path.is_symlink() or not path.is_file()
                        or path.stat().st_size > config.MAX_PRODUCTION_FILE_BYTES):
                    raise ValueError("invalid local candidate input")
                raw[name] = path.read_bytes()
            if artifacts.build_hash_manifest(raw) != context["input_sha256"]:
                raise ValueError("local candidate inputs changed")
            original = json.loads(raw["brief.json"])
            if (brief != original or candidate != original["candidate"]
                    or scenes != original["candidate"]["scenes"]
                    or storyboard != json.loads(raw["storyboard.json"])):
                raise ValueError("local candidate objects changed")
            verify_candidate_guard(gh, context)
        except Exception as exc:
            raise vpipe.VisualInfrastructureError(
                "visual candidate guard rejected current inputs") from exc

    guard()  # Reject drift before constructing model/upload adapters.
    return guard


def _run(a, work: Path) -> int:
    brief = _load(work / "brief.json")
    candidate = _load(work / "candidate.json")
    scenes_obj = _load(work / "scenes.json")
    storyboard = _load(work / "storyboard.json")
    scenes = scenes_obj.get("scenes", []) if isinstance(scenes_obj, dict) else scenes_obj
    repo = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not repo or not token:
        raise vpipe.VisualInfrastructureError(
            "GitHub repository/token unavailable for visual preflight")
    gh = GitHubClient(repo, token)
    candidate_guard = None
    if getattr(a, "candidate_context", ""):
        candidate_guard = _candidate_guard(
            a, work, gh, brief=brief, candidate=candidate, scenes=scenes,
            storyboard=storyboard)
    runner_temp = Path(os.environ.get("RUNNER_TEMP") or work.parent)
    runner_temp.mkdir(parents=True, exist_ok=True)
    sb_dir = runner_temp / "visual-storyboards"
    sb_dir.mkdir(parents=True, exist_ok=True)

    def _context(sb):
        import hashlib as _hl
        digest = _hl.sha256(json.dumps(sb, sort_keys=True).encode()).hexdigest()[:16]
        sb_path = sb_dir / f"storyboard-{digest}.json"
        sb_path.write_text(json.dumps(sb, indent=2), encoding="utf-8")
        return prepare_context(a.wav, work / "candidate.json", sb_path,
                               work / "brief.json")

    reviewer = vpipe.make_codex_visual_reviewer(
        base_prompt=(ROOT / ".github/studio/prompts/codex-visual-review.md").read_text(),
        schema_path=ROOT / ".github/studio/schemas/visual-review.schema.json",
        runner_temp=runner_temp / "visual-review",
        report_script=ROOT / ".github/studio/scripts/report-agent-error.py")
    uploader = vpipe.make_node_uploader(
        node_script=ROOT / ".github/studio/scripts/upload-visual-artifact.cjs",
        result_dir=runner_temp / "visual-uploads")
    muse_work_root = Path(a.muse_work) if a.muse_work else runner_temp / "muse-work"
    codex_led = getattr(a, "codex_led", False)
    base_prompt = "" if codex_led else (ROOT / ".github/studio/prompts/muse-storyboard.md").read_text()
    schema_text = "" if codex_led else (ROOT / ".github/studio/schemas/storyboard.schema.json").read_text()

    def _muse_repair(*, review, storyboard, brief, scenes):
        prior = state_holder.get("version", 0)
        workspace_parent = runner_temp / f"muse-visual-repair-{prior}"
        if workspace_parent.exists():
            shutil.rmtree(workspace_parent)
        workspace_parent.mkdir(parents=True)
        prompt = repair.build_muse_repair_prompt(
            base_prompt, brief=brief,
            scenes={"scenes": scenes} if not isinstance(scenes, dict) else scenes,
            prior_review=review, previous_storyboard=storyboard)
        ws = workspace_parent / "work"
        repair.prepare_muse_workspace(
            ws, brief=brief,
            scenes={"scenes": scenes} if not isinstance(scenes, dict) else scenes,
            storyboard_schema=schema_text, prompt=prompt,
            prior_review=review, previous_storyboard=storyboard)
        repair.run_muse_worker(
            ws, muse_run_script=ROOT / ".github/studio/scripts/muse-run.sh",
            runner_temp=runner_temp / f"muse-visual-{prior}",
            report_script=ROOT / ".github/studio/scripts/report-agent-error.py",
            timeout=repair.AGENT_TIMEOUT_SEC)
        sb_path = ws / "storyboard.json"
        if not sb_path.is_file() or sb_path.stat().st_size == 0:
            raise vpipe.VisualInfrastructureError(
                "Muse visual repair produced no storyboard")
        return _load(sb_path)

    state_holder: dict = {}
    real_muse = _muse_repair

    def _tracked_muse(*, review, storyboard, brief, scenes):
        try:
            st = vpipe.load_visual_state(work)
            state_holder["version"] = int(st.get("current_version", 0))
        except Exception:  # noqa: BLE001 - budget already persisted by pipeline
            state_holder["version"] = state_holder.get("version", 0)
        return real_muse(review=review, storyboard=storyboard,
                         brief=brief, scenes=scenes)

    result = vpipe.run_visual_pipeline(
        work_dir=work, context_provider=_context, storyboard=storyboard,
        brief=brief, candidate=candidate, scenes=scenes,
        parent=a.parent, child=a.child, engine_sha=a.engine_sha,
        run_id=a.run_id, run_attempt=a.run_attempt, gh=gh,
        reviewer=reviewer, uploader=uploader,
        muse_repair=None if codex_led else _tracked_muse,
        allow_repair=not codex_led, create_engineering_issue=not codex_led,
        candidate_guard=candidate_guard)
    # Only a both-stage approval may replace the storyboard the full
    # renderer will read; a blocked/repaired-but-unapproved result must
    # never leave a stale-or-new unreviewed storyboard in place.
    if result.get("proceed"):
        approved_sb = result.get("storyboard")
        if isinstance(approved_sb, dict):
            (work / "storyboard.json").write_text(
                json.dumps(approved_sb, indent=2), encoding="utf-8")
    out = {"ok": bool(result.get("proceed")),
           "status": result.get("status"),
           "proceed": bool(result.get("proceed")),
           "approval_path": result.get("approval_path") or "",
           "repairs_used": result.get("repairs_used"),
           "attempt_count": result.get("version")}
    print(json.dumps({"ok": out["ok"], "status": out["status"],
                      "proceed": out["proceed"],
                      "approval_path": out["approval_path"],
                      "repairs_used": out["repairs_used"]}))
    _outputs(out)
    _summary(out)
    return 0 if result.get("status") in _AESTHETIC else 2


if __name__ == "__main__":
    raise SystemExit(main())

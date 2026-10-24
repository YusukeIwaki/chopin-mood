"""Staged visual-review orchestration for Studio (still -> motion).

Testable controller between Muse storyboards and the full
renderer. Reuses :mod:`studio.visual_preflight` evidence/review/
approval core and the shared render context: pixels and timing come
from the deterministic renderer. Per storyboard version: native
still evidence -> immutable preview upload -> pending comment ->
Codex visual review (strict JSON) -> result comment -> on pass
short silent motion evidence -> upload -> review. Both approvals
are required before any full render. Stages short-circuit on fail.

One SHARED budget of ``config.MAX_REPAIRS`` (2) Muse repair calls
covers this preflight and the later full-video repair loop,
persisted in ``visual-state.json`` with exact brief/candidate/
scenes and per-version storyboard snapshots validated against the
evidence binding canonical hashes. Engine failures stop with
``needs_engine`` and no Muse call; no-change / visually identical
repairs and exhaustion also stop. A version from the final-video
repair loop is checked exactly once (``allow_repair=False``) with
no nested repair loop. Guards are freshly re-checked before every
model call, upload, comment and before finishing. Models receive
only bounded brief/storyboard/evidence JSON plus evidence images:
never source audio, never runtime/GitHub tokens.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import config, fingerprint
from . import visual_preflight as vp

__all__ = [
    "VisualInfrastructureError", "STATE_FILENAME", "APPROVAL_FILENAME",
    "canonical_hash", "artifact_name_for", "validate_upload_dir",
    "create_visual_state", "save_visual_state", "load_visual_state",
    "validate_visual_state", "verify_visual_guard",
    "run_visual_pipeline", "validate_visual_history",
    "build_visual_history", "build_visual_review_prompt",
    "stripped_model_env", "make_node_uploader",
    "make_codex_visual_reviewer",
]

STATE_FILENAME = "visual-state.json"
APPROVAL_FILENAME = "visual-approval.json"
STATE_VERSION = "1.0"
MAX_VERSIONS = config.VISUAL_MAX_VERSIONS
MAX_PREVIEW_ARTIFACTS = config.VISUAL_MAX_PREVIEW_ARTIFACTS
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")
_STATE_KEYS = {
    "schema_version", "parent_issue", "child_issue", "candidate_id",
    "engine_sha", "run_id", "run_attempt", "source_sha256",
    "brief_sha256", "candidate_sha256", "scenes_sha256",
    "brief", "candidate", "scenes", "max_repairs", "repairs_used",
    "current_version", "versions", "artifacts",
}


class VisualInfrastructureError(RuntimeError):
    """Malformed/stale/drift/agent/upload failure: fail closed."""


def canonical_hash(obj) -> str:
    """Canonical-JSON SHA-256 (same bytes as the evidence binding)."""
    return hashlib.sha256(
        fingerprint.canonical_json(obj).encode("utf-8")).hexdigest()


def artifact_name_for(*, child: int, run_id: int, run_attempt: int,
                      version: int, stage: str) -> str:
    """Immutable preview artifact name (never release-eligible)."""
    return (f"studio-visual-child-{int(child)}-run-{int(run_id)}"
            f"-a{int(run_attempt)}-v{int(version)}-{stage}")


def stripped_model_env(env=None) -> dict:
    """Model child environment without runtime/GitHub tokens."""
    out = dict(os.environ if env is None else env)
    for key in config.MODEL_ENV_STRIP_KEYS:
        out.pop(key, None)
    return out


# --- persisted state -------------------------------------------------------

def create_visual_state(*, parent: int, child: int, brief: dict,
                        candidate: dict, scenes, engine_sha: str,
                        run_id: int, run_attempt: int,
                        source_sha256: str = "") -> dict:
    cand = candidate if isinstance(candidate, dict) else {}
    scene_list = scenes.get("scenes") if isinstance(scenes, dict) else scenes
    return {
        "schema_version": STATE_VERSION,
        "parent_issue": int(parent), "child_issue": int(child),
        "candidate_id": cand.get("id") or brief.get("candidate_id"),
        "engine_sha": engine_sha, "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "source_sha256": source_sha256 or "",
        "brief_sha256": canonical_hash(brief),
        "candidate_sha256": canonical_hash(cand),
        "scenes_sha256": canonical_hash(scene_list),
        "brief": brief, "candidate": cand, "scenes": scene_list,
        "max_repairs": int(config.MAX_REPAIRS), "repairs_used": 0,
        "current_version": 0, "versions": [], "artifacts": [],
    }


def save_visual_state(work_dir, state: dict) -> Path:
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    path = work / STATE_FILENAME
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def load_visual_state(work_dir) -> dict:
    path = Path(work_dir) / STATE_FILENAME
    if path.is_symlink() or not path.is_file():
        raise VisualInfrastructureError("visual state missing")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - malformed state fails closed
        raise VisualInfrastructureError(
            "visual state is not valid JSON") from e
    result = validate_visual_state(state)
    if not result["ok"]:
        raise VisualInfrastructureError(
            "visual state invalid: " + "; ".join(result["failures"]))
    return state


def validate_visual_state(state) -> dict:
    """Structural + snapshot-hash validation of visual-state.json."""
    failures: list[str] = []
    if not isinstance(state, dict):
        return {"ok": False, "failures": ["visual state must be an object"]}
    if set(state) != _STATE_KEYS:
        failures.append(
            f"visual state keys {sorted(state)} != {sorted(_STATE_KEYS)}")
    if state.get("schema_version") != STATE_VERSION:
        failures.append("visual state schema_version must be '1.0'")
    for key in ("parent_issue", "child_issue", "run_id", "run_attempt"):
        if type(state.get(key)) is not int or state[key] < 1:
            failures.append(f"visual state {key} must be a positive integer")
    if not re.fullmatch(r"[0-9a-f]{40}", str(state.get("engine_sha", ""))):
        failures.append("visual state engine_sha invalid")
    if not _SHA64_RE.fullmatch(str(state.get("source_sha256", ""))):
        failures.append("visual state source_sha256 invalid")
    if state.get("max_repairs") != config.MAX_REPAIRS:
        failures.append("visual state shared repair limit mismatch")
    brief, cand = state.get("brief"), state.get("candidate")
    scenes = state.get("scenes")
    if not isinstance(brief, dict):
        failures.append("visual state brief must be an object")
    if not isinstance(cand, dict):
        failures.append("visual state candidate must be an object")
    if not isinstance(scenes, list):
        failures.append("visual state scenes must be a list")
    if isinstance(brief, dict) \
            and state.get("brief_sha256") != canonical_hash(brief):
        failures.append("visual state brief snapshot hash mismatch")
    if isinstance(cand, dict) \
            and state.get("candidate_sha256") != canonical_hash(cand):
        failures.append("visual state candidate snapshot hash mismatch")
    if isinstance(scenes, list) \
            and state.get("scenes_sha256") != canonical_hash(scenes):
        failures.append("visual state scenes snapshot hash mismatch")
    used = state.get("repairs_used")
    if not isinstance(used, int) or isinstance(used, bool) \
            or not (0 <= used <= int(config.MAX_REPAIRS)):
        failures.append("visual state repairs_used out of shared budget")
    versions = state.get("versions")
    if not isinstance(versions, list):
        failures.append("visual state versions must be a list")
        versions = []
    if len(versions) > MAX_VERSIONS:
        failures.append("visual state exceeds max storyboard versions")
    if type(state.get("current_version")) is not int \
            or state.get("current_version") != len(versions):
        failures.append("visual state current_version must match contiguous versions")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, list):
        failures.append("visual state artifacts must be a list")
        artifacts = []
    if len(artifacts) > MAX_PREVIEW_ARTIFACTS:
        failures.append("visual state exceeds max preview artifacts")
    for i, ver in enumerate(versions):
        if not isinstance(ver, dict):
            failures.append(f"visual version {i + 1} must be an object")
            continue
        if type(ver.get("version")) is not int or ver["version"] != i + 1:
            failures.append("visual versions must be contiguous positive integers")
        sb = ver.get("storyboard")
        if not isinstance(sb, dict):
            failures.append(f"visual version {i + 1} storyboard missing")
        elif ver.get("storyboard_sha256") != canonical_hash(sb):
            failures.append(
                f"visual version {i + 1} storyboard snapshot hash mismatch")
    return {"ok": not failures, "failures": failures}


# --- fresh guard -----------------------------------------------------------

def verify_visual_guard(gh, *, parent: int, child: int, brief: dict,
                        candidate: dict, scenes, state: dict | None = None,
                        source_sha256: str | None = None) -> None:
    """Fresh parent/child/binding guard; raises on any drift.

    Parent must be open and OWNER-authored; child must be open and
    bot-authored, carry the brief's exact child marker, and be an
    actually linked sub-issue when the transport can list them.
    Brief candidate, source hash and immutable scenes must match the
    persisted state snapshots. Called before every model call,
    upload, comment and before finishing.
    """
    if gh is None:
        # Offline callers still get the local binding checks below.
        pass
    else:
        try:
            parent_issue = gh.get_issue(int(parent))
            child_issue = gh.get_issue(int(child))
        except Exception as e:  # noqa: BLE001 - unreadable guard fails closed
            raise VisualInfrastructureError(
                "visual guard: parent/child unreadable") from e
        if parent_issue.get("state") != "open":
            raise VisualInfrastructureError("visual guard: parent not open")
        if (parent_issue.get("user") or {}).get("login") != config.OWNER:
            raise VisualInfrastructureError(
                "visual guard: parent not owner-authored")
        if child_issue.get("state") != "open":
            raise VisualInfrastructureError("visual guard: child not open")
        if (child_issue.get("user") or {}).get("login") \
                not in config.BOT_LOGINS:
            raise VisualInfrastructureError(
                "visual guard: child not bot-authored")
        markers = fingerprint.parse_child_markers(child_issue.get("body") or "")
        cand_id = (candidate or {}).get("id") or brief.get("candidate_id")
        fp = brief.get("fingerprint")
        if not any(m["parent"] == int(parent)
                   and m["candidate_id"] == cand_id
                   and m["fingerprint"] == fp for m in markers):
            raise VisualInfrastructureError(
                "visual guard: child marker does not match brief")
        if hasattr(gh, "get_sub_issue_numbers"):
            try:
                linked = gh.get_sub_issue_numbers(int(parent))
            except Exception as e:  # noqa: BLE001
                raise VisualInfrastructureError(
                    "visual guard: sub-issues unreadable") from e
            if int(child) not in [int(n) for n in linked]:
                raise VisualInfrastructureError(
                    "visual guard: child is not a linked sub-issue")
    if brief.get("candidate") != candidate:
        raise VisualInfrastructureError(
            "visual guard: candidate does not match brief.candidate")
    if brief.get("parent_issue") not in (None, int(parent)) \
            or brief.get("child_issue") not in (None, int(child)):
        raise VisualInfrastructureError(
            "visual guard: brief issue binding mismatch")
    scene_list = scenes.get("scenes") if isinstance(scenes, dict) else scenes
    if state is not None:
        if canonical_hash(brief) != state.get("brief_sha256"):
            raise VisualInfrastructureError("visual guard: brief drift")
        if canonical_hash(candidate) != state.get("candidate_sha256"):
            raise VisualInfrastructureError("visual guard: candidate drift")
        if canonical_hash(scene_list) != state.get("scenes_sha256"):
            raise VisualInfrastructureError(
                "visual guard: immutable scenes drift")
        if state.get("parent_issue") != int(parent) \
                or state.get("child_issue") != int(child):
            raise VisualInfrastructureError("visual guard: state issue drift")
        if source_sha256 is not None and state.get("source_sha256") \
                and source_sha256 != state.get("source_sha256"):
            raise VisualInfrastructureError("visual guard: source drift")
    if source_sha256 is not None:
        want = ((brief.get("source_hashes") or {}).get("decoded_wav_sha256")
                if isinstance(brief.get("source_hashes"), dict) else None)
        if want is not None and want != source_sha256:
            raise VisualInfrastructureError(
                "visual guard: source hash does not match brief")


# --- upload allowlist (checked in Python BEFORE invoking Node) -------------

_UPLOAD_FIXED = {"evidence.json", "contact-sheet.jpg"}
_FRAME_RE = re.compile(r"^frame-\d{6,}\.png$")


def validate_upload_dir(evidence_dir, *, stage: str) -> dict:
    """Pre-upload allowlist: evidence + sheet + native frames only.

    ``review.json`` must NOT be present before the review; logs, auth
    files, symlinks and extra files all fail closed. The evidence
    itself is fully re-validated via the core validator.
    """
    failures: list[str] = []
    base = Path(evidence_dir)
    if base.is_symlink() or not base.is_dir():
        return {"ok": False, "failures": ["evidence directory missing"]}
    names = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        names.append(entry.name)
        if entry.is_symlink():
            failures.append(f"symlink in upload dir: {entry.name}")
        elif entry.is_dir():
            failures.append(f"directory in upload dir: {entry.name}")
        elif entry.name == "review.json":
            failures.append("review.json must not be uploaded before review")
        elif entry.name in _UPLOAD_FIXED or _FRAME_RE.match(entry.name) \
                or (stage == "motion" and entry.name == "preview.mp4"):
            pass
        else:
            failures.append(f"file not allowlisted for upload: {entry.name}")
    if stage == "still" and "preview.mp4" in names:
        failures.append("still upload must not contain preview.mp4")
    if stage == "motion" and "preview.mp4" not in names:
        failures.append("motion upload missing preview.mp4")
    core = vp.validate_evidence(base)
    if not core["ok"]:
        failures.extend(core["failures"])
    elif (core.get("evidence") or {}).get("stage") != stage:
        failures.append("evidence stage does not match upload stage")
    return {"ok": not failures, "failures": failures,
            "evidence": core.get("evidence"),
            "evidence_sha256": core.get("evidence_sha256")}


# --- model prompt / production adapters ------------------------------------

def build_visual_review_prompt(base_prompt: str, *, brief, storyboard,
                               evidence, evidence_dir, stage: str) -> str:
    """Bounded Codex prompt with EXACT evidence paths for inspection.

    Gives the evidence directory and every native PNG path so the
    reviewer can open full-resolution frames via image viewing, not
    only the small contact-sheet thumbnails. Contains no source
    audio, no tokens and no absolute host paths beyond the evidence
    directory the reviewer must inspect.
    """
    evidence = evidence if isinstance(evidence, dict) else {}
    base_dir = Path(evidence_dir)
    digest = hashlib.sha256((base_dir / "evidence.json").read_bytes()).hexdigest()
    native = [str(base_dir / s.get("file", ""))
              for s in evidence.get("samples", []) if isinstance(s, dict)]
    lines = [
        base_prompt.rstrip(), "",
        f"Evidence stage: {stage}",
        f"Evidence SHA-256: {digest}",
        f"Evidence directory: {base_dir}",
        f"Evidence file: {base_dir / 'evidence.json'}",
        f"Contact sheet: {base_dir / 'contact-sheet.jpg'}",
        "Native frames (open these exact PNG paths at full resolution):",
        *[f"- {p}" for p in native],
        "",
        "## brief.json (bounded data)",
        json.dumps(brief, indent=2)[:20000], "",
        "## storyboard.json (bounded data)",
        json.dumps(storyboard, indent=2)[:20000], "",
        "## evidence.json (bounded data)",
        json.dumps(evidence, indent=2)[:20000], "",
        "Bind stage and the exact Evidence SHA-256 supplied above in the "
        "review. Return strict JSON matching the visual-review schema "
        "only.",
    ]
    if stage == "motion":
        lines.insert(7, f"Silent preview: {base_dir / 'preview.mp4'} "
                        "(sampled decoded frames only; no audio)")
    return "\n".join(lines) + "\n"


def make_node_uploader(*, node_script, result_dir, env=None):
    """Trusted uploader callable backed by upload-visual-artifact.cjs.

    Validates the allowlist/evidence in Python BEFORE invoking Node.
    The child keeps the trusted environment (runtime tokens reach the
    artifact SDK); SDK stdout/stderr are captured privately and any
    failure is safe-classified without raw provider text.
    """
    script = Path(node_script)
    out_dir = Path(result_dir)

    def _upload(evidence_dir, artifact_name: str) -> dict:
        stage = "motion" if artifact_name.endswith("-motion") else "still"
        check = validate_upload_dir(evidence_dir, stage=stage)
        if not check["ok"]:
            raise VisualInfrastructureError(
                "visual upload validation failed: "
                + "; ".join(check["failures"]))
        out_dir.mkdir(parents=True, exist_ok=True)
        result_path = out_dir / f"upload-{artifact_name}.json"
        cmd = ["node", str(script), "--dir", str(Path(evidence_dir)),
               "--name", artifact_name, "--result", str(result_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=300, env=dict(os.environ if env is None else env))
        meta = None
        try:
            meta = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - missing meta is an upload failure
            meta = None
        if proc.returncode != 0 or not isinstance(meta, dict) \
                or meta.get("ok") is not True:
            raise VisualInfrastructureError(
                "visual artifact upload failed (classification: upload_failed)")
        artifact_id = meta.get("id")
        url = meta.get("url") or ""
        if not url and artifact_id is not None:
            repo = os.environ.get("GITHUB_REPOSITORY", "")
            run_id = os.environ.get("GITHUB_RUN_ID", "")
            if repo and run_id:
                url = (f"https://github.com/{repo}/actions/runs/{run_id}"
                       f"/artifacts/{artifact_id}")
        return {"name": artifact_name, "id": artifact_id, "url": url,
                "size": meta.get("size")}

    return _upload


def make_codex_visual_reviewer(*, base_prompt: str, schema_path,
                               runner_temp, report_script=None,
                               timeout: int = config.VISUAL_AGENT_TIMEOUT_SEC):
    """Production Codex reviewer: strict JSON out, stripped model env."""
    from . import repair as repairmod

    rt = Path(runner_temp)
    schema = Path(schema_path)

    def _review(*, evidence_dir, stage, brief, storyboard, evidence):
        prompt = build_visual_review_prompt(
            base_prompt, brief=brief, storyboard=storyboard,
            evidence=evidence, evidence_dir=evidence_dir, stage=stage)
        rt.mkdir(parents=True, exist_ok=True)
        prompt_path = rt / f"visual-{stage}-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        out_path = rt / f"visual-{stage}-review.json"
        contact = Path(evidence_dir) / "contact-sheet.jpg"
        images = [str(contact)] + [
            str(Path(evidence_dir) / s["file"])
            for s in evidence.get("samples", []) if isinstance(s, dict)]
        cmd = ["codex", "--ask-for-approval", "never", "exec",
               "--strict-config", "--ephemeral", "--model",
               config.CODEX_MODEL, "--config",
               f'model_reasoning_effort="{config.CODEX_EFFORT}"',
               "--config", 'default_permissions="studio"']
        for img in images:
            cmd += ["--image", img]
        cmd += ["--output-schema", str(schema),
                "--output-last-message", str(out_path), "-"]
        model_env = stripped_model_env()
        runtime_home = model_env.get("CODEX_RUNTIME_HOME")
        if runtime_home:
            model_env["CODEX_HOME"] = runtime_home
        repairmod.run_agent_command(
            cmd, stdout_path=rt / f"codex-visual-{stage}.stdout",
            stderr_path=rt / f"codex-visual-{stage}.stderr",
            stdin_path=prompt_path, timeout=timeout,
            env=model_env, report_script=report_script,
            label="Codex visual review")
        if not out_path.is_file() or out_path.stat().st_size == 0:
            raise VisualInfrastructureError(
                "Codex visual review produced no review (classification: generic)")
        try:
            return json.loads(out_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            raise VisualInfrastructureError(
                "Codex visual review is not valid JSON") from e

    return _review


# --- orchestration ---------------------------------------------------------

def _rel(work: Path, path: Path) -> str:
    try:
        return str(Path(path).relative_to(work))
    except ValueError:
        return str(path)


def _result(*, status, proceed, state, version, storyboard, stage=None,
            review=None, evidence_sha256="", approval_path=None,
            approval=None, work_dir=None):
    return {
        "ok": status == "passed", "status": status,
        "proceed": bool(proceed), "stage": stage,
        "visual_stage": stage, "version": int(version),
        "storyboard": storyboard, "review": review,
        "visual_review": review, "evidence_sha256": evidence_sha256,
        "visual_evidence_sha256": evidence_sha256,
        "approval_path": str(approval_path) if approval_path else None,
        "approval": approval, "repairs_used": int(state.get("repairs_used", 0)),
        "state": state, "work_dir": str(work_dir) if work_dir else None,
        "next_action": (
            "Proceed to the full render with the visual approval; "
            "owner-ready still requires the existing full mechanical "
            "and aesthetic pass." if proceed else
            "Do not run the full render; address the visual outcome "
            "above (engineering handoff where routed)."),
    }


def run_visual_pipeline(*, work_dir, context_provider, storyboard, brief,
                        candidate, scenes, parent: int, child: int,
                        engine_sha: str, run_id: int, run_attempt: int,
                        gh=None, reviewer, uploader, muse_repair=None,
                        allow_repair: bool = True, state: dict | None = None,
                        ) -> dict:
    """Run still -> motion preflight for one candidate.

    ``context_provider(storyboard)`` returns a render context;
    ``reviewer(evidence_dir=, stage=, brief=, storyboard=, evidence=)``
    returns the strict visual-review JSON; ``uploader(evidence_dir,
    artifact_name)`` returns ``{id, url, ...}``; ``muse_repair(
    review=, storyboard=, brief=, scenes=)`` returns a new storyboard.
    Legitimate aesthetic outcomes return a result dict; malformed,
    stale, tampered, drift or agent/upload failures raise
    :class:`VisualInfrastructureError` (fail closed).
    """
    from . import controller as ctrl
    from . import storyboard as sbmod

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    visual_root = work / "visual"
    scene_list = scenes.get("scenes") if isinstance(scenes, dict) else list(scenes)

    def _context(sb):
        try:
            return context_provider(sb)
        except VisualInfrastructureError:
            raise
        except Exception as e:  # noqa: BLE001 - context failure is infra
            raise VisualInfrastructureError(
                f"context preparation failed: {type(e).__name__}") from e

    def _guard(ctx=None):
        verify_visual_guard(
            gh, parent=parent, child=child, brief=brief, candidate=candidate,
            scenes=scene_list, state=state,
            source_sha256=getattr(ctx, "source_sha256", None)
            if ctx is not None else (state or {}).get("source_sha256") or None)

    first_ctx = _context(storyboard)
    if state is None:
        state_path = work / STATE_FILENAME
        if state_path.is_file() and not state_path.is_symlink():
            state = load_visual_state(work)
        else:
            state = create_visual_state(
                parent=parent, child=child, brief=brief,
                candidate=candidate, scenes=scene_list,
                engine_sha=engine_sha, run_id=run_id,
                run_attempt=run_attempt,
                source_sha256=first_ctx.source_sha256)
            save_visual_state(work, state)
    # Identity binding of a reused state must match this run exactly.
    for key, want in (("parent_issue", int(parent)),
                      ("child_issue", int(child)),
                      ("engine_sha", engine_sha), ("run_id", int(run_id)),
                      ("run_attempt", int(run_attempt))):
        if state.get(key) != want:
            raise VisualInfrastructureError(
                f"visual state {key} does not match this run")
    if not state.get("source_sha256"):
        state["source_sha256"] = first_ctx.source_sha256
    checked_state = validate_visual_state(state)
    if not checked_state["ok"]:
        raise VisualInfrastructureError("visual state invalid: " + "; ".join(checked_state["failures"]))
    _guard(first_ctx)

    def _handoff(*, stage, review, evidence_sha256, artifact, status):
        if gh is None:
            return None
        _guard()
        return ctrl.ensure_visual_engineering_issue(
            gh, parent=parent, child=child, run_id=run_id,
            run_attempt=run_attempt, stage=stage,
            evidence_sha256=evidence_sha256,
            artifact_url=(artifact or {}).get("url", ""),
            artifact_name=(artifact or {}).get("name", ""),
            review=review, status=status)

    def _comment(*, version, stage, phase, body):
        if gh is None:
            return None
        _guard()
        return ctrl.ensure_visual_comment(
            gh, child=child, run_id=run_id, run_attempt=run_attempt,
            version=version, stage=stage, phase=phase, body=body)

    # No duplicate preflight for an already approved storyboard.
    sb_hash = canonical_hash(storyboard)
    for ver in state.get("versions", []):
        if ver.get("storyboard_sha256") == sb_hash \
                and ver.get("outcome") == "passed":
            still_dir = work / ver["still"]["dir"]
            motion_dir = work / ver["motion"]["dir"]
            approval = vp.approved_binding(
                still_dir, motion_dir, first_ctx,
                expected_engine_sha=engine_sha, expected_run_id=run_id,
                expected_run_attempt=run_attempt)
            apath = _write_approval(work, still_dir, motion_dir,
                                    engine_sha=engine_sha, run_id=run_id,
                                    run_attempt=run_attempt)
            _guard(first_ctx)
            return _result(status="passed", proceed=True, state=state,
                           version=int(ver["version"]), storyboard=storyboard,
                           approval_path=apath, approval=approval,
                           work_dir=work)

    current_sb = storyboard
    previous_still_media = None
    last_review, last_stage, last_sha, last_artifact = None, None, "", None
    for prior in state.get("versions", []):
        for stage in ("still", "motion"):
            rec = prior.get(stage) or {}
            if rec.get("review"):
                last_review, last_stage = rec["review"], stage
                last_sha, last_artifact = rec["evidence_sha256"], rec.get("artifact")
        prior_still = (prior.get("still") or {}).get("dir")
        if prior_still and (work / prior_still / "evidence.json").is_file():
            try:
                previous_still_media = json.loads(
                    (work / prior_still / "evidence.json")
                    .read_text(encoding="utf-8")).get("media_sha256")
            except Exception:  # noqa: BLE001 - artefact rechecked later
                previous_still_media = None

    while True:
        version = int(state.get("current_version", 0)) + 1
        if version > MAX_VERSIONS:
            raise VisualInfrastructureError(
                "visual storyboard version bound exceeded")
        if any(v.get("version") == version for v in state["versions"]):
            version = max(int(v["version"]) for v in state["versions"]) + 1
        state["current_version"] = version
        ver_rec: dict = {
            "version": version, "storyboard": current_sb,
            "storyboard_sha256": canonical_hash(current_sb),
            "outcome": "pending", "still": None, "motion": None}
        state["versions"].append(ver_rec)
        save_visual_state(work, state)
        ctx = first_ctx if version == int(state["versions"][0]["version"]) \
            and current_sb is storyboard else _context(current_sb)
        _guard(ctx)
        blocked = False
        for stage in ("still", "motion"):
            ev_dir = visual_root / f"v{version}" / stage
            try:
                evidence = vp.generate_evidence(
                    ctx, ev_dir, stage=stage, engine_sha=engine_sha,
                    run_id=run_id, run_attempt=run_attempt,
                    iteration=version - 1)
            except Exception as e:  # noqa: BLE001 - evidence failure is infra
                raise VisualInfrastructureError(
                    f"evidence generation failed at {stage}: "
                    f"{type(e).__name__}") from e
            checked = vp.validate_evidence(
                ev_dir, ctx, expected_engine_sha=engine_sha,
                expected_run_id=run_id, expected_run_attempt=run_attempt)
            if not checked["ok"]:
                raise VisualInfrastructureError(
                    f"evidence validation failed at {stage}: "
                    + "; ".join(checked["failures"]))
            ev_sha = checked["evidence_sha256"]
            # Snapshot must explain the actual evidence binding.
            binding = (checked.get("evidence") or {}).get("binding") or {}
            if binding.get("storyboard_sha256") != ver_rec["storyboard_sha256"] \
                    or binding.get("brief_sha256") != state["brief_sha256"] \
                    or binding.get("candidate_sha256") != state["candidate_sha256"]:
                raise VisualInfrastructureError(
                    "evidence binding does not match persisted snapshots")
            if stage == "still" and previous_still_media is not None \
                    and evidence.get("media_sha256") == previous_still_media:
                # Keep the new attempt and its actual inputs in history,
                # without inventing a new AI verdict for identical pixels.
                ver_rec[stage] = {
                    "dir": _rel(work, ev_dir), "evidence_sha256": ev_sha,
                    "artifact": None, "review": None, "verdict": None,
                    "review_status": "not_reviewed_no_visual_change"}
                ver_rec["outcome"] = "no_visual_change"
                save_visual_state(work, state)
                _handoff(stage=last_stage, review=last_review,
                         evidence_sha256=last_sha, artifact=last_artifact,
                         status="no_visual_change")
                return _result(status="no_visual_change", proceed=False,
                               state=state, version=version,
                               storyboard=current_sb, stage=last_stage,
                               review=last_review,
                               evidence_sha256=last_sha, work_dir=work)
            if len(state["artifacts"]) >= MAX_PREVIEW_ARTIFACTS:
                raise VisualInfrastructureError("preview artifact bound exceeded")
            name = artifact_name_for(child=child, run_id=run_id,
                                     run_attempt=run_attempt,
                                     version=version, stage=stage)
            _guard(ctx)  # before upload
            try:
                artifact = uploader(ev_dir, name)
            except VisualInfrastructureError:
                raise
            except Exception as e:  # noqa: BLE001 - upload stops the stage
                raise VisualInfrastructureError(
                    "visual artifact upload failed "
                    "(classification: upload_failed)") from e
            if not isinstance(artifact, dict):
                raise VisualInfrastructureError("uploader returned no artifact")
            artifact = {"name": name, "id": artifact.get("id"),
                        "url": artifact.get("url", "")}
            state["artifacts"].append(
                {"name": name, "version": version, "stage": stage, **artifact})
            stage_rec = {"dir": _rel(work, ev_dir), "evidence_sha256": ev_sha,
                         "artifact": artifact, "review": None,
                         "verdict": None}
            ver_rec[stage] = stage_rec
            save_visual_state(work, state)
            _comment(version=version, stage=stage, phase="pending",
                     body=ctrl.build_visual_pending_body(
                         child=child, run_id=run_id, run_attempt=run_attempt,
                         version=version, stage=stage, evidence_sha256=ev_sha,
                         artifact_name=name, artifact_url=artifact["url"],
                         repo=getattr(gh, "repo", "") if gh else ""))
            _guard(ctx)  # before the model call
            try:
                review = reviewer(evidence_dir=ev_dir, stage=stage,
                                  brief=brief, storyboard=current_sb,
                                  evidence=checked["evidence"])
            except VisualInfrastructureError:
                raise
            except Exception as e:  # noqa: BLE001 - agent failure is infra
                raise VisualInfrastructureError(
                    "visual reviewer failed (classification: generic)") from e
            valid = vp.validate_review(review, ev_dir)
            if not valid["ok"]:
                raise VisualInfrastructureError(
                    "visual review invalid: " + "; ".join(valid["failures"]))
            (ev_dir / "review.json").write_text(
                json.dumps(review, indent=2), encoding="utf-8")
            stage_rec["review"] = review
            stage_rec["verdict"] = review.get("verdict")
            last_review, last_stage, last_sha, last_artifact = \
                review, stage, ev_sha, artifact
            save_visual_state(work, state)
            _comment(version=version, stage=stage, phase="result",
                     body=ctrl.build_visual_result_body(
                         child=child, run_id=run_id, run_attempt=run_attempt,
                         version=version, stage=stage, review=review,
                         evidence_sha256=ev_sha,
                         artifact_url=artifact["url"],
                         artifact_id=artifact.get("id"),
                         repo=getattr(gh, "repo", "") if gh else ""))
            if review.get("verdict") != "pass":
                blocked = True
                break
        if not blocked:
            still_dir = work / ver_rec["still"]["dir"]
            motion_dir = work / ver_rec["motion"]["dir"]
            try:
                approval = vp.approved_binding(
                    still_dir, motion_dir, ctx,
                    expected_engine_sha=engine_sha, expected_run_id=run_id,
                    expected_run_attempt=run_attempt)
            except ValueError as e:
                raise VisualInfrastructureError(
                    f"visual approval failed: {e}") from e
            apath = _write_approval(work, still_dir, motion_dir,
                                    engine_sha=engine_sha, run_id=run_id,
                                    run_attempt=run_attempt)
            ver_rec["outcome"] = "passed"
            save_visual_state(work, state)
            _guard(ctx)  # before finishing
            return _result(status="passed", proceed=True, state=state,
                           version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, approval_path=apath,
                           approval=approval, work_dir=work)
        # A stage failed: route on the newest visual review.
        ver_rec["outcome"] = "failed"
        save_visual_state(work, state)
        route = ((last_review or {}).get("repair") or {}).get("route")
        if route == "engine":
            _handoff(stage=last_stage, review=last_review,
                     evidence_sha256=last_sha, artifact=last_artifact,
                     status="needs_engine")
            return _result(status="needs_engine", proceed=False, state=state,
                           version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, work_dir=work)
        if route != "storyboard":
            raise VisualInfrastructureError(
                f"failing visual review has unroutable route: {route!r}")
        if not allow_repair:
            return _result(status="visual_blocked", proceed=False, state=state,
                           version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, work_dir=work)
        if muse_repair is None \
                or int(state.get("repairs_used", 0)) >= int(config.MAX_REPAIRS):
            _handoff(stage=last_stage, review=last_review,
                     evidence_sha256=last_sha, artifact=last_artifact,
                     status="attempts_exhausted")
            return _result(status="attempts_exhausted", proceed=False,
                           state=state, version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, work_dir=work)
        _guard(ctx)  # before the Muse repair call
        state["repairs_used"] = int(state["repairs_used"]) + 1
        save_visual_state(work, state)  # budget consumed before the call
        try:
            new_sb = muse_repair(review=last_review, storyboard=current_sb,
                                 brief=brief, scenes=scene_list)
        except VisualInfrastructureError:
            raise
        except Exception as e:  # noqa: BLE001 - agent failure is infra
            raise VisualInfrastructureError(
                "Muse visual repair failed (classification: generic)") from e
        if not isinstance(new_sb, dict):
            raise VisualInfrastructureError(
                "Muse visual repair produced no storyboard")
        if fingerprint.canonical_json(new_sb) == \
                fingerprint.canonical_json(current_sb):
            ver_rec["outcome"] = "no_change"
            save_visual_state(work, state)
            _handoff(stage=last_stage, review=last_review,
                     evidence_sha256=last_sha, artifact=last_artifact,
                     status="no_change")
            return _result(status="no_change", proceed=False, state=state,
                           version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, work_dir=work)
        valid_sb = sbmod.validate_storyboard(new_sb, scene_list)
        if not valid_sb["ok"]:
            raise VisualInfrastructureError(
                "repaired storyboard invalid: "
                + "; ".join(valid_sb["failures"]))
        if version >= MAX_VERSIONS:
            _handoff(stage=last_stage, review=last_review,
                     evidence_sha256=last_sha, artifact=last_artifact,
                     status="attempts_exhausted")
            return _result(status="attempts_exhausted", proceed=False,
                           state=state, version=version, storyboard=current_sb,
                           stage=last_stage, review=last_review,
                           evidence_sha256=last_sha, work_dir=work)
        try:
            prev_ev = json.loads(
                (work / ver_rec["still"]["dir"] / "evidence.json")
                .read_text(encoding="utf-8"))
            previous_still_media = prev_ev.get("media_sha256")
        except Exception:  # noqa: BLE001
            previous_still_media = None
        current_sb = new_sb
        first_ctx = _context(current_sb)


def _write_approval(work: Path, still_dir: Path, motion_dir: Path, *,
                    engine_sha: str, run_id: int, run_attempt: int) -> Path:
    root = Path(vp.REPO_ROOT)

    def _ref(d: Path) -> str:
        try:
            return str(Path(d).resolve().relative_to(root.resolve()))
        except ValueError:
            raise VisualInfrastructureError(
                "visual approval evidence must be inside the repository") from None

    approval = {"schema_version": "1.0", "still_dir": _ref(still_dir),
                "motion_dir": _ref(motion_dir), "engine_sha": engine_sha,
                "run_id": int(run_id), "run_attempt": int(run_attempt)}
    path = Path(work) / APPROVAL_FILENAME
    path.write_text(json.dumps(approval, indent=2), encoding="utf-8")
    return path


# --- aggregate visual history ----------------------------------------------

def validate_visual_history(work_dir) -> dict:
    """Strictly validate state snapshots against evidence bindings.

    Every version's persisted storyboard must hash to the
    ``storyboard_sha256`` inside its evidence binding, and the exact
    brief/candidate/scenes snapshots must hash to the binding's
    brief/candidate hashes, so history explains the actual artistic
    inputs rather than only hashes of unavailable data.
    """
    failures: list[str] = []
    work = Path(work_dir)
    try:
        state = load_visual_state(work)
    except VisualInfrastructureError as e:
        return {"ok": False, "failures": [str(e)], "state": None}
    if state["candidate"].get("scenes") != state["scenes"]:
        failures.append("candidate and scene snapshots differ")
    if state["brief"].get("candidate") != state["candidate"]:
        failures.append("brief and candidate snapshots differ")
    for ver in state.get("versions", []):
        for stage in ("still", "motion"):
            rec = ver.get(stage)
            if not isinstance(rec, dict):
                continue
            ref = rec.get("dir")
            expected_ref = f"visual/v{ver['version']}/{stage}"
            if ref != expected_ref:
                failures.append("visual stage reference does not match version/stage")
                continue
            ev_dir = work / ref
            if any(p.is_symlink() for p in (ev_dir, *ev_dir.parents)):
                failures.append("visual stage reference traverses a symlink")
                continue
            checked = vp.validate_evidence(
                ev_dir, expected_engine_sha=state["engine_sha"],
                expected_run_id=state["run_id"],
                expected_run_attempt=state["run_attempt"])
            if not checked["ok"]:
                failures.extend(
                    f"v{ver.get('version')} {stage}: {f}"
                    for f in checked["failures"])
                continue
            binding = (checked.get("evidence") or {}).get("binding") or {}
            ev = checked["evidence"]
            identity = ev["identity"]
            for key, expected in (("iteration", ver["version"] - 1),
                                  ("parent_issue", state["parent_issue"]),
                                  ("child_issue", state["child_issue"]),
                                  ("candidate_id", state["candidate_id"])):
                if identity.get(key) != expected:
                    failures.append(f"visual history identity {key} mismatch")
            if ev["stage"] != stage or rec.get("evidence_sha256") != checked["evidence_sha256"]:
                failures.append("visual stage/digest mismatch")
            if binding.get("source_sha256") != state["source_sha256"]:
                failures.append("visual source binding mismatch")
            plan = ev["plan"]
            if canonical_hash(plan) != binding.get("public_plan_sha256"):
                failures.append("visual plan digest mismatch")
            expected_scenes = state["scenes"]
            plan_scenes = plan.get("scenes", [])
            timing_keys = ("id", "start_sample", "end_sample", "start_frame", "end_frame")
            if [[s.get(k) for k in timing_keys] for s in plan_scenes] != \
                    [[s.get(k) for k in timing_keys] for s in expected_scenes]:
                failures.append("visual plan and scene snapshot timing mismatch")
            for key in ("start_sample", "end_sample", "n_frames"):
                if plan.get("clip", {}).get(key) != state["candidate"].get(key):
                    failures.append(f"visual plan candidate {key} mismatch")
            if binding.get("storyboard_sha256") != ver.get("storyboard_sha256"):
                failures.append(
                    f"v{ver.get('version')} {stage}: storyboard snapshot "
                    "does not match evidence binding")
            if binding.get("brief_sha256") != state["brief_sha256"]:
                failures.append(
                    f"v{ver.get('version')} {stage}: brief snapshot "
                    "does not match evidence binding")
            if binding.get("candidate_sha256") != state["candidate_sha256"]:
                failures.append(
                    f"v{ver.get('version')} {stage}: candidate snapshot "
                    "does not match evidence binding")
            review_path = ev_dir / "review.json"
            if review_path.is_file():
                valid = vp.validate_review(review_path, ev_dir)
                if not valid["ok"]:
                    failures.extend(f"invalid history review: {f}" for f in valid["failures"])
                    continue
                review = json.loads(review_path.read_text())
                if review != rec.get("review") or review.get("verdict") != rec.get("verdict"):
                    failures.append("visual history review differs from persisted result")
            else:
                missing_allowed = ver.get("outcome") == "pending" or (
                    ver.get("outcome") == "no_visual_change"
                    and rec.get("review_status") == "not_reviewed_no_visual_change")
                if not missing_allowed or rec.get("review") is not None \
                        or rec.get("verdict") is not None:
                    failures.append("reviewed visual stage is missing its review")
        if ver.get("outcome") == "passed" and any(
                (ver.get(s) or {}).get("verdict") != "pass" for s in ("still", "motion")):
            failures.append("passed visual version requires both recorded pass reviews")
    return {"ok": not failures, "failures": failures, "state": state}


def build_visual_history(work_dir, out_dir=None) -> Path:
    """Copy validated state + reviewed stage bundles into one aggregate.

    The aggregate (uploaded as ONE additional history artifact after
    strict validation) carries the machine-readable ``review.json``
    files and full reviewed stage bundles alongside the exact
    brief/candidate/scenes and each version's storyboard.
    """
    work = Path(work_dir)
    checked = validate_visual_history(work)
    if not checked["ok"]:
        raise VisualInfrastructureError(
            "visual history invalid: " + "; ".join(checked["failures"]))
    state = checked["state"]
    out = Path(out_dir) if out_dir else work / "visual-history"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / STATE_FILENAME).write_text(json.dumps(state, indent=2),
                                      encoding="utf-8")
    for name, key in (("brief.json", "brief"), ("candidate.json", "candidate"),
                      ("scenes.json", "scenes")):
        (out / name).write_text(json.dumps(state[key], indent=2),
                                encoding="utf-8")
    for ver in state.get("versions", []):
        vdir = out / f"v{ver['version']}"
        vdir.mkdir(parents=True)
        (vdir / "storyboard.json").write_text(
            json.dumps(ver["storyboard"], indent=2), encoding="utf-8")
        for stage in ("still", "motion"):
            rec = ver.get(stage)
            if not isinstance(rec, dict):
                continue
            src = work / rec["dir"]
            dst = vdir / stage
            dst.mkdir(parents=True)
            for entry in sorted(src.iterdir(), key=lambda p: p.name):
                if entry.is_file() and not entry.is_symlink():
                    shutil.copyfile(entry, dst / entry.name)
    return out

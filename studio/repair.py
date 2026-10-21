"""Bounded aesthetic review/repair loop (initial + at most 2 repairs).

This module is the reusable orchestration core used by
``scripts/studio_repair.py``. The loop itself performs no GitHub,
model, rendering or network calls: a caller-injected ``attempt_runner``
performs one repair attempt (Muse storyboard -> trusted render ->
commit -> receipt -> Codex re-review in production; a fake in tests)
and the loop owns routing, bounds, no-progress detection, review
validation and the final status decision. No aesthetic pass is ever
manufactured: a pass requires the real review gate (all four checks,
no blockers, mechanical QA) on a structurally valid, head-bound
review.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import uuid
from pathlib import Path

from . import artifacts, config, fingerprint, gating, storyboard as sbmod

MAX_REPAIRS = config.MAX_REPAIRS
MAX_ATTEMPTS = config.REPAIR_MAX_ATTEMPTS
AGENT_TIMEOUT_SEC = config.REPAIR_AGENT_TIMEOUT_SEC

STATUSES = ("passed", "needs_engine", "attempts_exhausted",
            "no_change", "no_visual_change")
ESCALATION_STATUSES = ("needs_engine", "attempts_exhausted",
                       "no_change", "no_visual_change")

_NEXT_ACTIONS = {
    "passed": ("Publish the final reviewed bundle and request exact-head "
               "owner review; publication still requires owner approval "
               "and merge."),
    "needs_engine": ("Engineering implementation and review before "
                     "rerunning the source workflow."),
    "attempts_exhausted": ("Engineering implementation and review before "
                           "rerunning the source workflow."),
    "no_change": ("Engineering implementation and review before "
                  "rerunning the source workflow."),
    "no_visual_change": ("Engineering implementation and review before "
                         "rerunning the source workflow."),
}

WORKER_FILES = ("brief.json", "scenes.json", "storyboard.schema.json",
                "prompt.md", "previous-review.json",
                "previous-storyboard.json")


class RepairError(RuntimeError):
    """Base class for bounded-repair failures."""


class RepairInfrastructureError(RepairError):
    """Malformed/stale/drift/model failure: fail closed, never publish."""


# Backwards-friendly alias.
InfrastructureError = RepairInfrastructureError


def next_action_for(status: str) -> str:
    return _NEXT_ACTIONS.get(
        status,
        "Engineering implementation and review before rerunning the "
        "source workflow.")


# --- storyboard / timing invariants -------------------------------------

def canonical_storyboard(storyboard) -> str:
    return fingerprint.canonical_json(storyboard)


def storyboard_is_unchanged(previous, new) -> bool:
    if previous is None or new is None:
        return False
    return canonical_storyboard(previous) == canonical_storyboard(new)


def validate_new_storyboard(storyboard, deterministic_scenes) -> dict:
    return sbmod.validate_storyboard(storyboard, deterministic_scenes)


def verify_plan_preserved(initial_plan, new_plan) -> dict:
    """Clip window, scene boundaries and accents must match EXACTLY."""
    failures: list[str] = []
    if not isinstance(initial_plan, dict) or not isinstance(new_plan, dict):
        return {"ok": False, "failures": ["plan must be an object"]}
    for key in ("clip", "scenes", "accents"):
        if new_plan.get(key) != initial_plan.get(key):
            failures.append(f"plan {key} changed (timings must be preserved)")
    return {"ok": not failures, "failures": failures}


def verify_source_against_brief(decoded_wav_sha256: str, brief) -> dict:
    failures: list[str] = []
    hashes = (brief or {}).get("source_hashes") if isinstance(brief, dict) else None
    want = hashes.get("decoded_wav_sha256") if isinstance(hashes, dict) else None
    if not want:
        failures.append("brief source_hashes.decoded_wav_sha256 missing")
    elif decoded_wav_sha256 != want:
        failures.append("source WAV sha256 does not match brief (source drift)")
    return {"ok": not failures, "failures": failures}


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# --- media / bundle helpers ----------------------------------------------

def media_sha_map(files: dict) -> dict:
    """sha256 of the clean media files present in a bundle file map."""
    out = {}
    for name in artifacts.MEDIA_SHA256_FILES:
        data = (files or {}).get(name)
        if data is not None:
            out[name] = artifacts.sha256_bytes(data)
    return out


def media_digest(files: dict) -> str | None:
    m = media_sha_map(files)
    video = m.get("video.mp4")
    return video


def _media_key(value):
    if isinstance(value, dict):
        return value.get("video.mp4") or fingerprint.canonical_json(value)
    return value


def read_bundle_files(bundle_dir) -> dict:
    bundle = Path(bundle_dir)
    if not bundle.is_dir():
        raise RepairInfrastructureError(f"bundle directory missing: {bundle.name}")
    files: dict[str, bytes] = {}
    for p in sorted(bundle.iterdir()):
        if p.is_symlink():
            raise RepairInfrastructureError(f"symlink in bundle: {p.name}")
        if p.is_file():
            files[p.name] = p.read_bytes()
    return files


def verify_bundle_files(files: dict, *, expected_head_sha: str,
                        pr_number: int, child: int, parent: int,
                        run_id: int | None = None,
                        run_attempt: int | None = None,
                        engine_sha: str | None = None,
                        production_files: dict | None = None,
                        require_repair: bool = True) -> dict:
    """Validate a 12-file reviewed bundle: allowlist, hash manifest,
    receipt binding, QA pass and strictly-bound review structure.

    Beyond self-consistency of ``hashes.json`` this independently
    recomputes every committed production file digest from the actual
    bundle bytes (mapped to ``production/<child>/<name>``) and checks
    media bytes against the committed ``provenance.json`` via
    :func:`artifacts.verify_media_provenance`. A rehashed tampered
    video or storyboard therefore still fails: the receipt/provenance
    digests bound to the PR head do not move. Non-object JSON yields
    failures, never an incidental exception.
    """
    failures: list[str] = []
    expected = set(config.EXPECTED_ARTIFACT_FILES)
    if not isinstance(files, dict) or set(files) != expected:
        failures.append(
            f"bundle file set {sorted(files) if isinstance(files, dict) else files!r} "
            f"!= {sorted(expected)}")
        return {"ok": False, "failures": failures, "mechanical_pass": False,
                "review": None, "receipt": None, "qa": None,
                "brief": None, "provenance": None}
    def _json(name):
        try:
            value = json.loads(files[name])
        except Exception:  # noqa: BLE001 - malformed bundle JSON
            failures.append(f"bundle {name} is not valid JSON")
            return None
        if not isinstance(value, dict):
            failures.append(f"bundle {name} must be an object")
            return None
        return value
    manifest = _json("hashes.json")
    if isinstance(manifest, dict):
        v = artifacts.verify_hash_manifest(
            manifest, {k: v2 for k, v2 in files.items() if k != "hashes.json"})
        failures.extend(v["failures"])
    elif manifest is not None:
        failures.append("bundle hashes.json must be an object")
    qa = _json("qa.json")
    if qa is not None and not isinstance(qa, dict):
        failures.append("bundle qa.json must be an object")
    mechanical_pass = isinstance(qa, dict) and qa.get("pass") is True
    if not mechanical_pass:
        failures.append("mechanical QA did not pass")
    review = _json("review.json")
    if review is not None:
        if not isinstance(review, dict):
            failures.append("bundle review.json must be an object")
        else:
            failures.extend(gating.validate_review_structure(
                review, expected_head_sha=expected_head_sha,
                require_repair=require_repair)["failures"])
    receipt = _json("receipt.json")
    brief = _json("brief.json")
    provenance = _json("provenance.json")
    for _name, _obj in (("receipt.json", receipt), ("brief.json", brief),
                        ("provenance.json", provenance)):
        if _obj is not None and not isinstance(_obj, dict):
            failures.append(f"bundle {_name} must be an object")
    if isinstance(brief, dict):
        if brief.get("child_issue") != child:
            failures.append("brief child_issue does not match child")
        if brief.get("parent_issue") != parent:
            failures.append("brief parent_issue does not match parent")
    # Independently computed committed digests from actual bytes.
    actual_production = {
        f"production/{child}/{name}": artifacts.sha256_bytes(files[name])
        for name in config.COMMITTED_PRODUCTION_FILES}
    if isinstance(production_files, dict):
        if production_files != actual_production:
            failures.append(
                "supplied production_files do not match actual bundle bytes")
    elif production_files is not None:
        failures.append("production_files must be an object")
    if isinstance(receipt, dict):
        pf = production_files
        if pf is None:
            pf = receipt.get("production_files") or {}
        rc = artifacts.validate_receipt(
            receipt, pr=pr_number, child=child, parent=parent,
            head_sha=expected_head_sha, production_files=pf,
            expected_run_id=run_id, expected_run_attempt=run_attempt,
            expected_engine_sha=engine_sha,
            expected_source_hashes=(brief or {}).get("source_hashes")
            if isinstance(brief, dict) else None,
            expected_planning_fingerprint=(brief or {}).get("fingerprint")
            if isinstance(brief, dict) else None)
        failures.extend(rc["failures"])
        receipt_pf = receipt.get("production_files")
        if isinstance(receipt_pf, dict) and receipt_pf != actual_production:
            failures.append(
                "receipt production_files do not match actual bundle bytes")
        if isinstance(brief, dict) and isinstance(provenance, dict):
            failures.extend(artifacts.verify_source_hashes(
                receipt, brief, provenance)["failures"])
        elif brief is not None and provenance is not None:
            failures.append(
                "source hash verification requires object brief/provenance")
    if isinstance(provenance, dict):
        failures.extend(artifacts.verify_media_provenance(
            provenance, files,
            manifest if isinstance(manifest, dict) else {})["failures"])
    elif provenance is not None:
        failures.append("media provenance verification requires object provenance")
    return {"ok": not failures, "failures": failures,
            "mechanical_pass": mechanical_pass,
            "review": review if isinstance(review, dict) else None,
            "receipt": receipt if isinstance(receipt, dict) else None,
            "qa": qa if isinstance(qa, dict) else None,
            "brief": brief if isinstance(brief, dict) else None,
            "provenance": provenance if isinstance(provenance, dict) else None}


def snapshot_bundle(bundle_dir, attempts_dir, attempt_number: int) -> Path:
    """Immutable allowlisted copy into ``attempt-NN`` (no WAV/logs/auth)."""
    all_files = read_bundle_files(bundle_dir)
    missing = set(config.EXPECTED_ARTIFACT_FILES) - set(all_files)
    if missing:
        raise RepairInfrastructureError(
            f"cannot snapshot bundle: missing allowlisted files {sorted(missing)}")
    # Copy ONLY the allowlist: source audio/logs can never enter history.
    src_files = {n: all_files[n] for n in config.EXPECTED_ARTIFACT_FILES}
    dest = Path(attempts_dir) / f"attempt-{attempt_number:02d}"
    if dest.exists():
        existing = read_bundle_files(dest)
        if existing != src_files:
            raise RepairInfrastructureError(
                f"immutable attempt snapshot already exists and differs: "
                f"attempt-{attempt_number:02d}")
        return dest
    dest.mkdir(parents=True)
    for name in sorted(config.EXPECTED_ARTIFACT_FILES):
        (dest / name).write_bytes(src_files[name])
    return dest


def validate_attempt_history(attempts_dir, *, parent: int | None = None,
                             child: int | None = None,
                             pr_number: int | None = None,
                             run_id: int | None = None,
                             run_attempt: int | None = None,
                             engine_sha: str | None = None) -> dict:
    """Strict review-attempt history validation (read-only).

    The root must contain ONLY sequential ``attempt-01``..``attempt-NN``
    directories (1..3, no symlinks, no extra files or directories) and
    each must be a fully reviewed allowlisted 12-file bundle passing
    :func:`verify_bundle_files` (hash/receipt/media/committed digests).
    When caller expectations are supplied every receipt must match
    them; otherwise receipts must be mutually consistent (same
    pr/child/parent/run/engine, each bound to its own review head).
    """
    failures: list[str] = []
    base = Path(attempts_dir)
    if base.is_symlink() or not base.is_dir():
        return {"ok": False, "failures": ["history directory missing"],
                "count": 0, "attempts": []}
    entries = sorted(base.iterdir(), key=lambda p: p.name)
    names = [p.name for p in entries]
    if not names:
        return {"ok": False, "failures": ["history contains no attempts"],
                "count": 0, "attempts": []}
    if len(names) > MAX_ATTEMPTS:
        failures.append(f"history has {len(names)} attempts, max {MAX_ATTEMPTS}")
    expected_names = [f"attempt-{i:02d}" for i in range(1, len(names) + 1)]
    if names != expected_names:
        failures.append(
            f"history entries {names} != sequential {expected_names}")
    attempts: list[dict] = []
    reference: dict | None = None
    for entry in entries:
        if entry.is_symlink():
            failures.append(f"history entry is a symlink: {entry.name}")
            continue
        if not entry.is_dir():
            failures.append(f"history entry is not a directory: {entry.name}")
            continue
        sub = sorted(entry.iterdir(), key=lambda p: p.name)
        for item in sub:
            if item.is_symlink():
                failures.append(f"symlink in history bundle: {entry.name}/{item.name}")
            elif not item.is_file():
                failures.append(
                    f"non-file in history bundle: {entry.name}/{item.name}")
        if {p.name for p in sub if p.is_file() and not p.is_symlink()} \
                != set(config.EXPECTED_ARTIFACT_FILES) or len(sub) != 12:
            failures.append(
                f"history bundle {entry.name} is not the allowlisted 12-file bundle")
            continue
        try:
            files = read_bundle_files(entry)
        except RepairInfrastructureError as e:
            failures.append(str(e))
            continue
        receipt = None
        try:
            parsed = json.loads(files["receipt.json"])
            receipt = parsed if isinstance(parsed, dict) else None
        except Exception:  # noqa: BLE001
            receipt = None
        if receipt is None:
            failures.append(f"history bundle {entry.name} receipt must be an object")
            continue
        head = receipt.get("head_sha")
        check = verify_bundle_files(
            files, expected_head_sha=head if isinstance(head, str) else "",
            pr_number=pr_number if pr_number is not None
            else int(receipt.get("pr") or 0),
            child=child if child is not None else int(receipt.get("child") or 0),
            parent=parent if parent is not None else int(receipt.get("parent") or 0),
            run_id=run_id if run_id is not None else receipt.get("run_id"),
            run_attempt=run_attempt if run_attempt is not None
            else receipt.get("run_attempt"),
            engine_sha=engine_sha if engine_sha is not None
            else receipt.get("engine_sha"),
            production_files=receipt.get("production_files"),
            require_repair=True)
        failures.extend(f"{entry.name}: {f}" for f in check["failures"])
        if check["ok"]:
            attempts.append({"attempt": entry.name, "head_sha": head,
                             "pr_number": receipt.get("pr")})
            binding = {k: receipt.get(k) for k in
                       ("pr", "child", "parent", "run_id", "run_attempt",
                        "engine_sha")}
            if reference is None:
                reference = binding
            elif binding != reference:
                failures.append(
                    f"history bundle {entry.name} receipt binding differs across attempts")
    return {"ok": not failures, "failures": failures,
            "count": len(names), "attempts": attempts}


def replace_bundle_from_snapshot(snapshot_dir, bundle_dir) -> None:
    files = read_bundle_files(snapshot_dir)
    if set(files) != set(config.EXPECTED_ARTIFACT_FILES):
        raise RepairInfrastructureError(
            "final snapshot is not the allowlisted 12-file bundle")
    bundle = Path(bundle_dir)
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    for name in sorted(files):
        (bundle / name).write_bytes(files[name])


# --- reusable bounded loop -------------------------------------------------

def _result(*, status, attempt_count, max_repairs, head_sha, pr_number,
            review, history, repairs_attempted: int = 0):
    attempted = int(repairs_attempted)
    return {"status": status, "attempt_count": int(attempt_count),
            "max_repairs": int(max_repairs),
            "repairs_attempted": attempted,
            "repair_count": attempted,
            "muse_repair_calls": attempted,
            "repairs_used": attempted,
            "head_sha": head_sha, "final_head_sha": head_sha,
            "pr_number": pr_number, "final_pr_number": pr_number,
            "final_review": review, "history": history,
            "next_action": next_action_for(status)}


def run_repair_loop(*, initial_review, initial_mechanical_pass: bool,
                    initial_head_sha: str, initial_pr_number: int,
                    attempt_runner, snapshot_attempt=None,
                    initial_storyboard=None, initial_media_sha256=None,
                    max_repairs: int = MAX_REPAIRS) -> dict:
    """Bounded loop over an injected attempt runner.

    ``attempt_runner(repair_index, context)`` returns either
    ``{"kind": "reviewed", "review": ..., "head_sha": ..., ...}``,
    ``{"kind": "no_change"}`` or ``{"kind": "no_visual_change"}``; it
    may raise :class:`RepairInfrastructureError` for model/render/
    commit/review infra failures, which propagate fail-closed.
    ``snapshot_attempt(attempt_number, outcome)`` (optional) persists
    each valid reviewed attempt bundle. The SAME function drives the
    production script and the tests.
    """
    structure = gating.validate_review_structure(
        initial_review, expected_head_sha=initial_head_sha,
        require_repair=True)
    if not structure["ok"]:
        raise RepairInfrastructureError(
            "initial review malformed/stale: " + "; ".join(structure["failures"]))
    if initial_mechanical_pass is not True:
        raise RepairInfrastructureError("mechanical QA did not pass")
    history = [{"attempt": 1, "head_sha": initial_head_sha,
                "verdict": initial_review.get("verdict"),
                "route": gating.review_route(initial_review)}]
    gate = gating.evaluate_review_gate(
        initial_review, mechanical_pass=initial_mechanical_pass,
        expected_head_sha=initial_head_sha, require_repair=True)
    if gate["ok"]:
        return _result(status="passed", attempt_count=1,
                       max_repairs=max_repairs, head_sha=initial_head_sha,
                       pr_number=initial_pr_number, review=initial_review,
                       history=history)
    route = gating.review_route(initial_review)
    if route == "engine":
        return _result(status="needs_engine", attempt_count=1,
                       max_repairs=max_repairs, head_sha=initial_head_sha,
                       pr_number=initial_pr_number, review=initial_review,
                       history=history)
    if route != "storyboard":
        raise RepairInfrastructureError(
            f"failing review has unroutable repair route: {route!r}")
    current_review = initial_review
    current_head = initial_head_sha
    current_pr = initial_pr_number
    current_storyboard = initial_storyboard
    current_media = _media_key(initial_media_sha256)
    attempt_count = 1
    repairs_attempted = 0
    for repair_index in range(1, int(max_repairs) + 1):
        context = {"repair_index": repair_index,
                   "attempt_number": attempt_count + 1,
                   "previous_review": current_review,
                   "previous_head_sha": current_head,
                   "previous_pr_number": current_pr,
                   "previous_storyboard": current_storyboard,
                   "previous_media_sha256": current_media}
        repairs_attempted += 1
        outcome = attempt_runner(repair_index, context)
        if not isinstance(outcome, dict):
            raise RepairInfrastructureError(
                "repair attempt runner returned no outcome")
        kind = outcome.get("kind") or outcome.get("outcome") or (
            "reviewed" if "review" in outcome else None)
        if kind in ("no_change", "no_visual_change"):
            return _result(status=kind, attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
        if kind != "reviewed":
            raise RepairInfrastructureError(
                f"unknown repair attempt outcome: {kind!r}")
        new_review = outcome.get("review")
        new_head = outcome.get("head_sha") or outcome.get("new_head_sha")
        if not new_head or not isinstance(new_review, dict):
            raise RepairInfrastructureError(
                "repair attempt produced no bound review/head")
        # No-progress detection precedes counting/snapshotting: an
        # identical storyboard or identical clean media is not progress.
        new_storyboard = outcome.get("storyboard")
        if new_storyboard is not None and current_storyboard is not None \
                and storyboard_is_unchanged(current_storyboard, new_storyboard):
            return _result(status="no_change", attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
        new_media = _media_key(outcome.get("media_sha256",
                                           outcome.get("media_digest")))
        if new_media is not None and current_media is not None \
                and new_media == current_media:
            return _result(status="no_visual_change",
                           attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
        mech = outcome.get("mechanical_pass")
        if mech is not True:
            raise RepairInfrastructureError(
                "repair attempt mechanical QA did not pass")
        structure = gating.validate_review_structure(
            new_review, expected_head_sha=new_head, require_repair=True)
        if not structure["ok"]:
            raise RepairInfrastructureError(
                "repair review malformed/stale: "
                + "; ".join(structure["failures"]))
        if snapshot_attempt is not None:
            snapshot_attempt(attempt_count + 1, outcome)
        attempt_count += 1
        current_review = new_review
        current_head = new_head
        current_pr = int(outcome.get("pr_number") or current_pr)
        if new_storyboard is not None:
            current_storyboard = new_storyboard
        if new_media is not None:
            current_media = new_media
        history.append({"attempt": attempt_count, "head_sha": new_head,
                        "verdict": new_review.get("verdict"),
                        "route": gating.review_route(new_review)})
        gate = gating.evaluate_review_gate(
            new_review, mechanical_pass=mech, expected_head_sha=new_head,
            require_repair=True)
        if gate["ok"]:
            return _result(status="passed", attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
        new_route = gating.review_route(new_review)
        if new_route == "engine":
            return _result(status="needs_engine", attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
        if new_route != "storyboard":
            raise RepairInfrastructureError(
                f"failing review has unroutable repair route: {new_route!r}")
        if repair_index >= int(max_repairs):
            return _result(status="attempts_exhausted",
                           attempt_count=attempt_count,
                           max_repairs=max_repairs, head_sha=current_head,
                           pr_number=current_pr, review=current_review,
                           history=history,
                           repairs_attempted=repairs_attempted)
    return _result(status="attempts_exhausted", attempt_count=attempt_count,
                   max_repairs=max_repairs, head_sha=current_head,
                   pr_number=current_pr, review=current_review,
                   history=history, repairs_attempted=repairs_attempted)


# --- Muse workspace / prompts / agent invocation --------------------------

def build_muse_repair_prompt(base_prompt: str, *, brief, scenes,
                             prior_review, previous_storyboard) -> str:
    """Labelled repair prompt: original brief/scenes plus the FULL prior
    review (blockers + instructions) and previous storyboard, as data."""
    parts = [base_prompt.rstrip(), "",
             "## brief.json (verbatim)", json.dumps(brief, indent=2),
             "", "## scenes.json (deterministic)",
             json.dumps(scenes, indent=2), "",
             "## Previous review (data: full blockers and repair "
             "instructions to address)",
             json.dumps(prior_review, indent=2), "",
             "## Blockers (verbatim)",
             json.dumps((prior_review or {}).get("blockers", []), indent=2),
             "", "## Repair instructions (verbatim)",
             json.dumps(gating.review_instructions(prior_review), indent=2),
             "", "## Previous storyboard (data only)",
             json.dumps(previous_storyboard, indent=2), "",
             "Address the blockers/instructions above ONLY through the "
             "executed per-scene palette colours and the ten bounded "
             "numeric params. Motif/camera prose is not executed. Use "
             "the deterministic scene IDs exactly; never invent timing."]
    return "\n".join(parts) + "\n"


def build_codex_review_prompt(base_prompt: str, *, brief, storyboard,
                              production_files, expected_head_sha, plan,
                              qa, hashes, provenance, receipt) -> str:
    def _j(v):
        return v if isinstance(v, str) else json.dumps(v, indent=2)
    parts = [base_prompt.rstrip(), "",
             f"Expected head SHA: {expected_head_sha}", "",
             "## 1 SOURCE BRIEF brief.json", _j(brief), "",
             "## 2 STORYBOARD storyboard.json", _j(storyboard), "",
             "## 3 PRODUCTION FILES (committed digests)", _j(production_files),
             "", "## 4 PLAN plan.json", _j(plan), "",
             "## 4 QA qa.json", _j(qa), "",
             "## 5 HASHES hashes.json", _j(hashes), "",
             "## PROVENANCE provenance.json", _j(provenance), "",
             "## RECEIPT receipt.json", _j(receipt)]
    return "\n".join(parts) + "\n"


def prepare_muse_workspace(workspace, *, brief, scenes, storyboard_schema,
                           prompt: str, prior_review,
                           previous_storyboard) -> list:
    """Fresh isolated worker workspace (allowlisted files ONLY).

    Any stale directory (including a stale ``storyboard.json``) is
    removed first. Contains no source audio, no trusted checkout, no
    credentials: only brief/scenes/schema/prompt plus prior review and
    previous storyboard as data.
    """
    ws = Path(workspace)
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True)
    scenes_obj = scenes if isinstance(scenes, dict) else {"scenes": scenes}
    schema_text = storyboard_schema if isinstance(storyboard_schema, str) \
        else json.dumps(storyboard_schema, indent=2)
    files = {
        "brief.json": json.dumps(brief, indent=2),
        "scenes.json": json.dumps(scenes_obj, indent=2),
        "storyboard.schema.json": schema_text,
        "prompt.md": prompt,
        "previous-review.json": json.dumps(prior_review, indent=2),
        "previous-storyboard.json": json.dumps(previous_storyboard, indent=2),
    }
    for name, text in files.items():
        (ws / name).write_text(text, encoding="utf-8")
    return sorted(files)


def sanitized_worker_env(env=None) -> dict:
    """Environment for the isolated worker: no GitHub credentials."""
    out = dict(os.environ if env is None else env)
    for key in ("GH_TOKEN", "GITHUB_TOKEN"):
        out.pop(key, None)
    return out


def classify_agent_error(stderr_path, report_script) -> str:
    """Safe fixed classification only; raw provider output never leaks."""
    try:
        r = subprocess.run(
            [sys.executable, str(report_script), str(stderr_path)],
            capture_output=True, text=True, timeout=30)
        text = (r.stdout or "").strip()
        return text or "classification: generic"
    except Exception:  # noqa: BLE001 - diagnostics must not mask failure
        return "classification: generic"


_CONTAINER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
DOCKER_CLEANUP_TIMEOUT_SEC = 15
_PROCESS_TERM_GRACE_SEC = 5


def _terminate_process_group(proc, *, grace: float = _PROCESS_TERM_GRACE_SEC) -> None:
    """SIGTERM the whole process group, then SIGKILL after a bounded
    grace, and reap. No child (Docker CLI, Codex, renderer) survives."""
    if proc.poll() is not None:
        return
    def _signal_group(sig):
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
            except Exception:  # noqa: BLE001
                pass
    _signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The direct child may exit while a descendant ignores SIGTERM.
    # Its process group still belongs to this invocation; kill that
    # group even when wait() has already reaped the direct child.
    _signal_group(signal.SIGKILL)
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass


def _remove_owned_container(container_name, env=None) -> bool:
    """Remove ONLY the uniquely owned container; True when confirmed
    gone (removed or already absent). Never touches other containers."""
    if not container_name or not _CONTAINER_NAME_RE.match(str(container_name)):
        return False
    try:
        r = subprocess.run(
            ["docker", "rm", "-f", str(container_name)],
            capture_output=True, text=True,
            timeout=DOCKER_CLEANUP_TIMEOUT_SEC, env=env)
    except Exception:  # noqa: BLE001 - unconfirmed cleanup fails closed
        return False
    if r.returncode == 0:
        return True
    combined = ((r.stdout or "") + (r.stderr or "")).lower()
    return "no such container" in combined


def record_muse_cleanup(safe: bool, env=None) -> None:
    """Tell later Actions steps whether credential persistence is safe.

    Only the trusted host writes GITHUB_ENV; it is never mounted in
    the worker. Mark unsafe before launch, safe only after cleanup.
    """
    environment = os.environ if env is None else env
    path = environment.get("GITHUB_ENV")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"STUDIO_MUSE_CLEANUP_SAFE={'true' if safe else 'false'}\n")


def cleanup_muse_container(container_name: str, env=None) -> bool:
    safe = _remove_owned_container(container_name, env=env)
    record_muse_cleanup(safe, env=env)
    return safe


def run_agent_command(command: list, *, stdout_path, stderr_path,
                      stdin_path=None, timeout: int = AGENT_TIMEOUT_SEC,
                      env=None, report_script=None, label: str = "agent",
                      container_name: str | None = None):
    """Run an agent via an argument list (no shell), capturing provider
    stdout/stderr ONLY under the caller-provided (RUNNER_TEMP) paths.

    On timeout the ENTIRE process group is terminated (SIGTERM then
    bounded SIGKILL) and reaped before raising, so shell wrappers
    cannot leave Docker/Codex/renderer children running. When
    ``container_name`` is given, that uniquely owned container is
    explicitly removed in a bounded ``docker rm -f`` before returning
    on every exit path; unconfirmed cleanup fails closed so callers
    never persist credentials while a model may still write.
    """
    Path(stdout_path).parent.mkdir(parents=True, exist_ok=True)
    stdin_f = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL
    proc = None
    timed_out = False
    cleanup_ok = True
    if container_name is not None:
        record_muse_cleanup(False, env=env)
    try:
        with open(stdout_path, "wb") as out, open(stderr_path, "wb") as err:
            try:
                proc = subprocess.Popen(
                    list(command), stdin=stdin_f, stdout=out, stderr=err,
                    env=env, start_new_session=True)
            except Exception as e:  # noqa: BLE001 - fail closed, no raw detail
                raise RepairInfrastructureError(
                    f"{label} failed to start (classification: generic)") from e
            try:
                proc.wait(timeout=int(timeout))
            except subprocess.TimeoutExpired:
                timed_out = True
                _terminate_process_group(proc)
    finally:
        if stdin_path:
            try:
                stdin_f.close()
            except Exception:  # noqa: BLE001
                pass
        if container_name is not None:
            cleanup_ok = cleanup_muse_container(container_name, env=env)
    def _detail():
        return classify_agent_error(stderr_path, report_script) \
            if report_script else "classification: generic"
    if timed_out:
        if not cleanup_ok:
            raise RepairInfrastructureError(
                f"{label} timed out after {timeout}s and owned container "
                f"cleanup unconfirmed ({_detail()})")
        raise RepairInfrastructureError(
            f"{label} timed out after {timeout}s ({_detail()})")
    if not cleanup_ok:
        raise RepairInfrastructureError(
            f"{label} owned container cleanup unconfirmed ({_detail()})")
    if proc is None or proc.returncode != 0:
        rc = proc.returncode if proc is not None else "unknown"
        raise RepairInfrastructureError(
            f"{label} failed with exit {rc} ({_detail()})")
    return proc


def build_muse_command(muse_run_script, workspace) -> list:
    return ["bash", str(muse_run_script), str(workspace)]


def run_muse_worker(workspace, *, muse_run_script, runner_temp,
                    report_script=None, timeout: int = AGENT_TIMEOUT_SEC,
                    env=None) -> None:
    rt = Path(runner_temp)
    worker_env = sanitized_worker_env(env)
    container_name = worker_env.get("MUSE_REPAIR_CONTAINER_NAME") or \
        f"studio-repair-{uuid.uuid4().hex[:16]}"
    worker_env["MUSE_REPAIR_CONTAINER_NAME"] = container_name
    run_agent_command(
        build_muse_command(muse_run_script, workspace),
        stdout_path=rt / "muse-repair.stdout",
        stderr_path=rt / "muse-repair.stderr",
        timeout=timeout, env=worker_env,
        report_script=report_script, label="Muse repair",
        container_name=container_name)


def build_codex_review_command(*, contact_sheet, schema_path, output_path) -> list:
    return ["codex", "--ask-for-approval", "never", "exec",
            "--strict-config", "--ephemeral", "--model", config.CODEX_MODEL,
            "--config", f'model_reasoning_effort="{config.CODEX_EFFORT}"',
            "--config", 'default_permissions="studio"',
            "--image", str(contact_sheet),
            "--output-schema", str(schema_path),
            "--output-last-message", str(output_path), "-"]


def run_codex_review(*, contact_sheet, schema_path, output_path, prompt_path,
                     runner_temp, report_script=None,
                     timeout: int = AGENT_TIMEOUT_SEC, env=None) -> None:
    codex_env = dict(os.environ if env is None else env)
    runtime_home = codex_env.get("CODEX_RUNTIME_HOME")
    if runtime_home:
        codex_env["CODEX_HOME"] = runtime_home
    # Reviewer reads labelled JSON + contact sheet only: no raw audio,
    # no GitHub credentials in the model environment.
    codex_env.pop("GH_TOKEN", None)
    codex_env.pop("GITHUB_TOKEN", None)
    rt = Path(runner_temp)
    run_agent_command(
        build_codex_review_command(contact_sheet=contact_sheet,
                                   schema_path=schema_path,
                                   output_path=output_path),
        stdout_path=rt / "codex-repair.stdout",
        stderr_path=rt / "codex-repair.stderr",
        stdin_path=prompt_path, timeout=timeout, env=codex_env,
        report_script=report_script, label="Codex review")


# --- operational result / workflow outputs ----------------------------------

def build_repair_result(loop_result: dict, *, parent: int, child: int) -> dict:
    final_review = loop_result.get("final_review") or {}
    attempted = loop_result.get("repairs_attempted")
    if attempted is None:
        attempted = loop_result.get(
            "repairs_used", max(0, loop_result["attempt_count"] - 1))
    attempted = int(attempted)
    return {
        "schema_version": "1.0",
        "status": loop_result["status"],
        "attempt_count": loop_result["attempt_count"],
        "max_repairs": loop_result["max_repairs"],
        "repairs_attempted": attempted,
        "repair_count": attempted,
        "muse_repair_calls": attempted,
        "repairs_used": attempted,
        "parent_issue": int(parent), "child_issue": int(child),
        "pr_number": loop_result.get("pr_number"),
        "final_pr_number": loop_result.get("pr_number"),
        "head_sha": loop_result.get("head_sha"),
        "final_head_sha": loop_result.get("head_sha"),
        "final_route": gating.review_route(final_review),
        "history": loop_result.get("history", []),
        "next_action": loop_result.get("next_action")
        or next_action_for(loop_result["status"]),
    }


def write_repair_result(work_dir, result: dict) -> Path:
    out = Path(work_dir) / "repair-result.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return out


def append_github_output(result: dict, output_path=None) -> None:
    path = output_path or os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"head_sha={result.get('final_head_sha') or ''}\n")
        f.write(f"pr_number={result.get('pr_number') or ''}\n")
        f.write(f"status={result.get('status') or ''}\n")
        f.write(f"repair_status={result.get('status') or ''}\n")


def append_step_summary(result: dict, summary_path=None) -> None:
    path = summary_path or os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(
            f"Studio repair: status={result.get('status')} "
            f"attempts={result.get('attempt_count')}/"
            f"{int(result.get('max_repairs', 0)) + 1} "
            f"repairs_attempted={result.get('repairs_attempted', result.get('repairs_used'))} "
            f"head={result.get('final_head_sha')} "
            f"pr={result.get('pr_number')} "
            f"next={result.get('next_action')}\n")

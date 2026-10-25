"""Release/review gates as pure functions over GitHub-shaped dicts."""
from __future__ import annotations

from . import config

REVIEW_CHECKS = ("mechanical_qa_reviewed", "brief_adherence",
                 "timing_integrity", "aesthetic_approval")
LISTENING_NOTE = "audio listening unavailable; not a full musical judgement"


REVIEW_KEYS = {"verdict", "reviewed_head_sha", "checks", "blockers",
               "summary", "listening_note"}
REVIEW_KEYS_WITH_REPAIR = REVIEW_KEYS | {"repair"}
REPAIR_ROUTES = ("none", "storyboard", "engine")


def review_route(review) -> str | None:
    """Repair route from a review dict, or None when absent/malformed."""
    if not isinstance(review, dict):
        return None
    repair = review.get("repair")
    if not isinstance(repair, dict):
        return None
    route = repair.get("route")
    return route if route in REPAIR_ROUTES else None


def review_instructions(review) -> list:
    if not isinstance(review, dict):
        return []
    repair = review.get("repair")
    if not isinstance(repair, dict):
        return []
    instr = repair.get("instructions")
    return list(instr) if isinstance(instr, list) else []


def validate_repair_metadata(review, *, require: bool = False) -> list[str]:
    """Strict validation of the optional ``repair`` object.

    Legacy reviews without ``repair`` are accepted (past immutable
    artifacts remain readable). When ``repair`` is present it must be
    exactly ``{route, instructions}`` with strict types and verdict
    consistency: pass -> route ``none`` and no instructions; fail ->
    route ``storyboard``/``engine`` with non-empty actionable
    instructions (max 20, each 1..1000 chars). When ``require`` is true
    (new production reviews) a missing ``repair`` is a failure.
    """
    failures: list[str] = []
    if not isinstance(review, dict):
        return ["missing review result"]
    if "repair" not in review:
        if require:
            failures.append(
                "review missing repair metadata (required for new reviews)")
        return failures
    repair = review.get("repair")
    if not isinstance(repair, dict):
        return failures + ["review repair must be an object"]
    if set(repair) != {"route", "instructions"}:
        failures.append(
            "review repair must have exactly route and instructions")
    route = repair.get("route")
    if route not in REPAIR_ROUTES:
        failures.append("review repair.route must be none/storyboard/engine")
    instr = repair.get("instructions")
    if not isinstance(instr, list):
        failures.append("review repair.instructions must be a list")
    else:
        if len(instr) > 20:
            failures.append("review repair.instructions exceeds 20 items")
        for i, item in enumerate(instr):
            if not isinstance(item, str) or not item.strip() \
                    or len(item) > 1000:
                failures.append(
                    f"review repair.instructions[{i}] must be "
                    "non-empty text <= 1000 chars")
    verdict = review.get("verdict")
    if route in REPAIR_ROUTES and isinstance(instr, list):
        if verdict == "pass":
            if route != "none":
                failures.append("passing review repair.route must be none")
            if instr:
                failures.append(
                    "passing review repair.instructions must be empty")
        elif verdict == "fail":
            if route == "none":
                failures.append(
                    "failing review repair.route must be storyboard or engine")
            if route in ("storyboard", "engine") and not instr:
                failures.append(
                    "failing review repair.instructions must be non-empty")
    return failures


def validate_review_structure(review, *, expected_head_sha: str | None = None,
                              require_repair: bool = False) -> dict:
    """Structural review validation (no pass/fail gate decision).

    Accepts legacy reviews without ``repair`` and new reviews with a
    strictly validated ``repair`` object. Does NOT require a pass
    verdict, true checks or empty blockers: a structurally valid fail
    is a legitimate review outcome for the repair loop to route on.
    """
    failures: list[str] = []
    if not isinstance(review, dict):
        return {"ok": False, "failures": ["missing review result"]}
    extra = set(review) - REVIEW_KEYS_WITH_REPAIR
    if extra:
        failures.append(f"review has unexpected keys: {sorted(extra)}")
    missing = REVIEW_KEYS - set(review)
    if missing:
        failures.append(f"review missing keys: {sorted(missing)}")
    if review.get("verdict") not in ("pass", "fail"):
        failures.append("review verdict must be pass/fail")
    checks = review.get("checks")
    if not isinstance(checks, dict):
        failures.append("review checks must be an object")
    else:
        if set(checks) != set(REVIEW_CHECKS):
            failures.append(
                f"review checks keys {sorted(checks)} != {sorted(REVIEW_CHECKS)}")
        for key in REVIEW_CHECKS:
            if key in checks and not isinstance(checks.get(key), bool):
                failures.append(f"review check must be a boolean: {key}")
    blockers = review.get("blockers")
    if not isinstance(blockers, list) \
            or any(not isinstance(b, str) for b in blockers or []):
        failures.append("review blockers must be a list of strings")
    elif len(blockers) > 20:
        failures.append("review blockers exceeds 20 items")
    elif any(len(b) > 1000 for b in blockers):
        failures.append("review blockers items must be <= 1000 chars")
    summary = review.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 4000:
        failures.append("review summary must be non-empty text")
    if review.get("listening_note") != LISTENING_NOTE:
        failures.append("review listening_note missing or incorrect")
    rhs = review.get("reviewed_head_sha")
    if not (isinstance(rhs, str) and len(rhs) == 40
            and all(c in "0123456789abcdef" for c in rhs)):
        failures.append("review reviewed_head_sha must be a 40-hex sha")
    elif expected_head_sha is not None and rhs != expected_head_sha:
        failures.append("reviewed_head_sha does not match expected head SHA")
    failures.extend(validate_repair_metadata(review, require=require_repair))
    return {"ok": not failures, "failures": failures}


def evaluate_review_gate(review: dict | None, *, mechanical_pass: bool,
                         expected_head_sha: str | None = None,
                         require_repair: bool = False) -> dict:
    """Codex review gate: strict structure (legacy reviews without
    ``repair`` remain readable; a present ``repair`` is strictly
    validated), ALL four checks boolean true (including
    aesthetic_approval), no blockers, exact listening_note, non-empty
    summary, a 40-hex reviewed_head_sha binding the exact head when
    expected is given, and mechanical_pass exactly True. With
    ``require_repair`` a missing ``repair`` object also fails."""
    if not isinstance(review, dict):
        return {"ok": False, "failures": ["missing review result"]}
    failures: list[str] = list(validate_review_structure(
        review, expected_head_sha=expected_head_sha,
        require_repair=require_repair)["failures"])
    if review.get("verdict") in ("pass", "fail") \
            and review.get("verdict") != "pass":
        failures.append("AI review verdict is not pass")
    checks = review.get("checks")
    if isinstance(checks, dict):
        for key in REVIEW_CHECKS:
            if checks.get(key) is not True:
                failures.append(f"review check not true: {key}")
    blockers = review.get("blockers")
    if isinstance(blockers, list) and blockers:
        failures.append("review has blockers")
    if mechanical_pass is not True:
        failures.append("mechanical QA did not pass")
    # De-duplicate preserving order (structural + gate overlap).
    seen: set[str] = set()
    unique: list[str] = []
    for f in failures:
        if f not in seen:
            seen.add(f)
            unique.append(f)
    return {"ok": not unique, "failures": unique}


def _full_name(v) -> str | None:
    if isinstance(v, dict):
        fn = v.get("full_name")
        return fn if isinstance(fn, str) and fn else None
    return v if isinstance(v, str) and v else None


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_sha(v, n: int) -> bool:
    return (isinstance(v, str) and len(v) == n
            and all(c in "0123456789abcdef" for c in v))


def evaluate_run_provenance(run: dict | None, artifact: dict | None, *,
                            pr_number: int, head_sha: str,
                            expected_workflow_id: int | None = None,
                            repo: str | None = None,
                            child: int | None = None,
                            expected_run_attempt: int | None = None) -> dict:
    """Authentic successful studio-production run on main for this PR/head.

    Verifies status/conclusion, trusted-main head branch, exact workflow
    path (and workflow id when known), same-repo evidence, and that the
    artifact belongs to the run (actual ``workflow_run.id``), is
    unexpired, and its name is EXACTLY
    ``studio-child-CHILD-pr-PR-FULLHEAD-run-RUN-aATTEMPT``.

    Fail-closed for the real controller: when ``repo`` or
    ``expected_workflow_id`` is supplied (the controller always supplies
    both) run head_sha / run_attempt / repository fields must be
    present and exact — an absent field never passes. Direct pure
    helper calls without those expectations keep the structural checks
    (exact name pattern, expired-is-False, run belonging).
    """
    import re as _re
    failures: list[str] = []
    if not isinstance(run, dict):
        return {"ok": False, "failures": ["missing workflow run"]}
    strict = repo is not None or expected_workflow_id is not None
    if not _is_int(run.get("id")):
        failures.append("run id must be an integer")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        failures.append("run is not completed/success")
    if run.get("head_branch") != config.DEFAULT_BRANCH:
        failures.append("run head_branch must be main")
    if run.get("event") not in ("issues", "workflow_dispatch"):
        failures.append("run event must be issues/workflow_dispatch")
    if run.get("path") != config.WORKFLOW_PATH:
        failures.append("run workflow is not studio-production.yml")
    if expected_workflow_id is not None:
        if not _is_int(expected_workflow_id) or run.get("workflow_id") != expected_workflow_id:
            failures.append("run workflow_id does not match studio-production.yml")
    if strict:
        if not _is_sha(run.get("head_sha"), 40):
            failures.append("run head_sha must be a 40-hex sha")
        if not _is_int(run.get("run_attempt")) or run.get("run_attempt", 0) < 1:
            failures.append("run run_attempt must be a positive integer")
    if repo is not None:
        if _full_name(run.get("head_repository")) != repo:
            failures.append("run head repository is not this repository")
        if _full_name(run.get("repository")) != repo:
            failures.append("run repository mismatch")
    attempt = expected_run_attempt
    if attempt is None and _is_int(run.get("run_attempt")):
        attempt = run.get("run_attempt")
    if expected_run_attempt is not None and run.get("run_attempt") != expected_run_attempt:
        failures.append("run run_attempt does not match expected attempt")
    if not isinstance(artifact, dict):
        failures.append("missing artifact")
    else:
        wr = artifact.get("workflow_run")
        wr_id = wr.get("id") if isinstance(wr, dict) else artifact.get("workflow_run_id")
        if wr_id != run.get("id"):
            failures.append("artifact does not belong to the run")
        if artifact.get("expired") is not False:
            failures.append("artifact expired")
        name = artifact.get("name")
        if child is not None and attempt is not None and _is_int(run.get("id")):
            want = (f"studio-child-{child}-pr-{pr_number}-{head_sha}"
                    f"-run-{run.get('id')}-a{attempt}")
            if name != want:
                failures.append("artifact name does not exactly bind child/pr/head/run/attempt")
        elif not (isinstance(name, str) and _re.fullmatch(
                rf"studio-child-\d+-pr-{pr_number}-{_re.escape(head_sha)}-run-{run.get('id')}-a\d+",
                name or "")):
            failures.append("artifact name does not bind PR + head SHA")
    return {"ok": not failures, "failures": failures}


def evaluate_release_gate(ctx: dict) -> dict:
    """Owner merge is the human selection; no separate APPROVED review.

    ``ctx`` keys: pr, review, mechanical_pass,
    run, artifact, manifest_ok, bundle_ok (+ optional expected_workflow_id,
    repo, receipt_ok)."""
    failures: list[str] = []
    pr = ctx.get("pr") or {}
    if not isinstance(pr, dict) or pr.get("merged") is not True:
        failures.append("PR is not merged")
    if not isinstance(pr, dict) or pr.get("merged_by") != config.OWNER:
        failures.append("PR must be merged by the repository owner")
    if not isinstance(pr, dict) or pr.get("base") != config.DEFAULT_BRANCH:
        failures.append("PR base must be main")
    repo = ctx.get("repo")
    if repo is not None:
        if not isinstance(pr, dict) or pr.get("head_repo") != repo:
            failures.append("PR head repository is not this repository")
        if not isinstance(pr, dict) or pr.get("base_repo") != repo:
            failures.append("PR base repository is not this repository")
    head = pr.get("head_sha", "") if isinstance(pr, dict) else ""
    if not _is_sha(head, 40):
        failures.append("PR head_sha must be a 40-hex sha")
    pr_number = pr.get("number") if isinstance(pr, dict) else None
    if not _is_int(pr_number):
        failures.append("PR number must be an integer")
        pr_number = 0
    rg = evaluate_review_gate(ctx.get("review"), mechanical_pass=ctx.get("mechanical_pass"),
                              expected_head_sha=head or None)
    failures.extend(f"review: {f}" for f in rg["failures"])
    prov = evaluate_run_provenance(ctx.get("run"), ctx.get("artifact"),
                                   pr_number=int(pr_number),
                                   head_sha=head,
                                   expected_workflow_id=ctx.get("expected_workflow_id"),
                                   repo=repo,
                                   child=ctx.get("child"),
                                   expected_run_attempt=ctx.get("expected_run_attempt"))
    failures.extend(f"provenance: {f}" for f in prov["failures"])
    if ctx.get("receipt_ok") is not True:
        failures.append("artifact receipt does not bind pr/child/head/run")
    if ctx.get("manifest_ok") is not True:
        failures.append("artifact hash manifest mismatch")
    if ctx.get("bundle_ok") is not True:
        failures.append("production bundle blob hashes do not match head/merged result")
    return {"ok": not failures, "failures": failures}


def _closed_by_login(child: dict) -> str | None:
    login = child.get("closed_by")
    if isinstance(login, dict):
        return login.get("login")
    if isinstance(login, str) and login:
        return login
    # Legacy test shape: explicit boolean recorded by the controller.
    if child.get("closed_by_owner") is True:
        return config.OWNER
    return None


def child_is_terminal(child: dict) -> bool:
    """Released, or explicitly dismissed by the owner as not_planned.

    ``closed_by`` must be the owner's login (GitHub ``closed_by.login``);
    an arbitrary closed child never counts.
    """
    if child.get("released"):
        return True
    return (child.get("state") == "closed"
            and child.get("state_reason") == "not_planned"
            and _closed_by_login(child) == config.OWNER)


def should_close_parent(children: list[dict]) -> bool:
    return bool(children) and all(child_is_terminal(c) for c in children)

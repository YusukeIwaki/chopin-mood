"""Release/review gates as pure functions over GitHub-shaped dicts."""
from __future__ import annotations

from . import config

_SUBSTANTIVE = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
REVIEW_CHECKS = ("mechanical_qa_reviewed", "brief_adherence",
                 "timing_integrity", "aesthetic_approval")
LISTENING_NOTE = "audio listening unavailable; not a full musical judgement"


def evaluate_approval(reviews: list[dict], *, head_sha: str,
                      owner: str = config.OWNER) -> dict:
    """Latest substantive OWNER review wins.

    Only the owner's substantive reviews decide: other people's
    approvals, changes-requested or dismissals (and anyone's COMMENT)
    can neither create nor erase the owner's approval. Valid only
    when the owner's latest substantive review is APPROVED at exactly
    ``head_sha``; an owner CHANGES_REQUESTED after an approval rejects.
    """
    failures: list[str] = []
    if not isinstance(reviews, list):
        return {"ok": False, "failures": ["reviews must be a list (no approval)"]}
    owner_sub = [r for r in reviews
                 if isinstance(r, dict) and r.get("user") == owner
                 and r.get("state") in _SUBSTANTIVE]
    if not owner_sub:
        return {"ok": False, "failures": [
            "no substantive owner review (approver must be the repository owner)"]}
    latest = owner_sub[-1]
    if latest.get("state") == "CHANGES_REQUESTED":
        failures.append("latest substantive owner review is CHANGES_REQUESTED")
    elif latest.get("state") == "DISMISSED":
        failures.append("latest substantive owner review is DISMISSED")
    elif latest.get("state") != "APPROVED":
        failures.append(f"latest substantive owner review is {latest.get('state')}")
    if latest.get("commit_id") != head_sha:
        failures.append("approval commit_id does not match head SHA (stale approval)")
    return {"ok": not failures, "failures": failures, "latest": latest}


REVIEW_KEYS = {"verdict", "reviewed_head_sha", "checks", "blockers",
               "summary", "listening_note"}


def evaluate_review_gate(review: dict | None, *, mechanical_pass: bool,
                         expected_head_sha: str | None = None) -> dict:
    """Codex review, strict keys/types: exactly the schema keys, ALL
    four checks boolean true (including aesthetic_approval), no
    blockers, exact listening_note, non-empty summary, a 40-hex
    reviewed_head_sha binding the exact head when expected is given,
    and mechanical_pass exactly True."""
    failures: list[str] = []
    if not isinstance(review, dict):
        return {"ok": False, "failures": ["missing review result"]}
    extra = set(review) - REVIEW_KEYS
    if extra:
        failures.append(f"review has unexpected keys: {sorted(extra)}")
    missing = REVIEW_KEYS - set(review)
    if missing:
        failures.append(f"review missing keys: {sorted(missing)}")
    if review.get("verdict") not in ("pass", "fail"):
        failures.append("review verdict must be pass/fail")
    elif review.get("verdict") != "pass":
        failures.append("AI review verdict is not pass")
    checks = review.get("checks")
    if not isinstance(checks, dict):
        failures.append("review checks must be an object")
    else:
        if set(checks) != set(REVIEW_CHECKS):
            failures.append(
                f"review checks keys {sorted(checks)} != {sorted(REVIEW_CHECKS)}")
        for key in REVIEW_CHECKS:
            if checks.get(key) is not True:
                failures.append(f"review check not true: {key}")
    blockers = review.get("blockers")
    if not isinstance(blockers, list) or any(not isinstance(b, str) for b in blockers or []):
        failures.append("review blockers must be a list of strings")
    elif blockers:
        failures.append("review has blockers")
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
    if mechanical_pass is not True:
        failures.append("mechanical QA did not pass")
    return {"ok": not failures, "failures": failures}


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
    """Full release gate. ``ctx`` keys: pr, reviews, review, mechanical_pass,
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
    appr = evaluate_approval(ctx.get("reviews") or [], head_sha=head)
    failures.extend(f"approval: {f}" for f in appr["failures"])
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

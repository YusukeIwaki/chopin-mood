"""Release one directly owned Issue candidate after the owner's merge.

The trusted Actions receipt binds immutable artifacts to the exact PR inputs.
The source-level release tag is a selection lock; a different candidate never
replaces it. No Issue or competing PR closes until publication is confirmed.
Legacy child-Issue releases remain in :mod:`studio.controller`.
"""
from __future__ import annotations

import base64
import json
import re

from . import artifacts, config, controller, fingerprint, gating, validate
from .github_client import GitHubError

WORKFLOW_PATH = ".github/workflows/studio-candidate.yml"
INPUT_NAMES = ("project.json", "brief.json", "storyboard.json", "editorial.json")
BUNDLE_FILES = set(config.EXPECTED_ARTIFACT_FILES) | {"project.json", "editorial.json"}
BRANCH_RE = re.compile(r"codex/studio-issue-([1-9][0-9]*)-(cand-0[123])")
_RECEIPT_RE = re.compile(
    r"<!-- studio:candidate-receipt pr=([1-9][0-9]*) head=([0-9a-f]{40}) "
    r"run=([1-9][0-9]*) attempt=([1-9][0-9]*) artifact=([1-9][0-9]*) -->")
RECEIPT_KEYS = {"schema_version", "source_issue", "candidate_id", "pr", "head_sha",
                "engine_sha", "run_id", "run_attempt", "input_sha256", "source_hashes"}


def receipt_marker(*, pr: int, head_sha: str, run_id: int,
                   run_attempt: int, artifact_id: int) -> str:
    return (f"<!-- studio:candidate-receipt pr={pr} head={head_sha} "
            f"run={run_id} attempt={run_attempt} artifact={artifact_id} -->")


def artifact_name(*, pr: int, head_sha: str, run_id: int, run_attempt: int) -> str:
    return f"studio-candidate-pr-{pr}-{head_sha}-run-{run_id}-a{run_attempt}"


def _fail(*messages: str) -> dict:
    return {"ok": False, "failures": list(messages)}


def _sha(value, length=40) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(rf"[0-9a-f]{{{length}}}", value))


def _login(value) -> str | None:
    return value.get("login") if isinstance(value, dict) else None


def _repo(value) -> str | None:
    return value.get("full_name") if isinstance(value, dict) else None


def _identity(pr: dict, repo: str):
    head, base = pr.get("head") or {}, pr.get("base") or {}
    match = BRANCH_RE.fullmatch(head.get("ref") or "")
    if (not match or _repo(head.get("repo")) != repo
            or _repo(base.get("repo")) != repo or base.get("ref") != config.DEFAULT_BRANCH
            or not _sha(head.get("sha"))):
        return None
    return int(match[1]), match[2]


def _input_path(issue: int, candidate: str, name: str) -> str:
    return f"studio/projects/issue-{issue}/candidates/{candidate}/{name}"


def _load_inputs(gh, issue: int, candidate: str, ref: str) -> dict[str, bytes]:
    raw = {}
    for name in INPUT_NAMES:
        path = _input_path(issue, candidate, name)
        blob = gh.request("GET", f"/contents/{path}?ref={ref}")
        if blob.get("type") != "file" or blob.get("encoding") != "base64":
            raise ValueError(f"candidate input is not a regular base64 file: {name}")
        encoded = blob.get("content")
        if not isinstance(encoded, str) or len(encoded) > config.MAX_PRODUCTION_FILE_BYTES * 2:
            raise ValueError(f"candidate input exceeds size bound: {name}")
        value = base64.b64decode("".join(encoded.split()), validate=True)
        if len(value) > config.MAX_PRODUCTION_FILE_BYTES:
            raise ValueError(f"candidate input exceeds size bound: {name}")
        raw[name] = value
    return raw


def _check_changed_paths(gh, pr_number: int, issue: int, candidate: str) -> None:
    expected = {_input_path(issue, candidate, n) for n in INPUT_NAMES}
    changes = gh.paginated(f"/pulls/{pr_number}/files")
    if not changes or any(c.get("filename") not in expected
                          or c.get("status") in {"removed", "renamed"}
                          for c in changes):
        raise ValueError("candidate PR changes files outside its four input JSON files")


def _find_receipt(gh, number: int, head: str):
    matches = []
    for comment in gh.paginated(f"/issues/{number}/comments"):
        if _login(comment.get("user")) not in config.BOT_LOGINS:
            continue
        for match in _RECEIPT_RE.finditer(comment.get("body") or ""):
            if int(match[1]) == number and match[2] == head:
                matches.append({"pr": number, "head_sha": head, "run_id": int(match[3]),
                                "run_attempt": int(match[4]), "artifact_id": int(match[5])})
    return matches[-1] if matches else None


def _run_failures(run, artifact, binding, workflow_id, repo, project) -> list[str]:
    failures = []
    if type(workflow_id) is not int or workflow_id < 1:
        failures.append("candidate workflow id is invalid")
    required = {
        "id": binding["run_id"], "run_attempt": binding["run_attempt"],
        "status": "completed", "conclusion": "success", "event": "workflow_dispatch",
        "head_branch": config.DEFAULT_BRANCH, "path": WORKFLOW_PATH,
        "workflow_id": workflow_id, "head_sha": project["engine_sha"],
    }
    for key, want in required.items():
        if run.get(key) != want:
            failures.append(f"candidate run {key} mismatch")
    for key in ("actor", "triggering_actor"):
        if _login(run.get(key)) != config.OWNER:
            failures.append(f"candidate run {key} must be owner")
    for key in ("repository", "head_repository"):
        if _repo(run.get(key)) != repo:
            failures.append(f"candidate run {key} mismatch")
    if artifact.get("id") != binding["artifact_id"]:
        failures.append("artifact id mismatch")
    if artifact.get("expired") is not False:
        failures.append("artifact is expired")
    if (artifact.get("workflow_run") or {}).get("id") != binding["run_id"]:
        failures.append("artifact does not belong to candidate run")
    if artifact.get("name") != artifact_name(**{k: binding[k] for k in (
            "pr", "head_sha", "run_id", "run_attempt")}):
        failures.append("artifact name does not bind exact PR/head/run/attempt")
    return failures


def _selection_marker(issue: int, candidate: str, pr: int, head: str, merge: str) -> str:
    return (f"<!-- studio:selection issue={issue} candidate={candidate} "
            f"pr={pr} head={head} merge={merge} -->")


def _release_assets(gh, *, files, issue, candidate, pr_number, head, merge, stage_only=False):
    """Idempotent draft upload/publish; never repair a published release."""
    tag = f"studio-issue-{issue}"
    marker = _selection_marker(issue, candidate, pr_number, head, merge)
    exists, target = controller._resolve_tag(gh, tag)
    if exists and target != merge:
        return _fail("another candidate already owns this source Issue release tag")
    matches = [r for r in gh.paginated("/releases") if r.get("tag_name") == tag]
    if len(matches) > 1:
        return _fail("multiple releases use this source Issue tag")
    release = matches[0] if matches else None
    if release is not None:
        if release.get("target_commitish") != merge or marker not in (release.get("body") or ""):
            return _fail("existing source Issue release selects a different candidate or head")
        if release.get("draft") is not True and not exists:
            return _fail("published source Issue release tag is missing")
    if release is None:
        release = gh.request("POST", "/releases", {
            "tag_name": tag, "name": f"Issue #{issue} — {candidate}",
            "target_commitish": merge, "draft": True,
            "body": f"Selected by the owner's merge of PR #{pr_number}.\n\n{marker}"})
    existing_list = release.get("assets") or []
    existing = {a["name"]: a for a in existing_list}
    if len(existing) != len(existing_list) or set(existing) - set(files):
        return _fail("existing release has duplicate or unexpected assets")
    published = release.get("draft") is not True
    if published and set(existing) != set(files):
        return _fail("published release is missing assets; published assets are immutable")
    # Verify every existing asset before uploading any missing asset.
    for name, asset in existing.items():
        if controller._asset_digest(asset, gh, int(asset["id"])) != artifacts.sha256_bytes(files[name]):
            return _fail(f"existing release asset digest mismatch: {name}")
    for name, data in sorted(files.items()):
        if name not in existing:
            gh.upload_release_asset(release, name, data)
    release = gh.request("GET", f"/releases/{release['id']}")
    final_list = release.get("assets") or []
    final = {a["name"]: a for a in final_list}
    if len(final) != len(final_list) or set(final) != set(files):
        return _fail("final release asset set mismatch")
    for name, data in files.items():
        if controller._asset_digest(final[name], gh, int(final[name]["id"])) != artifacts.sha256_bytes(data):
            return _fail(f"final release asset digest mismatch: {name}")
    if stage_only:
        return {"ok": True, "tag": tag, "release_id": release["id"], "staged": True}
    from .publication import check_before_publish
    check_before_publish(release)
    if release.get("draft") is True:
        gh.request("PATCH", f"/releases/{release['id']}", {
            "draft": False, "tag_name": tag, "target_commitish": merge})
        release = gh.request("GET", f"/releases/{release['id']}")
    exists, target = controller._resolve_tag(gh, tag)
    if (release.get("draft") is not False or not exists or target != merge
            or release.get("tag_name") != tag
            or release.get("target_commitish") != merge):
        return _fail("release publication/tag confirmation failed; no Issues or PRs closed")
    return {"ok": True, "tag": tag, "release_id": release["id"]}


def _associated_candidate(raw, *, issue: int, candidate: str, source_url: str) -> bool:
    """Relationship identity, deliberately independent of artistic readiness.

    An alternative awaiting a rerender can have a stale editorial snapshot or
    incomplete storyboard. Those are reasons not to release it, not reasons to
    leave its otherwise authenticated competing PR open after a winner exists.
    """
    project, brief = json.loads(raw["project.json"]), json.loads(raw["brief.json"])
    if (not isinstance(project, dict) or set(project) != {
            "schema_version", "source_issue", "candidate_id", "source_url", "engine_sha"}
            or project.get("schema_version") != "2.0" or type(project.get("source_issue")) is not int
            or project.get("source_issue") != issue or project.get("candidate_id") != candidate
            or project.get("source_url") != source_url or not _sha(project.get("engine_sha"))
            or not validate.validate_attachment_url(source_url)["ok"]
            or not isinstance(brief, dict) or brief.get("parent_issue") != issue
            or brief.get("child_issue") is not None or brief.get("candidate_id") != candidate
            or not isinstance(brief.get("candidate"), dict)
            or brief["candidate"].get("id") != candidate):
        return False
    hashes = brief.get("source_hashes")
    if not isinstance(hashes, dict) or set(hashes) != set(artifacts.SOURCE_HASH_KEYS) \
            or not all(_sha(v, 64) for v in hashes.values()):
        return False
    expected = fingerprint.canonical_fingerprint(
        source_url=source_url, source_sha256=hashes["decoded_wav_sha256"],
        piece=brief.get("piece"), period=brief.get("period"), art_notes=brief.get("art_notes"))
    return brief.get("fingerprint") == expected


def _close_competitors(gh, *, issue: int, selected_pr: int, source_url: str) -> list[int]:
    """Close same-source alternatives by authenticated identity, never prose."""
    closed = []
    for listed in gh.paginated("/pulls?state=open&base=main"):
        number = listed.get("number")
        if number == selected_pr:
            continue
        pr = gh.request("GET", f"/pulls/{number}")
        ident = _identity(pr, gh.repo)
        if not ident or ident[0] != issue or pr.get("state") != "open":
            continue
        if _login(pr.get("user")) not in (config.BOT_LOGINS | {config.OWNER}):
            continue
        try:
            _check_changed_paths(gh, number, *ident)
            raw = _load_inputs(gh, *ident, pr["head"]["sha"])
            if not _associated_candidate(raw, issue=issue, candidate=ident[1], source_url=source_url):
                continue
        except GitHubError as exc:
            if exc.status != 404:
                # A temporary API error is not evidence that a rival is unrelated.
                # Keep the source open and resume closure after recovery.
                raise
            continue
        except (ValueError, TypeError, KeyError):
            # An unrelated/malformed PR must never be closed by association.
            continue
        gh.request("PATCH", f"/pulls/{number}", {"state": "closed"})
        closed.append(number)
    return closed


def release_controller(gh, *, pr_number: int, actor: str | None = None,
                       via_dispatch: bool = False, stage_only: bool = False) -> dict:
    """Validate all evidence, publish one winner, then close its source and rivals.

    The owner's merge is the human selection; a separate APPROVED review is
    deliberately unnecessary for v2. Mechanical/editorial/independent visual
    checks and exact-head immutable evidence remain mandatory.
    """
    if via_dispatch and actor != config.OWNER:
        return _fail("manual release dispatcher must be owner")
    try:
        return _release_controller(gh, pr_number=pr_number, stage_only=stage_only)
    except GitHubError as exc:
        return _fail(f"GitHub operation failed ({exc.status}); rerun safely after recovery")
    except (ValueError, TypeError, KeyError) as exc:
        return _fail(f"invalid candidate release evidence: {exc}")


def _release_controller(gh, *, pr_number: int, stage_only: bool = False) -> dict:
    from .candidate_project import validate_candidate_documents

    pr = gh.request("GET", f"/pulls/{pr_number}")
    ident = _identity(pr, gh.repo)
    if ident is None:
        return _fail("PR must be a same-repository candidate branch targeting main")
    issue, candidate = ident
    head, merge = pr["head"]["sha"], pr.get("merge_commit_sha")
    if pr.get("merged") is not True or _login(pr.get("merged_by")) != config.OWNER:
        return _fail("candidate PR must be merged by owner")
    if not _sha(merge):
        return _fail("merged PR must have a valid merge commit")
    _check_changed_paths(gh, pr_number, issue, candidate)
    raw = _load_inputs(gh, issue, candidate, head)
    digests = artifacts.build_hash_manifest(raw)
    merged = _load_inputs(gh, issue, candidate, merge)
    if merged != raw:
        return _fail("candidate input blobs differ between reviewed head and merged result")
    documents = {name: json.loads(value) for name, value in raw.items()}
    validated = validate_candidate_documents(
        documents, input_sha256=digests, issue=issue, candidate_id=candidate,
        gh=gh, require_ready=True, require_open=False)
    # Source Issue intake is owner-only even during a closure retry.
    source = gh.get_issue(issue)
    if _login(source.get("user")) != config.OWNER or "pull_request" in source:
        return _fail("source must be an owner-authored Issue")
    binding = _find_receipt(gh, pr_number, head)
    if binding is None:
        return _fail("no authentic bot candidate receipt for exact merged head")
    run = gh.request("GET", f"/actions/runs/{binding['run_id']}")
    workflow = gh.request("GET", "/actions/workflows/studio-candidate.yml")
    artifact = gh.request("GET", f"/actions/artifacts/{binding['artifact_id']}")
    failures = _run_failures(run, artifact, binding, workflow.get("id"), gh.repo,
                             validated["project"])
    listed_ids = {a.get("id") for a in gh.paginated(
        f"/actions/runs/{binding['run_id']}/artifacts", key="artifacts")}
    if binding["artifact_id"] not in listed_ids:
        failures.append("artifact is absent from candidate run")
    if failures:
        return _fail(*failures)
    archive = gh.download_artifact_bytes(binding["artifact_id"])
    if len(archive) > config.MAX_ARTIFACT_TOTAL_BYTES + 1024 * 1024:
        return _fail("artifact archive exceeds size bound")
    zipped = artifacts.read_and_validate_zip(archive, expected=BUNDLE_FILES)
    if not zipped["ok"]:
        return _fail(*zipped["failures"])
    files = zipped["files"]
    manifest = json.loads(files["hashes.json"])
    if not isinstance(manifest, dict):
        return _fail("artifact manifest must be an object")
    checked = artifacts.verify_hash_manifest(
        manifest, {n: v for n, v in files.items() if n != "hashes.json"})
    failures.extend(checked["failures"])
    if any(files[n] != raw[n] for n in INPUT_NAMES):
        failures.append("artifact inputs do not equal exact PR-head inputs")
    receipt = json.loads(files["receipt.json"])
    expected = {
        "schema_version": "2.0", "source_issue": issue, "candidate_id": candidate,
        "pr": pr_number, "head_sha": head, "engine_sha": run["head_sha"],
        "run_id": binding["run_id"], "run_attempt": binding["run_attempt"],
        "input_sha256": digests, "source_hashes": documents["brief.json"]["source_hashes"],
    }
    if (not isinstance(receipt, dict) or receipt != expected
            or set(receipt) != RECEIPT_KEYS
            or any(type(receipt.get(k)) is not int for k in
                   ("source_issue", "pr", "run_id", "run_attempt"))):
        failures.append("candidate receipt does not bind exact inputs/head/run/source")
    provenance = json.loads(files["provenance.json"])
    failures.extend(artifacts.verify_source_hashes(
        receipt, documents["brief.json"], provenance)["failures"])
    failures.extend(artifacts.verify_media_provenance(provenance, files, manifest)["failures"])
    qa = json.loads(files["qa.json"])
    from .candidate_project import QA_CHECK_NAMES
    checks = qa.get("checks") if isinstance(qa, dict) else None
    if (not isinstance(checks, list) or len(checks) != len(QA_CHECK_NAMES)
            or any(not isinstance(c, dict) or c.get("pass") is not True for c in checks)
            or {c.get("name") for c in checks if isinstance(c, dict)} != QA_CHECK_NAMES):
        failures.append("rendered QA must contain every passing mechanical check")
    if not isinstance(qa, dict):
        qa = {}
    review = json.loads(files["review.json"])
    failures.extend(gating.evaluate_review_gate(
        review, mechanical_pass=qa.get("pass"), expected_head_sha=head,
        require_repair=True)["failures"])
    if failures:
        return _fail(*failures)
    publication = _release_assets(gh, files=files, issue=issue, candidate=candidate,
                                  pr_number=pr_number, head=head, merge=merge,
                                  stage_only=stage_only)
    if not publication["ok"] or stage_only:
        return publication
    closed = _close_competitors(gh, issue=issue, selected_pr=pr_number,
                                source_url=validated["project"]["source_url"])
    # Closing source last makes partial failures clearly resumable.
    if source.get("state") != "closed":
        gh.request("PATCH", f"/issues/{issue}", {"state": "closed", "state_reason": "completed"})
    return {**publication, "source_issue": issue, "candidate_id": candidate,
            "pr": pr_number, "source_closed": True, "closed_competing_prs": closed}

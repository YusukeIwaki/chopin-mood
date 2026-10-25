"""Trusted-main reproduction of a locally authored candidate PR.

Only bounded declarative JSON is read from the PR. No branch code, workflow,
asset path or owner comment is executed. Models never receive the recording.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from . import artifacts, config, gating
from .candidate_project import INPUT_NAMES, QA_CHECK_NAMES, candidate_dir, validate_candidate_documents

BRANCH_RE = re.compile(r"codex/studio-issue-([1-9][0-9]*)-(cand-0[123])")
WORKFLOW_PATH = ".github/workflows/studio-candidate.yml"
BUNDLE_NAMES = config.EXPECTED_ARTIFACT_FILES | {"project.json", "editorial.json"}


def identity(pr: dict, repo: str, *, open_only=True) -> tuple[int, str]:
    head, base = pr.get("head") or {}, pr.get("base") or {}
    match = BRANCH_RE.fullmatch(head.get("ref") or "")
    if not match:
        raise ValueError("not a direct-source candidate branch")
    if ((head.get("repo") or {}).get("full_name") != repo
            or (base.get("repo") or {}).get("full_name") != repo
            or base.get("ref") != config.DEFAULT_BRANCH):
        raise ValueError("candidate must target main in the same repository")
    if open_only and (pr.get("state") != "open" or pr.get("merged")):
        raise ValueError("candidate PR is not open")
    if (pr.get("user") or {}).get("login") not in config.BOT_LOGINS | {config.OWNER}:
        raise ValueError("candidate PR author is not authorized")
    if not re.fullmatch(r"[a-f0-9]{40}", head.get("sha") or ""):
        raise ValueError("invalid candidate head SHA")
    return int(match[1]), match[2]


def read_inputs(gh, *, issue: int, candidate_id: str, ref: str) -> dict[str, bytes]:
    folder = candidate_dir(issue, candidate_id).as_posix()
    result = {}
    for name in INPUT_NAMES:
        value = gh.request("GET", f"/contents/{folder}/{name}?ref={ref}")
        if (value.get("type") != "file" or value.get("encoding") != "base64"
                or type(value.get("size")) is not int
                or not 0 < value["size"] <= config.MAX_PRODUCTION_FILE_BYTES):
            raise ValueError("candidate input must be a bounded regular JSON file")
        try:
            data = base64.b64decode("".join(value["content"].split()), validate=True)
        except (KeyError, ValueError) as e:
            raise ValueError("invalid candidate input encoding") from e
        if len(data) != value["size"]:
            raise ValueError("candidate input size mismatch")
        result[name] = data
    return result


def prepare_candidate(gh, *, pr_number: int, actor: str, engine_sha: str) -> dict:
    if actor != config.OWNER:
        raise ValueError("candidate dispatcher must be the owner")
    pr = gh.request("GET", f"/pulls/{pr_number}")
    issue, cid = identity(pr, gh.repo)
    folder = candidate_dir(issue, cid).as_posix()
    changed = gh.paginated(f"/pulls/{pr_number}/files")
    if not changed or any(f.get("filename") not in {f"{folder}/{n}" for n in INPUT_NAMES}
                          or f.get("status") not in {"added", "modified"} for f in changed):
        raise ValueError("candidate PR may change only its four production inputs")
    for sibling in gh.paginated("/pulls?state=open"):
        if sibling.get("number") == pr_number:
            continue
        if (sibling.get("head") or {}).get("ref") == (pr["head"]["ref"]):
            raise ValueError("duplicate open candidate PR")
    if gh.get_or_none(f"/releases/tags/studio-issue-{issue}") is not None:
        raise ValueError("source issue already has a selected release")
    raw = read_inputs(gh, issue=issue, candidate_id=cid, ref=pr["head"]["sha"])
    documents = {n: json.loads(data) for n, data in raw.items()}
    digests = {n: artifacts.sha256_bytes(data) for n, data in raw.items()}
    checked = validate_candidate_documents(documents, input_sha256=digests,
        issue=issue, candidate_id=cid, gh=gh, require_ready=True)
    if documents["project.json"]["engine_sha"] != engine_sha:
        raise ValueError("local evidence engine differs from this main revision; revalidate locally")
    return {"pr": pr_number, "head_sha": pr["head"]["sha"],
            "source_issue": issue, "candidate_id": cid, "engine_sha": engine_sha,
            "input_sha256": digests, "raw": raw, "checked": checked}


def write_prepared(prepared: dict, work: Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    inputs = work / "inputs"
    inputs.mkdir(exist_ok=True)
    for name, data in prepared["raw"].items():
        (inputs / name).write_bytes(data)
        (work / name).write_bytes(data)
    brief = json.loads(prepared["raw"]["brief.json"])
    for name, value in {"candidate": brief["candidate"],
                        "scenes": {"scenes": brief["candidate"]["scenes"]}}.items():
        (work / f"{name}.json").write_text(json.dumps(value, indent=2))
    context = {k: v for k, v in prepared.items() if k not in {"raw", "checked"}}
    (work / "context.json").write_text(json.dumps(context, indent=2))
    return context


def verify_candidate_guard(gh, context: dict) -> None:
    """Recheck live input identity before model calls, uploads and comments."""
    pr = gh.request("GET", f"/pulls/{context['pr']}")
    issue, cid = identity(pr, gh.repo)
    if (issue != context["source_issue"] or cid != context["candidate_id"]
            or pr["head"]["sha"] != context["head_sha"]):
        raise ValueError("candidate PR/head changed during reproduction")
    raw = read_inputs(gh, issue=issue, candidate_id=cid, ref=context["head_sha"])
    hashes = artifacts.build_hash_manifest(raw)
    if hashes != context["input_sha256"]:
        raise ValueError("candidate input changed during reproduction")
    documents = {name: json.loads(value) for name, value in raw.items()}
    validate_candidate_documents(documents, input_sha256=hashes,
        issue=issue, candidate_id=cid, gh=gh, require_ready=True)
    if documents["project.json"]["engine_sha"] != context["engine_sha"]:
        raise ValueError("candidate engine changed during reproduction")


def seal_candidate(work: Path, *, run_id: int, run_attempt: int) -> dict:
    """Bind the reproduced bytes to raw PR inputs, before final review."""
    ctx = json.loads((work / "context.json").read_text())
    bundle = work / "bundle"
    for name in INPUT_NAMES:
        data = (work / "inputs" / name).read_bytes()
        if artifacts.sha256_bytes(data) != ctx["input_sha256"][name]:
            raise ValueError("candidate input changed during reproduction")
        (bundle / name).write_bytes(data)
    brief = json.loads((bundle / "brief.json").read_text())
    receipt = {"schema_version": "2.0", **ctx, "run_id": run_id,
               "run_attempt": run_attempt, "source_hashes": brief["source_hashes"]}
    (bundle / "receipt.json").write_text(json.dumps(receipt, indent=2))
    refresh_manifest(bundle)
    return receipt


def refresh_manifest(bundle: Path) -> None:
    files = {p.name: p.read_bytes() for p in bundle.iterdir() if p.is_file() and p.name != "hashes.json"}
    (bundle / "hashes.json").write_text(json.dumps(artifacts.build_hash_manifest(files), indent=2))


def validate_bundle(bundle: Path) -> dict:
    if {p.name for p in bundle.iterdir()} != BUNDLE_NAMES:
        raise ValueError("candidate artifact allowlist mismatch")
    if any(p.is_symlink() or not p.is_file() for p in bundle.iterdir()):
        raise ValueError("candidate artifact contains a non-regular file")
    files = {p.name: p.read_bytes() for p in bundle.iterdir()}
    if any(len(v) > config.MAX_ARTIFACT_FILE_BYTES for v in files.values()) or sum(map(len, files.values())) > config.MAX_ARTIFACT_TOTAL_BYTES:
        raise ValueError("candidate artifact exceeds size limits")
    manifest = json.loads(files["hashes.json"])
    checks = [artifacts.verify_hash_manifest(manifest, {n: data for n, data in files.items() if n != "hashes.json"}),
              artifacts.verify_media_provenance(json.loads(files["provenance.json"]), files, manifest)]
    if any(not r["ok"] for r in checks):
        raise ValueError("candidate artifact hash or media binding failed")
    receipt = json.loads(files["receipt.json"])
    for name in INPUT_NAMES:
        if artifacts.sha256_bytes(files[name]) != receipt["input_sha256"][name]:
            raise ValueError("candidate artifact input changed")
    if not artifacts.verify_source_hashes(receipt, json.loads(files["brief.json"]),
                                         json.loads(files["provenance.json"]))["ok"]:
        raise ValueError("candidate artifact source binding failed")
    qa = json.loads(files["qa.json"])
    checks = qa.get("checks") if isinstance(qa, dict) else None
    if (not isinstance(checks, list) or len(checks) != len(QA_CHECK_NAMES)
            or any(not isinstance(c, dict) or type(c.get("pass")) is not bool for c in checks)
            or {c.get("name") for c in checks} != QA_CHECK_NAMES
            or type(qa.get("pass")) is not bool
            or qa["pass"] != all(c["pass"] for c in checks)):
        raise ValueError("candidate artifact mechanical QA is incomplete or inconsistent")
    review = json.loads(files["review.json"])
    check = gating.validate_review_structure(review, expected_head_sha=receipt["head_sha"], require_repair=True)
    if not check["ok"]:
        raise ValueError("invalid candidate final review")
    return {"receipt": receipt, "review": review, "qa": qa}


def publish_candidate(gh, *, work: Path, artifact_id: int, artifact_url: str) -> dict:
    from .candidate_release import receipt_marker
    result = validate_bundle(work / "bundle")
    receipt = result["receipt"]
    verify_candidate_guard(gh, receipt)
    prnum = receipt["pr"]
    pr = gh.request("GET", f"/pulls/{prnum}")
    issue, cid = identity(pr, gh.repo)
    if (pr["head"]["sha"] != receipt["head_sha"] or issue != receipt["source_issue"]
            or cid != receipt["candidate_id"]):
        raise ValueError("candidate PR changed while reproducing; no notification")
    gate = gating.evaluate_review_gate(result["review"], mechanical_pass=result["qa"].get("pass"),
        expected_head_sha=receipt["head_sha"], require_repair=True)
    marker = receipt_marker(pr=prnum, head_sha=receipt["head_sha"], run_id=receipt["run_id"],
        run_attempt=receipt["run_attempt"], artifact_id=artifact_id)
    comments = gh.paginated(f"/issues/{prnum}/comments")
    seen = any(marker in (c.get("body") or "") and (c.get("user") or {}).get("login") in config.BOT_LOGINS for c in comments)
    if not seen:
        body = (f"候補 {cid} / Issue #{issue}\n\n"
                f"[動画・検証Artifact]({artifact_url})\n\n"
                f"機械QA: {result['qa'].get('pass')}; 独立映像レビュー: {result['review']['verdict']}\n\n"
                f"{sanitize_review_text(result['review']['summary'])}\n\n"
                "音声聴取は未実施です。音楽的自然さの確定評価ではありません。\n\n")
        if gate["ok"]:
            body += "本人のMergeでこの案を採用します。公開成功後に元Issueと他候補PRを閉じます。\n\n"
        else:
            body += "公開不可。同じ制作Codexセッションで以下を修正してください。\n\n" + "\n".join(f"- {sanitize_review_text(s)}" for s in result["review"]["blockers"]) + "\n\n"
        # Only validated successful evidence becomes a release receipt.
        body += marker if gate["ok"] else marker.replace("candidate-receipt", "candidate-failed")
        gh.request("POST", f"/issues/{prnum}/comments", {"body": body})
    if gate["ok"]:
        # Independent retry units: a failure after the receipt must not lose the ping/assignee.
        gh.assign(prnum, config.OWNER)
        reviews = gh.paginated(f"/pulls/{prnum}/reviews")
        if not any(marker in (r.get("body") or "") and (r.get("user") or {}).get("login") in config.BOT_LOGINS for r in reviews):
            gh.review_comment(prnum, f"@{config.OWNER} please check this\n\n{marker}", commit_id=receipt["head_sha"])
    return {"ok": gate["ok"], "failures": gate["failures"], "pr": prnum}


def sanitize_review_text(text):
    from .controller import sanitize_comment_text
    return sanitize_comment_text(text)

"""Load a Codex-authored project; no model calls or issue creation.

Existing child IDs are retained solely to keep production/release receipts
compatible. They are not a request to decompose the creative work again.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, creative, fingerprint, storyboard, validate


def load_project(root: Path, issue: int, source_url: str, gh=None) -> dict:
    if type(issue) is not int or issue < 1:
        raise ValueError("source issue must be a positive integer")
    directory = root / f"issue-{issue}"
    names = ("project.json", "brief.json", "storyboard.json")
    for name in names:
        path = directory / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("project paths cannot traverse symlinks")
        if not path.is_file() or path.stat().st_size > config.MAX_PRODUCTION_FILE_BYTES:
            raise ValueError(f"missing/oversized Codex project file: {name}")
    project, brief, sb = [json.loads((directory / n).read_text()) for n in names]
    if set(project) != {"schema_version", "parent_issue", "child_issue", "source_url"}:
        raise ValueError("unexpected project metadata")
    if project["schema_version"] != "1.0" or project["parent_issue"] != issue:
        raise ValueError("project identity mismatch")
    if not validate.validate_attachment_url(source_url)["ok"] or project["source_url"] != source_url:
        raise ValueError("source attachment changed")
    child = project["child_issue"]
    if type(child) is not int or child < 1 or child == issue:
        raise ValueError("an existing production issue is required for receipt compatibility")
    if brief.get("parent_issue") != issue or brief.get("child_issue") != child:
        raise ValueError("brief issue binding mismatch")
    candidate = brief["candidate"]
    if candidate.get("id") != brief.get("candidate_id"):
        raise ValueError("candidate identity mismatch")
    hashes = brief["source_hashes"]
    for name in ("attachment_sha256", "decoded_wav_sha256"):
        if not re.fullmatch(r"[a-f0-9]{64}", str(hashes.get(name, ""))):
            raise ValueError("source hash required")
    expected_fp = fingerprint.canonical_fingerprint(
        source_url=source_url, source_sha256=hashes["decoded_wav_sha256"],
        piece=brief.get("piece"), period=brief.get("period"), art_notes=brief.get("art_notes"))
    if expected_fp != brief.get("fingerprint"):
        raise ValueError("project fingerprint mismatch")
    checked = storyboard.validate_storyboard(sb, candidate["scenes"])
    if not checked["ok"]:
        raise ValueError("invalid storyboard: " + "; ".join(checked["failures"]))
    if gh is not None:
        parent = gh.get_issue(issue)
        if parent.get("state") != "open" or parent.get("user", {}).get("login") != config.OWNER:
            raise ValueError("source issue must be open and owner-authored")
        urls = [url for url in validate.extract_attachment_urls(parent.get("body") or "")
                if validate.validate_attachment_url(url)["ok"]]
        if urls != [source_url]:
            raise ValueError("live source attachment changed")
        fields = creative.parse_issue_body(parent.get("body"))
        if any((fields.get(k) or "") != (brief.get(k) or "")
               for k in ("piece", "period", "art_notes")):
            raise ValueError("live creative direction changed; update the project")
        target = gh.get_issue(child)
        if target.get("state") != "open" or target.get("user", {}).get("login") not in config.BOT_LOGINS:
            raise ValueError("production issue must be an existing open bot issue")
        markers = fingerprint.parse_child_markers(target.get("body") or "")
        if not any(m == {"parent": issue, "candidate_id": candidate["id"], "fingerprint": expected_fp}
                   for m in markers):
            raise ValueError("production issue marker mismatch")
        if child not in gh.get_sub_issue_numbers(issue):
            raise ValueError("production issue is not linked to the source")
    return {"project": project, "brief": brief, "candidate": candidate,
            "scenes": {"scenes": candidate["scenes"]}, "storyboard": sb,
            "source-meta": hashes,
            "matrix": {"include": [{"child_issue": child, "candidate_id": candidate["id"]}]}}

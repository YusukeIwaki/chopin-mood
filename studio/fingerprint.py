"""Canonical fingerprint + hidden HTML-comment markers (idempotency).

Markers live in issue/PR bodies. They are verified together with bot
authorship and real sub-issue relationships by callers; user-editable
text alone never proves provenance.
"""
from __future__ import annotations

import hashlib
import json
import re


def canonical_fingerprint(*, source_url: str, source_sha256: str,
                          piece: str = "", period: str = "",
                          art_notes: str = "") -> str:
    payload = {
        "source_url": source_url.strip(),
        "source_sha256": source_sha256.strip().lower(),
        "piece": (piece or "").strip(),
        "period": (period or "").strip().lower(),
        "art_notes": (art_notes or "").strip(),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def fingerprint_marker(fp: str) -> str:
    return f"<!-- studio:fingerprint:{fp} -->"


def child_marker(parent: int, candidate_id: str, fp: str) -> str:
    return f"<!-- studio:child parent={parent} candidate={candidate_id} fp={fp} -->"


def receipt_marker(*, run_id: int, artifact_id: int, pr: int, head_sha: str,
                   child: int) -> str:
    return (f"<!-- studio:receipt run={run_id} artifact={artifact_id} "
            f"pr={pr} head={head_sha} child={child} -->")


_CHILD_RE = re.compile(
    r"<!-- studio:child parent=(\d+) candidate=([A-Za-z0-9_-]+) fp=([0-9a-f]{64}) -->")
_FP_RE = re.compile(r"<!-- studio:fingerprint:([0-9a-f]{64}) -->")
_RECEIPT_RE = re.compile(
    r"<!-- studio:receipt run=(\d+) artifact=(\d+) pr=(\d+) "
    r"head=([0-9a-f]{40}) child=(\d+) -->")


def parse_fingerprint(body: str | None) -> str | None:
    m = _FP_RE.search(body or "")
    return m.group(1) if m else None


def parse_child_markers(body: str | None) -> list[dict]:
    out = []
    for m in _CHILD_RE.finditer(body or ""):
        out.append({"parent": int(m.group(1)), "candidate_id": m.group(2),
                    "fingerprint": m.group(3)})
    return out


def parse_receipt(body: str | None) -> dict | None:
    m = _RECEIPT_RE.search(body or "")
    if not m:
        return None
    return {"run_id": int(m.group(1)), "artifact_id": int(m.group(2)),
            "pr": int(m.group(3)), "head_sha": m.group(4),
            "child": int(m.group(5))}


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def briefs_hash(briefs: dict) -> str:
    """Hash of the candidate-specific creative briefs (canonical JSON)."""
    return sha256_text(canonical_json(briefs or {}))


# --- authentic planning record (bot comment on the parent issue) --------
# The full validated planning record (selection + briefs + creative) is
# persisted in a bot-authored comment so retries rehydrate the ORIGINAL
# briefs instead of replanning under the same PRs. User text containing
# the marker never counts: callers must check bot authorship first.
_PLAN_RE = re.compile(r"<!-- studio:plan fp=([0-9a-f]{64}) briefs=([0-9a-f]{64}) -->")


def planning_marker(fp: str, bhash: str) -> str:
    return f"<!-- studio:plan fp={fp} briefs={bhash} -->"


def build_planning_comment(record: dict) -> str:
    fp, bhash = record["fingerprint"], briefs_hash(record.get("briefs") or {})
    body = ("Studio planning record (bot state; retries reuse these exact "
            "candidates and briefs).\n\n"
            + planning_marker(fp, bhash) + "\n\n```json\n"
            + canonical_json(record) + "\n```")
    if len(body.encode("utf-8")) > 60000:
        raise ValueError("planning record exceeds GitHub comment budget; shorten creative instructions")
    return body


def parse_planning_comment(body: str | None) -> dict | None:
    if not body:
        return None
    m = _PLAN_RE.search(body)
    if not m:
        return None
    start = body.find("```json")
    if start < 0:
        return None
    start = body.find("\n", start) + 1
    try:
        record, _ = json.JSONDecoder().raw_decode(body[start:])
    except (ValueError, TypeError):
        return None
    if record.get("fingerprint") != m.group(1):
        return None
    if briefs_hash(record.get("briefs") or {}) != m.group(2):
        return None
    return record


def find_bot_planning_record(comments: list[dict],
                             bot_logins: set[str] | None = None) -> dict | None:
    """Latest authentic (bot-authored) planning record, or None."""
    from . import config as _cfg
    bots = bot_logins if bot_logins is not None else _cfg.BOT_LOGINS
    found = None
    for c in comments or []:
        if ((c.get("user") or {}).get("login") in bots):
            rec = parse_planning_comment(c.get("body"))
            if rec is not None:
                found = rec
    return found


def decide_intake(*, existing_fingerprint: str | None, new_fingerprint: str,
                  has_children: bool) -> dict:
    """Same fingerprint reuses; changed brief after planning must not
    overwrite human-approved work (open a new source issue instead)."""
    if existing_fingerprint is None:
        return {"action": "plan", "reason": "no prior plan"}
    if existing_fingerprint == new_fingerprint:
        return {"action": "reuse", "reason": "same fingerprint; reuse children"}
    if has_children:
        return {"action": "fail_new_issue",
                "reason": ("source/brief changed after planning; do not "
                           "overwrite existing work — open a new source issue")}
    return {"action": "plan", "reason": "fingerprint changed before planning"}

"""Lifecycle orchestration over a GitHub-like adapter (real or Fake).

Idempotency keys: fingerprint + child markers + branch names + artifact
keys. Duplicate events reuse children/branches/PRs and never re-notify
for the same head+artifact.
"""
from __future__ import annotations

import json

from . import config, fingerprint


def child_body(parent: int, cand: dict, fp: str, *, piece=None,
               period=None, art_notes: str = "", brief: dict | None = None,
               creative: dict | None = None) -> str:
    """Detailed child-issue body: DSP facts + owner creative fields
    (multi-line notes verbatim) + the candidate-specific planner brief
    verbatim. Selection IDs alone are never sufficient."""
    direction = config.PERIOD_DIRECTIONS.get(period or "", "")
    creative = creative or {}
    piece_v = piece if piece is not None else creative.get("piece")
    period_v = period if period is not None else creative.get("period")
    notes_v = art_notes if art_notes else (creative.get("art_notes") or "")
    lines = [
        f"Studio production child for source issue #{parent}.",
        "",
        f"- Candidate: `{cand['id']}` (DSP window, decided by ID only)",
        f"- Window: samples [{cand['start_sample']}, {cand['end_sample']}) "
        f"@ {cand['sample_rate']} Hz, {cand['n_frames']} frames @30fps, "
        f"{cand['duration_sec']}s",
        f"- Score: {cand['score']} (acoustic ranking; musical quality not demonstrated)",
        f"- Endpoint evidence: `{cand.get('endpoint_evidence')}`",
        f"- Scenes (deterministic): {[s['id'] for s in cand.get('scenes', [])]}",
        f"- Piece (owner-supplied, optional): {piece_v or 'unknown'}",
        f"- Period direction (creative, not fact): {period_v or 'unknown'}"
        + (f" — {direction}" if direction else ""),
        "- Art / direction notes (owner, verbatim):",
        notes_v or "(none)",
        "",
        "No manual seconds/cuts. Muse implements a declarative "
        "storyboard from the planner brief below; the trusted renderer "
        "produces the MP4. Timings come only from the deterministic "
        "scene IDs above.",
    ]
    if brief is not None:
        lines += [
            "",
            "## Planner creative brief (candidate-specific, verbatim)",
            "",
            "World / setting:",
            str(brief.get("world", "")),
            "",
            "Character:",
            str(brief.get("character", "")),
            "",
            "Narrative arc:",
            str(brief.get("narrative_arc", "")),
            "",
            "Creative interpretation (piece/period as direction, not fact):",
            str(brief.get("creative_interpretation", "")),
            "",
            "Scene guidance (deterministic scene IDs; no timing edits):",
        ]
        for g in brief.get("scene_guidance", []):
            lines.append(
                f"- `{g.get('scene_id')}` direction: {g.get('direction', '')} "
                f"| composition: {g.get('composition', '')} "
                f"| palette intent: {g.get('palette_intent', '')}")
        lines += [
            "",
            "Renderer constraints:",
            str(brief.get("renderer_constraints", "")),
            "",
            "Evidence notes:",
            str(brief.get("evidence_notes", "")),
            "",
            "Uncertainty:",
            str(brief.get("uncertainty", "")),
            "",
            "```json",
            json.dumps(brief, ensure_ascii=False, indent=2),
            "```",
        ]
    lines += [
        "",
        fingerprint.child_marker(parent, cand["id"], fp),
        fingerprint.fingerprint_marker(fp),
    ]
    return "\n".join(lines)


def ensure_children(gh, *, parent: int, selected: list[dict], fp: str,
                    piece=None, period=None, art_notes: str = "",
                    briefs: dict | None = None,
                    creative: dict | None = None) -> list[dict]:
    """Create-or-reuse true sub-issues and link them (sub_issue_id = db id).

    A child that exists but is not yet linked (partial create/link
    failure) is re-attached on retry instead of duplicated.
    """
    briefs = briefs or {}
    linked: set[int] = set()
    if hasattr(gh, "get_sub_issue_numbers"):
        linked = set(gh.get_sub_issue_numbers(parent))
    out = []
    for cand in selected:
        brief = briefs.get(cand["id"])
        existing = gh.find_child(parent, cand["id"], fp) \
            if hasattr(gh, "find_child") else None
        if existing is None:
            created = gh.create_issue(
                title=f"Studio production: {cand['id']} for #{parent}",
                body=child_body(parent, cand, fp, piece=piece,
                                period=period, art_notes=art_notes,
                                brief=brief, creative=creative))
            gh.link_sub_issue(parent, int(created["id"]))
            out.append(created)
        else:
            if int(existing["number"]) not in linked:
                gh.link_sub_issue(parent, int(existing["id"]))
            out.append(existing)
    return out


def artifact_key(pr_number: int, head_sha: str, artifact_id: int) -> str:
    return f"pr-{pr_number}:{head_sha}:{artifact_id}"


def bot_bodies(comments: list[dict]) -> list[str]:
    """Bodies of authentic bot comments only (user-forged marker text in
    a human comment never counts as bot evidence)."""
    return [(c.get("body") or "") for c in comments or []
            if (c.get("user") or {}).get("login") in config.BOT_LOGINS]


def should_notify(existing_bodies: list[str], key: str) -> bool:
    """Never duplicate the owner notification for same head+artifact."""
    return not any(key in (b or "") for b in existing_bodies)


def release_child_states(children: list[dict]) -> list[dict]:
    return [{"released": bool(c.get("released")),
             "state": c.get("state", "open"),
             "state_reason": c.get("state_reason"),
             "closed_by": ((c.get("closed_by") or {}).get("login")
                           if isinstance(c.get("closed_by"), dict)
                           else c.get("closed_by")),
             "closed_by_owner": bool(c.get("closed_by_owner"))}
            for c in children]

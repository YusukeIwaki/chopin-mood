"""Owner creative fields: parse a source issue form or a plain-text issue.

Piece/period are optional creative direction (never facts about the
recording); art notes are free multi-line text carried verbatim through
plan -> children -> Muse brief -> review. The period dropdown text
maps to early/middle/late; anything else is unknown.
"""
from __future__ import annotations

import re

from .validate import validate_attachment_url

_FIELD_HEADS = ("Recording attachment", "Piece", "Period direction",
                "Art / direction notes", "Art/direction notes")

_PERIOD_MAP = (("early", "early"), ("middle", "middle"), ("late", "late"))


def _field(body: str, names: tuple[str, ...]) -> str:
    for name in names:
        # GitHub uses the entire form label, including its parenthetical hint.
        pat = re.compile(r"^###\s+" + re.escape(name) + r"(?:\s+\([^\n]*\))?\s*$", re.M)
        m = pat.search(body or "")
        if not m:
            continue
        rest = body[m.end():]
        nxt = re.search(r"^###\s+", rest, re.M)
        text = rest[:nxt.start()] if nxt else rest
        # Drop the HTML-comment guidance GitHub forms may append.
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        return text.strip()
    return ""


def parse_issue_body(body: str | None) -> dict:
    """Return {piece, period, art_notes}; period None when unknown."""
    body = body or ""
    piece = _field(body, ("Piece",)) or None
    period_raw = _field(body, ("Period direction",)).lower()
    period = next((v for k, v in _PERIOD_MAP if k in period_raw), None)
    notes = _field(body, ("Art / direction notes", "Art/direction notes"))
    is_form = any(re.search(
        r"^###\s+" + re.escape(name) + r"(?:\s+\([^\n]*\))?\s*$",
        body, re.M) for name in _FIELD_HEADS)
    if not is_form:
        # A recording uploaded to an ordinary issue is also a supported
        # intake. Preserve the owner's direction, without treating an issue
        # title or attachment filename as a verified musical identity.
        notes = re.sub(r"<!--.*?-->", "", body, flags=re.S)
        notes = re.sub(
            r"!?\[[^\]\n]*\]\((https://[^\s)]+)\)",
            lambda m: "" if validate_attachment_url(m.group(1))["ok"] else m.group(0),
            notes)
        notes = re.sub(
            r"<?https://[^\s<>)]+>?",
            lambda m: "" if validate_attachment_url(m.group(0).strip("<>"))["ok"] else m.group(0),
            notes).strip()
    if notes in ("_No response_", "None", "none"):
        notes = ""
    if piece in ("_No response_",):
        piece = None
    return {"piece": piece, "period": period, "art_notes": notes}

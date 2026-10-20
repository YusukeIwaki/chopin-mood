"""Pure candidate-window logic (stdlib only, no DSP imports).

Windows are dicts with native-sample [start_sample, end_sample), n_frames,
sample_rate and fps [num, den]. Downstream decisions use ``id`` only.
"""
from __future__ import annotations

from fractions import Fraction

from . import config


def _round_half_up_frac(num: int, den: int) -> int:
    return (2 * num + den) // (2 * den)


def frame_boundary_sample(start: int, frame_index: int, sr: int,
                          fps_num: int, fps_den: int) -> int:
    spf = Fraction(sr * fps_den, fps_num)
    return int(start) + _round_half_up_frac(frame_index * spf.numerator,
                                            spf.denominator)


def verify_frame_bounds(cand: dict) -> list[str]:
    errs: list[str] = []
    try:
        s0, s1 = int(cand["start_sample"]), int(cand["end_sample"])
        nf, sr = int(cand["n_frames"]), int(cand["sample_rate"])
        fn, fd = (int(x) for x in cand["fps"])
    except (KeyError, TypeError, ValueError) as e:
        return [f"missing/invalid bound field: {e}"]
    if s0 < 0 or s1 <= s0:
        errs.append("samples must satisfy 0 <= start < end")
    if frame_boundary_sample(s0, nf, sr, fn, fd) != s1:
        errs.append("end_sample is not the n_frames frame boundary")
    return errs


def validate_candidate(cand: dict, *, min_sec: float = config.CANDIDATE_MIN_SEC,
                       max_sec: float = config.CANDIDATE_MAX_SEC) -> list[str]:
    errs = verify_frame_bounds(cand)
    try:
        dur = (int(cand["end_sample"]) - int(cand["start_sample"])) / int(cand["sample_rate"])
    except (KeyError, TypeError, ValueError):
        return errs + ["cannot compute duration"]
    if dur < min_sec - 1e-9:
        errs.append(f"duration {dur:.3f}s below min {min_sec:g}s")
    if dur > max_sec + 1e-9:
        errs.append(f"duration {dur:.3f}s above max {max_sec:g}s")
    if not str(cand.get("id", "")).startswith("cand-"):
        errs.append("candidate id must be a DSP-assigned cand-* id")
    return errs


def _overlap_ratio(a: dict, b: dict) -> float:
    lo = max(int(a["start_sample"]), int(b["start_sample"]))
    hi = min(int(a["end_sample"]), int(b["end_sample"]))
    if hi <= lo:
        return 0.0
    denom = min(int(a["end_sample"]) - int(a["start_sample"]),
                int(b["end_sample"]) - int(b["start_sample"]))
    return (hi - lo) / denom if denom > 0 else 0.0


def diversity_filter(ranked: list[dict], max_n: int = config.MAX_CANDIDATES,
                     max_overlap: float = 0.5) -> list[dict]:
    picked: list[dict] = []
    for cand in ranked:
        if len(picked) >= max_n:
            break
        if all(_overlap_ratio(cand, p) < max_overlap for p in picked):
            picked.append(cand)
    return picked


BRIEF_TEXT_MAX = 800
BRIEF_SCENE_TEXT_MAX = 220
_FORBIDDEN_BRIEF_KEYS = {"start_sample", "end_sample", "start_sec", "end_sec",
                         "seconds", "duration_sec", "n_frames", "start_frame",
                         "end_frame", "timing", "timestamps"}
BRIEF_FIELDS = ("world", "character", "narrative_arc",
                "creative_interpretation", "renderer_constraints",
                "evidence_notes", "uncertainty")


def validate_candidate_brief(candidate_id: str, brief, cand: dict) -> list[str]:
    """One candidate's creative brief: world/character/narrative plus
    scene guidance keyed EXACTLY to the candidate's deterministic scene
    IDs. Any timing field is rejected (no LLM timing edits)."""
    errs: list[str] = []
    if not isinstance(brief, dict):
        return [f"{candidate_id}: brief must be an object"]
    for key in brief:
        if str(key).lower() in _FORBIDDEN_BRIEF_KEYS:
            errs.append(f"{candidate_id}: brief must not set timing field {key}")
    for field in BRIEF_FIELDS:
        v = brief.get(field)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{candidate_id}: brief.{field} must be non-empty text")
        elif len(v) > BRIEF_TEXT_MAX:
            errs.append(f"{candidate_id}: brief.{field} exceeds {BRIEF_TEXT_MAX} chars")
    guidance = brief.get("scene_guidance")
    want = [s["id"] for s in cand.get("scenes", [])]
    if not isinstance(guidance, list) or not guidance:
        errs.append(f"{candidate_id}: brief.scene_guidance must be a non-empty list")
        return errs
    got = [g.get("scene_id") for g in guidance if isinstance(g, dict)]
    if got != want:
        errs.append(f"{candidate_id}: scene_guidance ids must be exactly {want}, got {got}")
    for g in guidance:
        if not isinstance(g, dict):
            errs.append(f"{candidate_id}: scene guidance must be objects")
            continue
        for key in g:
            if str(key).lower() in _FORBIDDEN_BRIEF_KEYS:
                errs.append(f"{candidate_id}: guidance must not set timing field {key}")
        for field in ("direction", "composition", "palette_intent"):
            v = g.get(field)
            if not isinstance(v, str) or not v.strip():
                errs.append(f"{candidate_id}/{g.get('scene_id')}: guidance.{field} required")
            elif len(v) > BRIEF_SCENE_TEXT_MAX:
                errs.append(f"{candidate_id}/{g.get('scene_id')}: guidance.{field} too long")
    return errs


def validate_planner_output(output, candidates: list[dict]) -> dict:
    """Full planner output: selected IDs (known only) + a candidate-
    specific creative brief for EVERY selected candidate."""
    failures: list[str] = []
    if not isinstance(output, dict):
        return {"ok": False, "failures": ["planner output must be an object"],
                "selected": [], "briefs": {}}
    ids = output.get("selected_candidate_ids")
    base = validate_planner_selection(ids, candidates)
    failures.extend(base["failures"])
    if not isinstance(output.get("rationale"), str) or not output["rationale"].strip():
        failures.append("planner rationale must be non-empty text")
    briefs = output.get("briefs")
    # Strict model JSON schemas cannot use an arbitrary-key object.
    # The external contract is an array; persisted controller records use a map.
    if isinstance(briefs, list):
        if any(not isinstance(x, dict) or set(x) != {"candidate_id", "brief"}
               for x in briefs):
            failures.append("each brief entry needs candidate_id and brief")
            briefs = {}
        else:
            pairs = [(x["candidate_id"], x["brief"]) for x in briefs]
            briefs = dict(pairs)
            if len(briefs) != len(pairs):
                failures.append("duplicate candidate brief")
    if not isinstance(briefs, dict):
        failures.append("planner briefs must be an object keyed by candidate id")
        briefs = {}
    if isinstance(ids, list):
        if set(briefs) != set(ids):
            failures.append(
                f"briefs must cover exactly the selected ids {sorted(set(ids))}, "
                f"got {sorted(briefs)}")
        by_id = {c["id"]: c for c in candidates}
        for cid in ids:
            if cid in by_id and cid in briefs:
                failures.extend(validate_candidate_brief(cid, briefs[cid], by_id[cid]))
    return {"ok": not failures, "failures": failures,
            "selected": base["selected"], "briefs": briefs}


def validate_planner_selection(selected_ids, candidates: list[dict]) -> dict:
    """Planner may only reference DSP candidate IDs (never invented timing)."""
    known = {c["id"] for c in candidates}
    failures: list[str] = []
    if not isinstance(selected_ids, list):
        return {"ok": False, "failures": ["selected_ids must be a list"],
                "selected": []}
    if len(selected_ids) > config.MAX_CANDIDATES:
        failures.append(f"at most {config.MAX_CANDIDATES} selections")
    if len(set(selected_ids)) != len(selected_ids):
        failures.append("duplicate selections")
    for sid in selected_ids:
        if sid not in known:
            failures.append(f"unknown candidate id: {sid}")
    return {"ok": not failures, "failures": failures,
            "selected": [c for c in candidates if c["id"] in set(selected_ids)]}

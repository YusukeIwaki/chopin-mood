"""Automatic clip selection (30-45 s) and sparse scene-cut optimization.

No input-specific times are hardcoded: candidates come from music-endpoint
candidates (dip / struct_boundary positions) plus a 1 s grid fallback, and
are ranked by dip/structure/activity/edge features. Candidate list with
accept/reject reasons is saved for audit.

Duration/fps arithmetic:
  The clip is defined by integer frames: n_frames in
  [ceil(30*fps), floor(45*fps)] (rational-fps exact, so n_frames/fps is in [30,45] s).
  end_sample = Timebase.clip_end_sample(start_sample, n_frames), i.e. frame
  boundaries b(i) = start + round_half_up(i * sr * fps_den / fps_num) in
  exact integer arithmetic. Display duration = n_frames / fps.
  A clip start is frame 0 of its own grid by definition; music-endpoint
  ends are snapped DOWN to the clip frame grid
  (end = clip_end_sample(start, sample_to_frame(start, music_end))) so the
  [30, 45] s window and every scene bound hold on frames, not on samples.

Scene cuts (2D DP over the frame grid):
  Cut positions may ONLY come from struct_boundary candidates inside the
  clip (margin 3 s from each edge) plus slow-envelope valleys, each snapped
  to the clip frame grid BEFORE the DP runs. Constraints (min scene length,
  max cuts) are then enforced in frame units. The DP state is
  dp[node][cuts_used]: keeping a single best path per node would discard a
  cheaper-fewer-cuts route that a later cut needs (see
  tests/test_clip_dp.py for a minimal counterexample), so the full table
  is kept. Objective: sum(cut strength) - 0.05 * n_cuts.
  3-6 scenes is a goal, never forced: weak support falls back to fewer
  scenes, down to a single scene with a recorded reason.

Endpoint honesty: every clip edge records its basis (nearest dip /
struct_boundary with distance and support, nearest accent distance, local
RMS) and a concern string. A musically "natural excerpt" is NOT claimed:
human evaluation of excerpt quality is pending (see docs/07).
"""
from __future__ import annotations

import math

import numpy as np

from .timebase import Timebase

MIN_CLIP_SEC = 30.0
MAX_CLIP_SEC = 45.0
MIN_SCENE_SEC = 6.0
MAX_CUTS = 5
EDGE_MARGIN_SEC = 3.0
CUT_STRENGTH_FLOOR = 0.15
#: distance within which an edge counts as "near" a music endpoint candidate
ENDPOINT_NEAR_SEC = 2.0
#: accents closer than this to an edge risk an audibly abrupt cut
ATTACK_GUARD_SEC = 0.5
ATTACK_PENALTY_W = 0.5
EDGE_RMS_PENALTY_W = 0.3
CUT_PENALTY = 0.05


def _sec_to_clip_frames(sec: float, tb: Timebase) -> int:
    return max(1, int(round(sec * float(tb.fps))))


def _edge_evidence(
    edge: int,
    dip_samples: np.ndarray,
    struct_samples: np.ndarray,
    accent_samples: np.ndarray,
    centers: np.ndarray,
    rms: np.ndarray,
    rms_max: float,
    sr: int,
) -> dict:
    """Per-edge basis record: nearest candidates, attack proximity, loudness."""
    def nearest(arr: np.ndarray) -> float | None:
        return float(np.min(np.abs(arr - edge))) / sr if arr.size else None

    d_dip = nearest(dip_samples)
    d_struct = nearest(struct_samples)
    d_acc = nearest(accent_samples)
    win = (centers >= edge - int(0.25 * sr)) & (centers < edge + int(0.25 * sr))
    edge_rms = float(rms[win].mean() / rms_max) if win.any() and rms_max > 0 else 0.0
    return {
        "nearest_dip_s": None if d_dip is None else round(d_dip, 3),
        "nearest_struct_s": None if d_struct is None else round(d_struct, 3),
        "nearest_accent_s": None if d_acc is None else round(d_acc, 3),
        "edge_rms_ratio": round(edge_rms, 4),
    }


def _lengths_frames(min_sec: float, max_sec: float, fps_f: float,
                    min_frames: int, max_frames: int) -> list[int]:
    """Frame lengths spanning [min_sec, max_sec]; defaults keep legacy set."""
    if abs(min_sec - MIN_CLIP_SEC) < 1e-9 and abs(max_sec - MAX_CLIP_SEC) < 1e-9:
        raw = [min_frames, int(round(32 * fps_f)), int(round(35 * fps_f)),
               int(round(38 * fps_f)), int(round(40 * fps_f)), max_frames]
    else:
        span = max_sec - min_sec
        fracs = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
        raw = [int(round((min_sec + span * f) * fps_f)) for f in fracs]
        raw[0], raw[-1] = min_frames, max_frames
    return sorted({L for L in raw if min_frames <= L <= max_frames})


def select_clip(
    feat: dict,
    events: dict,
    tb: Timebase,
    n_src: int,
    seed: int = 0,
    min_sec: float = MIN_CLIP_SEC,
    max_sec: float = MAX_CLIP_SEC,
    source_ending: bool = False,
) -> dict:
    """Rank clip candidates; return best + ranked table with reasons.

    ``min_sec``/``max_sec`` are optional (defaults preserve P0 30-45 s).
    Lengths are derived from the range. Raises ValueError when the source
    is too short for ``min_sec`` (no hidden padding/stretch).
    """
    if not (min_sec > 0 and max_sec > min_sec):
        raise ValueError(f"invalid clip range min_sec={min_sec} max_sec={max_sec}")
    if n_src / float(tb.sr) < min_sec:
        raise ValueError(
            f"source too short for excerpt: duration {n_src / float(tb.sr):.2f}s "
            f"< min_sec {min_sec:g}s; provide a longer recording "
            f"(no padding/stretch applied)")
    sr = tb.sr
    slow = feat["slow_n"]
    rms_raw = feat["rms"]
    rms_max = float(np.max(rms_raw)) if rms_raw.size else 0.0
    centers = feat["centers"]
    dip_samples = np.array([d["source_sample"] for d in events.get("dip", [])], dtype=np.int64)
    struct_samples = np.array(
        [e["source_sample"] for e in events.get("struct_boundary", [])], dtype=np.int64
    )
    peak_samples = np.array(
        [e["source_sample"] for e in events.get("activity_peak", [])], dtype=np.int64
    )
    accent_samples = np.array(
        [e["source_sample"] for e in events.get("accent", [])], dtype=np.int64
    )

    import math as _math

    fps_f = float(tb.fps)
    # Rational-fps-exact bounds: min=ceil(min_sec*fps), max=floor(max_sec*fps).
    min_frames = _math.ceil(min_sec * tb.fps)
    max_frames = _math.floor(max_sec * tb.fps)
    spf = tb.samples_per_frame
    min_clip_samples = (min_frames * spf.numerator + spf.denominator - 1) // spf.denominator

    lengths_frames = _lengths_frames(min_sec, max_sec, fps_f, min_frames, max_frames)

    # --- start pool: 1 s grid + music-endpoint positions (dip/struct) ---
    grid_starts = np.arange(0, max(1, n_src - min_clip_samples + 1), sr, dtype=np.int64)
    if tb.exact_integer:
        k = int(spf)
        grid_starts = (grid_starts // k) * k  # frame-exact starts on integer grids
    music_starts = np.array(
        [s for s in np.concatenate(
            [dip_samples, struct_samples]) if 0 <= s <= n_src - min_clip_samples],
        dtype=np.int64,
    )
    start_orig: dict[int, str] = {int(s): "grid" for s in grid_starts}
    for s in music_starts:
        s = int(s)
        if s not in start_orig:
            start_orig[s] = "music-endpoint"
    if len(music_starts) == 0:
        grid_fallback_note = "grid-fallback (no dip/struct start candidates)"
    else:
        grid_fallback_note = ""

    # --- music ends usable per start: dip/struct inside the [30,45] s window,
    # snapped down to the clip frame grid ---
    music_ends = np.array(
        sorted(set(int(s) for s in np.concatenate([dip_samples, struct_samples])
                   if 0 <= s <= n_src)),
        dtype=np.int64,
    )

    cands = []
    for s0, s_origin in sorted(start_orig.items()):
        # standard grid lengths
        pairs: list[tuple[int, int, str]] = []
        for nf in lengths_frames:
            s1 = tb.clip_end_sample(int(s0), nf)
            if s1 <= n_src:
                pairs.append((int(s0), int(s1), "grid-length"))
        # music ends in the duration window, snapped to this clip's grid
        if music_ends.size:
            lo, hi = s0 + int(min_sec * sr), s0 + int(max_sec * sr)
            for e in music_ends[(music_ends >= lo) & (music_ends <= hi)][:4]:
                nf = tb.sample_to_frame(int(s0), int(e))
                if not (min_frames <= nf <= max_frames):
                    continue
                s1 = tb.clip_end_sample(int(s0), nf)
                if s1 <= n_src and all(p[1] != s1 for p in pairs):
                    pairs.append((int(s0), int(s1), "music-endpoint"))
        if source_ending:
            # A separately ranked terminal alternative, never a hand-set
            # timestamp. Keep the entire recorded ending except a possible
            # incomplete last video frame. Normal P0 rankings stay unchanged.
            nf = tb.sample_to_frame(int(s0), n_src)
            pairs = ([(int(s0), int(tb.clip_end_sample(int(s0), nf)), "source-ending")]
                     if min_frames <= nf <= max_frames else [])
        for s0i, s1, e_origin in pairs:
            dur = (s1 - s0i) / sr
            mask = (centers >= s0i) & (centers < s1)
            seg = slow[mask] if mask.any() else np.zeros(1)
            contrast = float(seg.std())
            mean_act = float(seg.mean())
            n_struct = int(((struct_samples >= s0i + 2 * sr) & (struct_samples < s1 - 2 * sr)).sum())
            n_peak = int(((peak_samples >= s0i) & (peak_samples < s1)).sum())
            d0 = float(dip_samples[np.argmin(np.abs(dip_samples - s0i))]) if dip_samples.size else None
            d1 = float(dip_samples[np.argmin(np.abs(dip_samples - s1))]) if dip_samples.size else None
            edge_dip = 0.0
            if d0 is not None:
                edge_dip += max(0.0, 1.0 - abs(d0 - s0i) / (2.0 * sr))
            if d1 is not None:
                edge_dip += max(0.0, 1.0 - abs(d1 - s1) / (2.0 * sr))
            f0 = int(np.searchsorted(centers, s0i))
            f1 = int(np.searchsorted(centers, s1)) - 1
            edge_slope = 0.0
            if 2 <= f0 < len(slow) - 2:
                edge_slope += abs(float(slow[f0 + 1] - slow[f0 - 1]))
            if 2 <= f1 < len(slow) - 2:
                edge_slope += abs(float(slow[f1 + 1] - slow[f1 - 1]))
            ev0 = _edge_evidence(s0i, dip_samples, struct_samples, accent_samples,
                                 centers, rms_raw, rms_max, sr)
            ev1 = _edge_evidence(s1, dip_samples, struct_samples, accent_samples,
                                 centers, rms_raw, rms_max, sr)
            attack_pen = 0.0
            for ev in (ev0, ev1):
                if ev["nearest_accent_s"] is not None:
                    attack_pen += max(0.0, 1.0 - ev["nearest_accent_s"] / ATTACK_GUARD_SEC)
            edge_rms_pen = (ev0["edge_rms_ratio"] + ev1["edge_rms_ratio"]) / 2.0
            score = (
                1.6 * contrast
                + 0.35 * min(n_struct, 3)
                + 0.20 * min(n_peak, 3)
                + 0.30 * edge_dip
                + 0.10 * mean_act
                - 0.80 * edge_slope
                - ATTACK_PENALTY_W * attack_pen
                - EDGE_RMS_PENALTY_W * edge_rms_pen
            )
            quiet = mean_act < 0.08
            if quiet:
                score -= 0.5
            nf = tb.sample_to_frame(s0i, s1 - 1) + 1 if s1 > s0i else 0
            cands.append(
                {
                    "start_sample": int(s0i), "end_sample": int(s1),
                    "n_frames": int(nf), "duration_sec": round((s1 - s0i) / sr, 4),
                    "score": round(float(score), 4),
                    "parts": {
                        "contrast": round(contrast, 4), "n_struct_inside": n_struct,
                        "n_peaks_inside": n_peak, "edge_dip": round(float(edge_dip), 4),
                        "mean_activity": round(mean_act, 4),
                        "edge_slope": round(float(edge_slope), 4),
                        "attack_prox_pen": round(float(attack_pen), 4),
                        "edge_rms_pen": round(float(edge_rms_pen), 4),
                    },
                    "start_origin": s_origin, "end_origin": e_origin,
                    "endpoint_evidence": {"start": ev0, "end": ev1},
                    "quiet": bool(quiet),
                }
            )
    if not cands:
        raise RuntimeError("no clip candidate fits the source length")
    cands.sort(key=lambda c: c["score"], reverse=True)
    best = dict(cands[0])
    ev0, ev1 = best["endpoint_evidence"]["start"], best["endpoint_evidence"]["end"]
    concerns = []
    for name, ev in (("start", ev0), ("end", ev1)):
        d_dip, d_st = ev["nearest_dip_s"], ev["nearest_struct_s"]
        near = min([d for d in (d_dip, d_st) if d is not None], default=None)
        if near is None:
            concerns.append(f"{name}: no dip/struct candidates at all")
        elif near > ENDPOINT_NEAR_SEC:
            concerns.append(f"{name}: {near:.1f}s from nearest music endpoint (> {ENDPOINT_NEAR_SEC:g}s)")
        if ev["nearest_accent_s"] is not None and ev["nearest_accent_s"] < 0.3:
            concerns.append(f"{name}: accent {ev['nearest_accent_s']:.2f}s away (abrupt-cut risk)")
        if ev["edge_rms_ratio"] > 0.5:
            concerns.append(f"{name}: loud edge (rms ratio {ev['edge_rms_ratio']:.2f})")
    if not concerns:
        concerns.append("edges near music endpoints; musical naturalness still unevaluated by humans")
    else:
        concerns.append("musical naturalness of the excerpt is unevaluated by humans")
    basis = f"{best['start_origin']}/{best['end_origin']}"
    if grid_fallback_note:
        basis += "; " + grid_fallback_note
    best["endpoint_basis"] = basis
    best["endpoint_concerns"] = concerns
    reasons = (
        f"contrast={best['parts']['contrast']}, "
        f"struct_inside={best['parts']['n_struct_inside']}, "
        f"peaks_inside={best['parts']['n_peaks_inside']}, "
        f"edge_dip={best['parts']['edge_dip']}, "
        f"edge_slope={best['parts']['edge_slope']}, "
        f"attack_pen={best['parts']['attack_prox_pen']}, "
        f"edge_rms_pen={best['parts']['edge_rms_pen']}"
        + (", QUIET-penalized" if best["quiet"] else "")
    )
    best["selection_reason"] = (
        f"top score ({reasons}); start {best['start_origin']}, end {best['end_origin']}; "
        "no hardcoded times; excerpt musical quality NOT human-evaluated"
    )
    rejected = [
        {"start_sample": c["start_sample"], "end_sample": c["end_sample"],
         "score": c["score"], "reason": "lower score"}
        for c in cands[1:6]
    ]
    return {"best": best, "ranked": cands[:12], "rejected_sample": rejected}


def _valleys(feat: dict, s0: int, s1: int, sr: int) -> list[tuple[int, float]]:
    slow = feat["slow_n"]
    centers = feat["centers"]
    mask = (centers >= s0) & (centers < s1)
    idx = np.nonzero(mask)[0]
    if len(idx) < 10:
        return []
    seg = slow[idx]
    depth = 1.0 - seg
    from scipy import signal as sig

    peaks, props = sig.find_peaks(depth, distance=max(1, int(4 * sr / feat["meta"]["hop"])),
                                  prominence=0.08)
    out = []
    for p, prom in zip(peaks, props["prominences"]):
        out.append((int(centers[idx[p]]), round(float(min(prom, 1.0)) * 0.5, 4)))
    return out


def _dp_cuts(
    pos_frames: list[int],
    weights: list[float],
    min_len_frames: int,
    max_cuts: int,
    cut_penalty: float = CUT_PENALTY,
) -> tuple[list[int], float]:
    """2D DP: best interior-node subset under min-length + max-cuts.

    dp[i][c] = best value reaching node i using exactly c cuts (interior
    nodes selected so far). Returns (chosen interior node indices, value).
    Pure function of frame positions; independently checkable by brute force.
    """
    m = len(pos_frames)
    NEG = -1e18
    dp = [[NEG] * (max_cuts + 1) for _ in range(m)]
    prev: list[list[tuple[int, int]]] = [[(-1, -1)] * (max_cuts + 1) for _ in range(m)]
    dp[0][0] = 0.0
    for i in range(1, m):
        is_cut = 0 < i < m - 1
        for c in range(max_cuts + 1):
            c_prev = c - (1 if is_cut else 0)
            if c_prev < 0:
                continue
            gain = weights[i] - cut_penalty if is_cut else 0.0
            for j in range(i):
                if pos_frames[i] - pos_frames[j] < min_len_frames:
                    continue
                if dp[j][c_prev] == NEG:
                    continue
                val = dp[j][c_prev] + gain
                if val > dp[i][c]:
                    dp[i][c] = val
                    prev[i][c] = (j, c_prev)
    best_c = max(range(max_cuts + 1), key=lambda c: dp[m - 1][c])
    if dp[m - 1][best_c] == NEG:
        return [], NEG
    chain: list[int] = []
    i, c = m - 1, best_c
    while i > 0:
        if 0 < i < m - 1:
            chain.append(i)
        i, c = prev[i][c]
    return sorted(chain), dp[m - 1][best_c]


def plan_scenes(
    clip: dict,
    events: dict,
    feat: dict,
    tb: Timebase,
) -> dict:
    """DP over candidate cut positions; sparse cuts or single-scene fallback.

    Candidates are snapped to the clip frame grid FIRST; the DP and all
    length constraints run on frame indices, so scenes can never violate
    the minimum length through a post-hoc sample floor.
    """
    sr = tb.sr
    s0, s1 = clip["start_sample"], clip["end_sample"]
    n_frames = clip["n_frames"]
    margin = int(EDGE_MARGIN_SEC * sr)
    raw: list[tuple[int, float]] = [
        (int(e["source_sample"]), float(e["strength"]))
        for e in events.get("struct_boundary", [])
        if s0 + margin <= e["source_sample"] <= s1 - margin
    ]
    raw += [(s, w) for s, w in _valleys(feat, s0, s1, sr) if s0 + margin <= s <= s1 - margin]
    raw = [(s, w) for s, w in raw if w >= CUT_STRENGTH_FLOOR]
    raw.sort()
    # dedupe within 2 s, keep stronger
    dedup: list[tuple[int, float]] = []
    for s, w in raw:
        if dedup and s - dedup[-1][0] <= 2 * sr:
            if w > dedup[-1][1]:
                dedup[-1] = (s, w)
        else:
            dedup.append((s, w))

    # snap to the clip frame grid BEFORE solving constraints
    snapped: dict[int, float] = {}
    for s, w in dedup:
        if not (s0 < s < s1):
            continue
        f = tb.sample_to_frame(s0, s)
        if f <= 0 or f >= n_frames:
            continue
        qb = tb.frame_boundary_sample(s0, f)
        if qb not in snapped or w > snapped[qb]:
            snapped[qb] = w
    cut_frames = sorted(tb.sample_to_frame(s0, q) for q in snapped)
    cut_w = [snapped[tb.frame_boundary_sample(s0, f)] for f in cut_frames]

    fps_frac = tb.fps
    min_len_frames = (int(MIN_SCENE_SEC * fps_frac.numerator) + fps_frac.denominator - 1) // fps_frac.denominator
    pos = [0] + cut_frames + [n_frames]
    wgt = [0.0] + cut_w + [0.0]
    chosen, value = _dp_cuts(pos, wgt, min_len_frames, MAX_CUTS)
    cuts = [tb.frame_boundary_sample(s0, pos[i]) for i in chosen]
    if value <= -1e17:
        cuts = []
        reason = "no feasible cut placement under min-scene constraint; single scene fallback"
    elif not cuts:
        reason = (
            "best DP value uses no cuts (cut support below strength floor "
            f"{CUT_STRENGTH_FLOOR} or gain < cut penalty); single scene fallback"
        )
    else:
        reason = (
            f"DP chose {len(cuts)} cut(s) from {len(dedup)} candidate(s) "
            f"({len(snapped)} after frame snap); "
            f"min scene {MIN_SCENE_SEC:g}s = {min_len_frames} frames, max cuts {MAX_CUTS}"
        )
    bounds = [s0] + cuts + [s1]
    scenes = []
    for k in range(len(bounds) - 1):
        a, b = bounds[k], bounds[k + 1]
        scenes.append(
            {
                "id": f"scene{k + 1:02d}",
                "start_sample": int(a), "end_sample": int(b),
                "start_frame": int(tb.sample_to_frame(s0, a)),
                "end_frame": int(tb.sample_to_frame(s0, b - 1) + 1) if b > a else 0,
                "duration_sec": round((b - a) / sr, 4),
            }
        )
    if scenes:
        scenes[-1]["end_frame"] = int(n_frames)
    return {
        "cuts_source_samples": [int(c) for c in cuts],
        "candidate_cuts": [
            {"source_sample": int(s), "strength": float(w),
             "frame_snapped": int(tb.sample_to_frame(s0, s))}
            for s, w in sorted(snapped.items())
        ],
        "scenes": scenes,
        "reason": reason,
        "constraints": {
            "min_scene_sec": MIN_SCENE_SEC, "min_scene_frames": min_len_frames,
            "max_cuts": MAX_CUTS,
            "strength_floor": CUT_STRENGTH_FLOOR, "edge_margin_sec": EDGE_MARGIN_SEC,
            "grid": "clip frame grid (snapped before DP)",
        },
    }


def _overlap_ratio(a: dict, b: dict) -> float:
    """Overlap / min(duration) for two [start,end) sample windows."""
    lo = max(int(a["start_sample"]), int(b["start_sample"]))
    hi = min(int(a["end_sample"]), int(b["end_sample"]))
    if hi <= lo:
        return 0.0
    denom = min(int(a["end_sample"]) - int(a["start_sample"]),
                int(b["end_sample"]) - int(b["start_sample"]))
    return (hi - lo) / denom if denom > 0 else 0.0


def diversity_filter(ranked: list[dict], max_n: int = 3,
                     max_overlap: float = 0.5) -> list[dict]:
    """Greedy diverse subset of score-ranked windows (never forces ``max_n``)."""
    picked: list[dict] = []
    for cand in ranked:
        if len(picked) >= max_n:
            break
        if all(_overlap_ratio(cand, p) < max_overlap for p in picked):
            picked.append(cand)
    return picked


def rank_excerpt_candidates(
    feat: dict, events: dict, tb: Timebase, n_src: int,
    seed: int = 0, min_sec: float = 60.0, max_sec: float = 120.0,
    max_candidates: int = 3,
) -> dict:
    """Ranked 60-120 s excerpt candidates with stable IDs (studio P1).

    Wraps :func:`select_clip` with the extended range, then applies the
    diversity filter. Each candidate carries native-sample [start,end),
    frame count and endpoint evidence. Decisions downstream use ``id``
    only, never LLM seconds.
    """
    sel = select_clip(feat, events, tb, n_src, seed=seed,
                      min_sec=min_sec, max_sec=max_sec)
    diverse = diversity_filter(sel["ranked"], max_n=max_candidates)
    # Reserve an unused slot for a terminal alternative. Diversity filtering
    # alone can discard a coda because its window overlaps a higher score.
    # Existing ranked IDs are preserved; an overlapping ending is explicitly
    # an alternative, not evidence of a different musical section.
    if len(diverse) < max_candidates and not any(
            tb.sample_to_frame(int(c["start_sample"]), n_src) == int(c["n_frames"])
            for c in diverse):
        terminal = select_clip(feat, events, tb, n_src, seed=seed,
                               min_sec=min_sec, max_sec=max_sec, source_ending=True)
        diverse.append(terminal["best"])
    out = []
    for i, c in enumerate(diverse):
        s0, s1 = int(c["start_sample"]), int(c["end_sample"])
        nf = int(c["n_frames"])
        # Frame/sample endpoint verification on the native grid.
        assert tb.clip_end_sample(s0, nf) == s1, (s0, nf, s1)
        assert min_sec - 1e-9 <= (s1 - s0) / float(tb.sr) <= max_sec + 1e-9
        out.append({
            "id": f"cand-{i + 1:02d}",
            "start_sample": s0, "end_sample": s1, "n_frames": nf,
            "duration_sec": round((s1 - s0) / float(tb.sr), 4),
            "score": c["score"], "parts": c["parts"],
            "start_origin": c["start_origin"], "end_origin": c["end_origin"],
            "endpoint_evidence": c["endpoint_evidence"],
            "endpoint_basis": c.get("endpoint_basis", ""),
            "sample_rate": int(tb.sr),
            "fps": [tb.fps.numerator, tb.fps.denominator],
        })
    return {"candidates": out, "ranked_total": len(sel["ranked"]),
            "selection_caveat": ("acoustic ranking only; musical quality "
                                 "not demonstrated by DSP scores")}

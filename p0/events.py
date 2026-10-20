"""Event / structure candidates with integer source-sample positions.

Every candidate carries an uncalibrated `strength` in [0,1] and a `method`
string. Strength is NOT a probability: call it `strength`, never
`confidence`. Time in seconds is display-only (`time_sec`); the reference
is `source_sample` (int, source grid).

Kinds:
  accent            fast attack candidate (spectral flux peak)
  activity_peak     slow-envelope local peak (broad energy high point)
  dip               low-energy / rest candidate (slow envelope below p15)
  struct_boundary   section-change CANDIDATE (slow-envelope edge and/or
                    harmonic-novelty peak). Sparse by construction
                    (min separation 8 s). Not a phrase/cadence fact.

Flat inputs (see features.robust_norm) yield no events.
"""
from __future__ import annotations

import numpy as np
from scipy import signal as sig

MIN_STRUCT_SEP_SEC = 8.0
MIN_ACCENT_SEP_SEC = 0.4


def _sec_to_frames(sec: float, sr: int, hop: int) -> int:
    return max(1, int(round(sec * sr / hop)))


def _pick_peaks(
    curve: np.ndarray,
    centers: np.ndarray,
    sr: int,
    min_sep_sec: float,
    hop: int,
    prominence_frac: float,
    method: str,
    kind: str,
    top_k: int | None = None,
    strength_curve: np.ndarray | None = None,
) -> list[dict]:
    dist = _sec_to_frames(min_sep_sec, sr, hop)
    rng = float(curve.max() - curve.min()) if curve.size else 0.0
    if rng <= 0:
        return []
    peaks, props = sig.find_peaks(curve, distance=dist, prominence=prominence_frac * rng)
    if len(peaks) == 0:
        return []
    order = np.argsort(props["prominences"])[::-1]
    if top_k is not None:
        order = order[:top_k]
    out = []
    for idx in peaks[order]:
        s = strength_curve if strength_curve is not None else curve
        denom = float(s.max() - s.min())
        strength = float((s[idx] - s.min()) / denom) if denom > 0 else 0.0
        sample = int(centers[idx])
        out.append(
            {
                "kind": kind,
                "source_sample": sample,
                "time_sec": sample / float(sr),
                "strength": round(strength, 4),
                "method": method,
            }
        )
    out.sort(key=lambda e: e["source_sample"])
    return out


def detect_accents(feat: dict) -> list[dict]:
    """Fast attack candidates from spectral flux. Empty when flux is flat."""
    if feat["meta"]["flat"]["flux"]:
        return []
    return _pick_peaks(
        feat["flux_n"], feat["centers"], feat["meta"]["sr"],
        MIN_ACCENT_SEP_SEC, feat["meta"]["hop"],
        prominence_frac=0.12, method="flux_peak_v1", kind="accent",
    )


def detect_activity_peaks(feat: dict, top_k: int = 12) -> list[dict]:
    if feat["meta"]["flat"]["slow_env"]:
        return []
    return _pick_peaks(
        feat["slow_n"], feat["centers"], feat["meta"]["sr"],
        MIN_STRUCT_SEP_SEC, feat["meta"]["hop"],
        prominence_frac=0.10, method="slow_env_peak_v1", kind="activity_peak",
        top_k=top_k,
    )


def detect_dips(feat: dict, min_dur_sec: float = 1.5, quantile: float = 15.0) -> list[dict]:
    """Low-energy/rest candidates: slow envelope below its p15 for >= min_dur."""
    meta, sr, hop = feat["meta"], feat["meta"]["sr"], feat["meta"]["hop"]
    if meta["flat"]["slow_env"]:
        return []
    env = feat["slow_env"]
    thr = float(np.percentile(env, quantile))
    below = env < thr
    min_len = _sec_to_frames(min_dur_sec, sr, hop)
    out, i, n = [], 0, len(env)
    while i < n:
        if not below[i]:
            i += 1
            continue
        j = i
        while j < n and below[j]:
            j += 1
        if j - i >= min_len:
            seg = env[i:j]
            k = i + int(np.argmin(seg))
            depth = 1.0 - float(env[k] / (env.max() + 1e-12))
            out.append(
                {
                    "kind": "dip",
                    "source_sample": int(feat["centers"][k]),
                    "time_sec": float(feat["centers"][k]) / sr,
                    "strength": round(float(np.clip(depth, 0, 1)), 4),
                    "method": f"slow_env_below_p{quantile:g}_v1",
                }
            )
        i = j
    return out


def _pick_novelty_peaks(feat: dict, sr: int, hop: int) -> list[dict]:
    """Peak-pick harm novelty but report the comparison-interval midpoint.

    harm_novelty[i] compares [(i-lag)*hop, i*hop]; the event time is
    mid(i) = (start+end)//2 (integer, t-lag/2). Volume edges and flux
    attacks keep the right-edge convention and are NOT shifted here.
    Each novelty candidate carries novelty_start_sample/novelty_end_sample
    plus source_sample=mid. All samples are ints in [0, n_src].
    """
    from scipy import signal as sig

    meta = feat["meta"]
    lag = int(meta.get("novelty_lag_frames", 1))
    n_src = int(meta.get("n_source_samples", int(np.max(feat["centers"])) if len(feat["centers"]) else 0))
    curve = np.asarray(feat["nov_n"], dtype=np.float64)
    n = int(curve.size)
    if n == 0:
        return []
    # Interval arrays: prefer stored arrays, else recompute from lag/hop.
    if "novelty_start_samples" in feat and "novelty_end_samples" in feat:
        starts = np.asarray(feat["novelty_start_samples"], dtype=np.int64)
        ends = np.asarray(feat["novelty_end_samples"], dtype=np.int64)
        mids = np.asarray(feat["novelty_centers"], dtype=np.int64)
    else:
        idx = np.arange(n, dtype=np.int64)
        starts = (idx - np.int64(lag)) * np.int64(hop)
        ends = idx * np.int64(hop)
        mids = (starts + ends) // np.int64(2)
    dist = _sec_to_frames(MIN_STRUCT_SEP_SEC, sr, hop)
    rng = float(curve.max() - curve.min())
    if rng <= 0:
        return []
    peaks, props = sig.find_peaks(curve, distance=dist, prominence=0.15 * rng)
    if len(peaks) == 0:
        return []
    order = np.argsort(props["prominences"])[::-1]
    denom = float(curve.max() - curve.min())
    out = []
    for idx in peaks[order]:
        i = int(idx)
        mid = int(mids[i])
        # Clamp to the source grid; peaks with novelty 0 at the head never
        # survive prominence, but guard explicitly per spec.
        mid = max(0, min(mid, n_src))
        start = int(starts[i])
        end = int(ends[i])
        start = max(0, min(start, n_src))
        end = max(0, min(end, n_src))
        strength = float((curve[i] - curve.min()) / denom) if denom > 0 else 0.0
        out.append(
            {
                "kind": "struct_nov_raw",
                "source_sample": mid,
                "novelty_start_sample": start,
                "novelty_end_sample": end,
                "time_sec": float(mid) / float(sr),
                "strength": round(strength, 4),
                "method": "chroma_novelty_v1",
            }
        )
    out.sort(key=lambda e: e["source_sample"])
    return out


def detect_struct_boundaries(feat: dict) -> list[dict]:
    """Sparse section-change candidates from slow-envelope edges + harmonic novelty.

    Each candidate records which cues support it (`support`: subset of
    {"slow_edge", "harm_novelty"}). Single-cue candidates get lower strength.
    Novelty candidates use the comparison-interval midpoint mid(i)
    (t-lag/2) and carry novelty_start/end_sample; slow-edge candidates keep
    the right-edge i*hop convention. Merged dual-cue events average the two
    cue times (existing weighting, unchanged) but retain the novelty
    interval when harm_novelty is in support.

    Silence gating vs flat-envelope are SEPARATE: only TRUE silence
    (max time-domain RMS below SILENCE_RMS) yields no candidates. A
    constant-energy signal (flat slow envelope, e.g. a chord change at
    fixed loudness) still yields harmonic-novelty candidates, single-cue
    with reduced strength. Envelope-flatness alone never suppresses the
    chroma cue.
    """
    from .features import SILENCE_RMS

    meta, sr, hop = feat["meta"], feat["meta"]["sr"], feat["meta"]["hop"]
    n_src = int(meta.get("n_source_samples", 0)) or None
    if float(np.max(feat["rms"])) < SILENCE_RMS:
        return []  # true silence: no structure to find
    edges: list[dict] = []
    if not meta["flat"]["slow_env"]:
        slow_n = feat["slow_n"]
        grad = np.abs(np.gradient(slow_n))
        edges = _pick_peaks(
            grad, feat["centers"], sr, MIN_STRUCT_SEP_SEC, hop,
            prominence_frac=0.15, method="slow_edge_v1", kind="struct_edge_raw",
        )
    novs: list[dict] = []
    if not meta["flat"]["harm_novelty"]:
        novs = _pick_novelty_peaks(feat, sr, hop)
    tol = _sec_to_frames(2.0, sr, hop)
    by_pos = sorted(
        [(e["source_sample"], "slow_edge", e["strength"], None, None) for e in edges] +
        [(e["source_sample"], "harm_novelty", e["strength"],
          e.get("novelty_start_sample"), e.get("novelty_end_sample")) for e in novs]
    )
    merged: list[dict] = []
    for sample, cue, strength, nstart, nend in by_pos:
        if merged and abs(sample - merged[-1]["source_sample"]) <= tol * hop:
            prev = merged[-1]
            if cue not in prev["support"]:
                prev["support"].append(cue)
            prev["strength"] = round(max(prev["strength"], strength), 4)
            prev["source_sample"] = int((prev["source_sample"] + sample) // 2)
            if n_src is not None:
                prev["source_sample"] = max(0, min(prev["source_sample"], n_src))
            prev["time_sec"] = prev["source_sample"] / float(sr)
            # Retain the novelty comparison interval when harm_novelty supports
            # the merged candidate (existing averaging of times is unchanged).
            if cue == "harm_novelty" and nstart is not None and nend is not None:
                if "novelty_start_sample" in prev and "novelty_end_sample" in prev:
                    prev["novelty_start_sample"] = int(
                        (prev["novelty_start_sample"] + int(nstart)) // 2)
                    prev["novelty_end_sample"] = int(
                        (prev["novelty_end_sample"] + int(nend)) // 2)
                else:
                    prev["novelty_start_sample"] = int(nstart)
                    prev["novelty_end_sample"] = int(nend)
        else:
            entry: dict = {
                "kind": "struct_boundary",
                "source_sample": int(sample),
                "time_sec": float(sample) / sr,
                "strength": round(strength, 4),
                "method": "slow_edge+chroma_novelty_v1",
                "support": [cue],
            }
            if cue == "harm_novelty" and nstart is not None and nend is not None:
                entry["novelty_start_sample"] = int(nstart)
                entry["novelty_end_sample"] = int(nend)
            merged.append(entry)
    for m in merged:
        if len(m["support"]) == 1:
            m["strength"] = round(m["strength"] * 0.7, 4)
    merged.sort(key=lambda e: e["source_sample"])
    return merged


def detect_all(feat: dict) -> dict[str, list[dict]]:
    """Run all detectors; flat-input guards live in each detector."""
    return {
        "accent": detect_accents(feat),
        "activity_peak": detect_activity_peaks(feat),
        "dip": detect_dips(feat),
        "struct_boundary": detect_struct_boundaries(feat),
    }

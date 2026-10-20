"""Build analysis.json / plan.json / storyboard.json and per-frame control arrays.

Control semantics (two render modes, same clip + seed):
  naive:      light/zoom follow RAW rms directly (twitchy baseline).
  structured: light follows the SLOW envelope with hysteresis + slew-rate
              limit; zoom/camera drift slowly per scene; accent_pulse is a
              short decaying pulse ONLY at sparse accent frames (min gap 2 s,
              strength floor 0.5, cap 10 per clip). Scene/palette switch only
              at DP cut frames.

All control arrays are pure functions of (frame_index, clip, events, seed):
rendering order cannot change them. Per-frame grain uses RandomState(seed,
frame) reseeding, also order-independent.
"""
from __future__ import annotations

import numpy as np

ACCENT_MIN_GAP_SEC = 2.0
ACCENT_STRENGTH_FLOOR = 0.5
ACCENT_CAP = 10
#: Minimum structured light level. Silence still renders darkest, but the
#: placeholder never encodes full black: stars/moon stay visible so the
#: degraded (fallback) render is a dim scene, not a black file that QA
#: must reject. Recorded here, not hidden in the renderer.
LIGHT_FLOOR = 0.12


def _hysteresis_slew(x: np.ndarray, lo: float = 0.35, hi: float = 0.55,
                     max_step: float = 0.02) -> np.ndarray:
    """Hysteresis gate + slew-rate limit so light cannot twitch."""
    y = np.zeros_like(x)
    on = False
    cur = x[0] if len(x) else 0.0
    for i, v in enumerate(x):
        if v >= hi:
            on = True
        elif v <= lo:
            on = False
        target = v if on else min(v, lo)
        dv = target - cur
        cur += float(np.clip(dv, -max_step, max_step))
        y[i] = cur
    return y


def pick_accents(events: dict, clip: dict, sr: int) -> list[dict]:
    s0, s1 = clip["start_sample"], clip["end_sample"]
    cands = [e for e in events.get("accent", [])
             if s0 <= e["source_sample"] < s1 and e["strength"] >= ACCENT_STRENGTH_FLOOR]
    cands.sort(key=lambda e: -e["strength"])
    picked: list[dict] = []
    for e in cands:
        if all(abs(e["source_sample"] - p["source_sample"]) >= ACCENT_MIN_GAP_SEC * sr
               for p in picked):
            picked.append(e)
        if len(picked) >= ACCENT_CAP:
            break
    picked.sort(key=lambda e: e["source_sample"])
    return picked


def _resample_to_frames(curve: np.ndarray, centers: np.ndarray, bounds: np.ndarray) -> np.ndarray:
    """Nearest-left resample of a feature curve onto frame-boundary grid."""
    idx = np.searchsorted(centers, bounds, side="right") - 1
    idx = np.clip(idx, 0, len(curve) - 1)
    return curve[idx]


def build_controls(
    mode: str,
    n_frames: int,
    tb,
    clip: dict,
    scenes: list[dict],
    accents: list[dict],
    feat: dict,
    seed: int,
) -> dict[str, np.ndarray]:
    assert mode in ("naive", "structured")
    sr = tb.sr
    s0 = clip["start_sample"]
    bounds = np.array([tb.frame_boundary_sample(s0, i) for i in range(n_frames)])
    rms_f = _resample_to_frames(feat["rms_n"], feat["centers"], bounds)
    slow_f = _resample_to_frames(feat["slow_n"], feat["centers"], bounds)

    scene_of = np.zeros(n_frames, dtype=np.int64)
    for k, sc in enumerate(scenes):
        scene_of[sc["start_frame"]:sc["end_frame"]] = k

    t = np.arange(n_frames, dtype=np.float64)
    if mode == "naive":
        light = np.clip(rms_f, 0, 1)
        zoom = 1.0 + 0.12 * rms_f
        camx = 0.5 + 0.10 * (rms_f - 0.5)
        particle = np.clip(rms_f * 1.2, 0, 1)
        accent_pulse = np.zeros(n_frames)
    else:
        light = np.clip(_hysteresis_slew(np.clip(slow_f, 0, 1)),
                        LIGHT_FLOOR, 1.0)
        zoom = np.ones(n_frames)
        camx = np.zeros(n_frames)
        for k, sc in enumerate(scenes):
            a, b = sc["start_frame"], sc["end_frame"]
            if b <= a:
                continue
            rng = np.random.RandomState(seed + 1000 + k)
            drift = np.linspace(0, 1, b - a)
            zoom[a:b] = 1.0 + 0.04 * drift + 0.02 * rng.rand()
            camx[a:b] = 0.5 + 0.06 * np.sin(2 * np.pi * drift + rng.rand() * 6.28)
        particle = np.clip(slow_f * 0.8, 0, 1)
        accent_pulse = np.zeros(n_frames)
        for e in accents:
            f = tb.sample_to_frame(s0, int(e["source_sample"]))
            if 0 <= f < n_frames:
                decay = np.exp(-np.arange(0, min(15, n_frames - f)) / 4.0)
                accent_pulse[f:f + len(decay)] = np.maximum(
                    accent_pulse[f:f + len(decay)], decay * float(e["strength"])
                )
    camy = 0.5 + 0.03 * np.sin(2 * np.pi * t / max(1, n_frames))
    return {
        "light": light.astype(np.float64),
        "zoom": zoom.astype(np.float64),
        "camx": camx.astype(np.float64),
        "camy": camy.astype(np.float64),
        "particle": particle.astype(np.float64),
        "accent_pulse": accent_pulse.astype(np.float64),
        "scene_id": scene_of.astype(np.int64),
    }


def downsample_curve(x: np.ndarray, n: int = 600) -> list[float]:
    if len(x) <= n:
        return [round(float(v), 4) for v in x]
    idx = np.linspace(0, len(x) - 1, n).astype(int)
    return [round(float(x[i]), 4) for i in idx]


def downsample_curve_with_samples(
    values: np.ndarray, samples: np.ndarray, n: int = 600,
    valid: np.ndarray | None = None,
) -> tuple[list[float], list[int]]:
    """Downsample a curve AND its integer source-sample positions together.

    `values[j]` and `samples[j]` always come from the same element: a shared
    linspace index is applied to both after optional validity filtering, so
    value/coordinate pairs can never drift apart. Returns
    (rounded_values, int_samples). `valid`, when given, is a boolean mask
    over the full-length arrays (excluded points are dropped as pairs).
    """
    v = np.asarray(values)
    s = np.asarray(samples, dtype=np.int64)
    if valid is not None:
        mask = np.asarray(valid, dtype=bool)
        v, s = v[mask], s[mask]
    if len(v) <= n:
        idx = np.arange(len(v), dtype=int)
    else:
        idx = np.linspace(0, len(v) - 1, n).astype(int)
    return [round(float(v[i]), 4) for i in idx], [int(s[i]) for i in idx]


def build_analysis_json(
    rel_audio: str, input_hash: str, feat: dict, events: dict,
    n_src: int, sr: int, params: dict,
    features_uri: str = "output/features.npz",
    features_uri_base: str = "repo",
) -> dict:
    flat = feat["meta"]["flat"]
    # Paired downsampling: each value shares its extraction index with its
    # integer source-sample position, so consumers must NOT spread curves
    # uniformly over the full duration. rms/flux/slow use `centers`
    # (right-edge i*hop); nov_n uses `novelty_centers` (comparison-interval
    # midpoint (start+end)//2). novelty_centers holds negative head
    # positions (pre-source padding region); those points are dropped as
    # value/coordinate pairs and never appear as source times.
    centers = np.asarray(feat["centers"], dtype=np.int64)
    nov_centers_all = np.asarray(feat["novelty_centers"], dtype=np.int64)
    nov_valid = (nov_centers_all >= 0) & (nov_centers_all <= n_src)
    nov_excluded = int(len(nov_centers_all) - int(nov_valid.sum()))
    rms_d, rms_s = downsample_curve_with_samples(feat["rms_n"], centers)
    flux_d, flux_s = downsample_curve_with_samples(feat["flux_n"], centers)
    slow_d, slow_s = downsample_curve_with_samples(feat["slow_n"], centers)
    nov_d, nov_s = downsample_curve_with_samples(
        feat["nov_n"], nov_centers_all, valid=nov_valid)
    return {
        "schema_version": "0.1",
        "asset": rel_audio,
        "input_sha256": input_hash,
        "sample_rate": sr,
        "n_source_samples": n_src,
        "duration_sec_display": round(n_src / sr, 4),
        "analysis_params": params,
        "features_uri": features_uri,
        "features_uri_base": features_uri_base,
        "feature_conventions": {
            "rms": feat["meta"].get("rms_convention"),
            "padding": feat["meta"].get("padding"),
            "chroma_input": feat["meta"].get("chroma_input"),
            "novelty_lag_note": feat["meta"].get("novelty_lag_note"),
            "curve_time_base": (
                "curve_sample_positions.* hold integer SOURCE samples on the "
                "original grid (same extraction index as curves_downsampled.*); "
                "display seconds must be derived as sample/sr per point, never "
                "by spreading curves uniformly over the full duration"
            ),
            "novelty_interval_note": (
                "novelty_start/end_sample bound the smoothed chroma-vector "
                "comparison interval [(i-lag)*hop, i*hop]; the midpoint is the "
                "detection quantity's time coordinate. The interval is the "
                "comparison range of smoothed vectors, NOT a confidence "
                "interval for the true structural boundary"
            ),
        },
        "flat_input_flags": flat,
        "curves_downsampled": {
            "rms_n": rms_d,
            "flux_n": flux_d,
            "slow_n": slow_d,
            "nov_n": nov_d,
        },
        "curve_sample_positions": {
            "rms_n": rms_s,
            "flux_n": flux_s,
            "slow_n": slow_s,
            "nov_n": nov_s,
        },
        "curve_position_validity": {
            "rms_n": "centers (right-edge i*hop)",
            "flux_n": "centers (right-edge i*hop)",
            "slow_n": "centers (right-edge i*hop)",
            "nov_n": (
                "novelty_centers (comparison-interval midpoint); "
                f"{nov_excluded} pre-source (negative) head positions excluded "
                "as value/coordinate pairs; remaining positions are detection "
                "coordinates, not boundary confidence intervals"
            ),
        },
        "sections": [],
        "events": {
            k: [
                {**e, "note": "candidate only; strength is uncalibrated, not a probability"}
                for e in v
            ]
            for k, v in events.items()
        },
        "event_counts": {k: len(v) for k, v in events.items()},
        "caveats": [
            "strength values are uncalibrated ranking scores, not probabilities.",
            "centroid/rolloff/band ratios mix register and timbre; not disambiguated.",
            "chroma novelty marks harmonic-change candidates, not harmony/cadence facts.",
            "downsampled novelty comparison intervals are smoothed-vector comparison ranges, not confidence intervals for true boundaries.",
            "activity peaks are acoustic-activity peaks, not emotion/climax evidence.",
        ],
        "semantic_interpretation": None,
    }


def build_plan_json(
    clip: dict, scenes_info: dict, accents: list[dict], tb,
    seed: int, params: dict, fallback_notes: list[str],
) -> dict:
    s0 = clip["start_sample"]
    return {
        "schema_version": "0.1",
        "seed": seed,
        "timebase": tb.describe(),
        "clip": {
            "start_sample": s0, "end_sample": clip["end_sample"],
            "start_frame": 0, "end_frame": clip["n_frames"],
            "n_frames": clip["n_frames"], "fps": [tb.fps.numerator, tb.fps.denominator],
            "duration_sec_display": round(clip["n_frames"] / float(tb.fps), 4),
            "selection_reason": clip.get("selection_reason", ""),
            "edge_100ms_rms_dbfs": clip.get("edge_100ms_rms_dbfs"),
            "edge_energy_note": clip.get("edge_energy_note"),
        },
        "scenes": [
            {**sc, "palette_id": k % 3, "motion_preset": "slow_parallax",
             "reactivity_profile": "restrained"}
            for k, sc in enumerate(scenes_info["scenes"])
        ],
        "scene_reason": scenes_info["reason"],
        "candidate_cuts": scenes_info["candidate_cuts"],
        "accents": [
            {**e, "frame": int(tb.sample_to_frame(s0, int(e["source_sample"])))}
            for e in accents
        ],
        "accent_policy": {
            "min_gap_sec": ACCENT_MIN_GAP_SEC,
            "strength_floor": ACCENT_STRENGTH_FLOOR,
            "cap_per_clip": ACCENT_CAP,
        },
        "params": params,
        "fallback_notes": fallback_notes,
        "semantic_interpretation": None,
    }


def build_storyboard_json(plan: dict, style_id: str = "original-placeholder-night-window-v1") -> dict:
    return {
        "schema_version": "0.1",
        "clip": plan["clip"],
        "narrative": (
            "Original placeholder imagery (night window, hills, drifting light, "
            "small traveler). No story emotion is claimed from the audio; "
            "visual change follows acoustic candidates only."
        ),
        "style_id": style_id,
        "scenes": [
            {
                "id": sc["id"], "start_frame": sc["start_frame"], "end_frame": sc["end_frame"],
                "narrative_role": "placeholder",
                "asset_type": "procedural_placeholder",
                "motion_preset": sc["motion_preset"],
                "reactivity_profile": sc["reactivity_profile"],
                "transition": "cut",
            }
            for sc in plan["scenes"]
        ],
        "credits": {"note": "validation render: no title or performer is claimed"},
    }

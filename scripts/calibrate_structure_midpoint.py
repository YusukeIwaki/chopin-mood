"""Midpoint calibration: old right-edge vs midpoint representative time.

Synthetic equal-power triad changes (no musical cadence claim). Compares
the previous right-edge timestamp (idx*hop) with the midpoint timestamp
mid=(start+end)//2 (t-lag/2) from the SAME peak frame, so the comparison
isolates the time-coordinate definition (smoothing/polyphony leave the
true structural boundary uncertain).

Centers [4.5, 8.0, 12.0] s deliberately avoid the parent 6.123 s fixture
to prevent overfitting. Lags 0.5/1/2 s, gains 0.5x/2.0x. Negatives:
constant tone, silence, same-chord octave shift (abrupt + crossfade).

Writes output/structure_calibration.json. No audio upload, no paid APIs.
"""
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from p0.features import compute_features
from p0 import events as E

SR = 22050
HOP = 512
CENTERS = [4.5, 8.0, 12.0]
LAGS = [0.5, 1.0, 2.0]
GAINS = [0.5, 2.0]
WIDTH = 0.2
OLD_FREQS = (220.0, 277.1826, 329.6276)
NEW_FREQS = (196.0, 246.9417, 293.6648)


def triad(freqs, n, amp):
    t = np.arange(n) / SR
    y = np.zeros(n)
    for f in freqs:
        y += np.sin(2 * np.pi * f * t) * amp
    return y


def xfade_change(center, gain_mult, dur_sec=18.0):
    n = int(dur_sec * SR)
    t = np.arange(n) / SR
    old = triad(OLD_FREQS, n, 0.12 * gain_mult)
    new = triad(NEW_FREQS, n, 0.12 * gain_mult)
    alpha = np.clip((t - (center - WIDTH / 2)) / WIDTH, 0, 1)
    y = old * np.cos(alpha * np.pi / 2) + new * np.sin(alpha * np.pi / 2)
    return y


def nearest_harm(cands, center):
    harm = [e for e in cands if "harm_novelty" in e["support"]]
    if not harm:
        return None
    return min(harm, key=lambda e: abs(e["source_sample"] / SR - center))


cases = []
for center in CENTERS:
    for lag_sec in LAGS:
        for gain in GAINS:
            y = xfade_change(center, gain)
            feat = compute_features(y, SR, chroma_lag_sec=lag_sec)
            lag_frames = int(feat["meta"]["novelty_lag_frames"])
            hop = int(feat["meta"]["hop"])
            shift = (lag_frames * hop) // 2
            assert hop == HOP, (hop, HOP)
            ev = E.detect_struct_boundaries(feat)
            # All event samples must be integer, non-negative, in range.
            n_src = int(feat["meta"]["n_source_samples"])
            for e in ev:
                assert isinstance(e["source_sample"], int)
                assert 0 <= e["source_sample"] <= n_src, e
                if "novelty_start_sample" in e:
                    s, en = e["novelty_start_sample"], e["novelty_end_sample"]
                    assert isinstance(s, int) and isinstance(en, int)
                    assert 0 <= s <= n_src and 0 <= en <= n_src
                    assert e["source_sample"] == (s + en) // 2 or "slow_edge" in e["support"], e
            det = nearest_harm(ev, center)
            if det is None:
                cases.append({
                    "center_sec": center, "lag_sec": lag_sec, "gain_mult": gain,
                    "lag_frames": lag_frames, "shift_samples": shift,
                    "detected": False,
                })
                continue
            new_t = det["source_sample"] / SR
            # Old right-edge equivalent from the same peak frame:
            # old = mid + shift (exact for even lag*hop; hop=512 even here).
            old_t = (det["source_sample"] + shift) / SR
            cases.append({
                "center_sec": center, "lag_sec": lag_sec, "gain_mult": gain,
                "lag_frames": lag_frames, "shift_samples": shift,
                "shift_sec": round(shift / SR, 6),
                "detected": True,
                "support": det["support"],
                "strength": det["strength"],
                "novelty_start_sample": det.get("novelty_start_sample"),
                "novelty_end_sample": det.get("novelty_end_sample"),
                "source_sample_mid": int(det["source_sample"]),
                "time_new_sec": round(new_t, 6),
                "time_old_sec": round(old_t, 6),
                "err_new_sec": round(new_t - center, 6),
                "err_old_sec": round(old_t - center, 6),
                "abs_err_new_sec": round(abs(new_t - center), 6),
                "abs_err_old_sec": round(abs(old_t - center), 6),
                "raw_novelty_max": round(float(feat["harm_novelty"].max()), 6),
            })

detected = [c for c in cases if c["detected"]]
assert len(detected) == len(cases), f"missed detections: {[c for c in cases if not c['detected']]}"
mean_old = float(np.mean([c["abs_err_old_sec"] for c in detected]))
mean_new = float(np.mean([c["abs_err_new_sec"] for c in detected]))
max_old = float(np.max([c["abs_err_old_sec"] for c in detected]))
max_new = float(np.max([c["abs_err_new_sec"] for c in detected]))
per_lag = {}
for lag_sec in LAGS:
    sub = [c for c in detected if c["lag_sec"] == lag_sec]
    per_lag[str(lag_sec)] = {
        "n": len(sub),
        "mean_abs_old": round(float(np.mean([c["abs_err_old_sec"] for c in sub])), 6),
        "mean_abs_new": round(float(np.mean([c["abs_err_new_sec"] for c in sub])), 6),
        "max_abs_old": round(float(np.max([c["abs_err_old_sec"] for c in sub])), 6),
        "max_abs_new": round(float(np.max([c["abs_err_new_sec"] for c in sub])), 6),
    }

# --- negatives / limitations (same code path, not tuned thresholds) ---
negatives = {}
y_const = triad(OLD_FREQS, SR * 10, 0.12)
f_const = compute_features(y_const, SR)
e_const = E.detect_struct_boundaries(f_const)
negatives["constant_tone"] = {
    "kind": "negative (no pitch-class change)",
    "n_struct": len(e_const),
    "n_harm_support": sum(1 for e in e_const if "harm_novelty" in e["support"]),
    "flat_harm_novelty": bool(f_const["meta"]["flat"]["harm_novelty"]),
    "raw_novelty_max": round(float(f_const["harm_novelty"].max()), 6),
    "note": ("no harm_novelty candidate (pass for the pitch cue); "
             "any slow_edge is a start/end transient, excluded by the 3 s "
             "clip margin in real use, not a phrase boundary"),
}
y_sil = np.zeros(SR * 10)
f_sil = compute_features(y_sil, SR)
e_sil = E.detect_struct_boundaries(f_sil)
negatives["silence"] = {
    "kind": "negative",
    "n_struct": len(e_sil),
    "flat_all": {k: bool(v) for k, v in f_sil["meta"]["flat"].items()},
    "note": "true silence yields no candidates (pass)",
}
n10 = SR * 10
base = triad((261.63, 329.63, 392.00), n10, 0.12)
octv = triad((523.25, 659.25, 783.99), n10, 0.12)
s0 = 5 * SR
y_oct = np.concatenate([base[:s0], octv[s0:]])
f_oct = compute_features(y_oct, SR)
e_oct = E.detect_struct_boundaries(f_oct)
negatives["octave_abrupt_same_pitch_class"] = {
    "kind": "limitation (R-8)",
    "change_sec": 5.0,
    "n_struct": len(e_oct),
    "candidates": [
        {"time_sec": round(e["source_sample"] / SR, 4), "support": e["support"],
         "strength": e["strength"]} for e in e_oct],
    "raw_novelty_max": round(float(f_oct["harm_novelty"].max()), 6),
    "note": ("octave shift (same pitch classes) still yields a harm_novelty "
             "candidate with strength 0.7 despite tiny raw novelty; strength "
             "cannot distinguish it (R-8). Timing itself is close, but the "
             "false-positive class remains."),
}
L = int(0.5 * SR)
w = np.linspace(0, 1, L)
w = w * w * (3 - 2 * w)
a = triad((261.63, 329.63, 392.00), n10, 0.12)
b = triad((523.25, 659.25, 783.99), n10, 0.12)
y_ox = a.copy()
y_ox[s0:s0 + L] = (1 - w) * a[s0:s0 + L] + w * b[s0:s0 + L]
y_ox[s0 + L:] = b[s0 + L:]
f_ox = compute_features(y_ox, SR)
e_ox = E.detect_struct_boundaries(f_ox)
negatives["octave_crossfade_same_pitch_class"] = {
    "kind": "limitation (R-8)",
    "change_interval_sec": [5.0, 5.5],
    "n_struct": len(e_ox),
    "candidates": [
        {"time_sec": round(e["source_sample"] / SR, 4), "support": e["support"],
         "strength": e["strength"]} for e in e_ox],
    "raw_novelty_max": round(float(f_ox["harm_novelty"].max()), 6),
    "note": ("crossfaded octave shift timing is unreliable under the current "
             "peak picker; recorded as a limitation, not a pass"),
}

adopt = (
    len(detected) == len(cases)
    and all(c["abs_err_new_sec"] < c["abs_err_old_sec"] for c in detected)
    and max_new < 0.10
    and mean_new < mean_old / 3
)
decision = {
    "adopt_midpoint": bool(adopt),
    "reason": (
        f"midpoint wins in all {len(detected)} cases (mean|new|={mean_new:.4f}s "
        f"vs mean|old|={mean_old:.4f}s, max|new|={max_new:.4f}s vs "
        f"max|old|={max_old:.4f}s); per-lag table in 'per_lag'. "
        "Adoption is a time-coordinate definition (t-lag/2), not a fixed "
        "'minus 1 s' correction; smoothing/polyphony leave true phrase "
        "boundaries uncertain, and real-performance phrase accuracy is NOT "
        "guaranteed."
        if adopt else
        "midpoint did NOT consistently improve; keeping the old convention "
        "and recording this file as the rejection evidence."
    ),
}

report = {
    "schema_version": "0.1",
    "purpose": ("calibrate chroma-novelty representative time: old right-edge "
                "vs midpoint (t-lag/2); not a musical cadence evaluation"),
    "signal": ("synthetic sustained triad change, equal-power crossfade "
               "width 0.2 s; no claim of phrase cadence"),
    "code_sha256": {
        "p0/features.py": hashlib.sha256((ROOT / "p0/features.py").read_bytes()).hexdigest(),
        "p0/events.py": hashlib.sha256((ROOT / "p0/events.py").read_bytes()).hexdigest(),
    },
    "grid": {"sr": SR, "hop": HOP, "width_sec": WIDTH,
             "centers_sec": CENTERS, "lags_sec": LAGS, "gains": GAINS,
             "note": "centers avoid the parent 6.123 s fixture to prevent overfitting"},
    "cases": cases,
    "summary": {
        "n_cases": len(cases), "n_detected": len(detected),
        "mean_abs_old_sec": round(mean_old, 6), "mean_abs_new_sec": round(mean_new, 6),
        "max_abs_old_sec": round(max_old, 6), "max_abs_new_sec": round(max_new, 6),
        "per_lag": per_lag,
    },
    "negatives": negatives,
    "decision": decision,
    "limitations": [
        "Midpoint improves synthetic triad timing only; real rubato/pedal/polyphony phrase accuracy is NOT established.",
        "Octave-shift (R-8) limitation retained: tiny raw novelty normalizes to the same strength.",
        "Start/end transients can emit slow_edge candidates; real use excludes a 3 s margin.",
        "Volume edges and flux attacks keep the right-edge convention and were NOT shifted.",
    ],
}
out = ROOT / "output/structure_calibration.json"
out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
print(json.dumps(report["summary"], indent=2))
print(json.dumps(report["decision"], indent=2))

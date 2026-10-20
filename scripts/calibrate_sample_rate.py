#!/usr/bin/env python3
"""Synthetic A/B calibration, including uncorrected octave-only false cues.

No recordings, model calls, or manual production timing adjustments.
The known crossfade center is an acoustic reference, not a cadence label.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import librosa
import numpy as np
import scipy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from p0.events import detect_struct_boundaries  # noqa: E402
from p0.features import compute_features  # noqa: E402


def cases():
    old = (220., 277.1826, 329.6276)
    new = (196., 246.9417, 293.6648)
    for sr in (22050, 44100, 48000):
        for center, lag in ((7.349, .75), (10.777, 1.5)):
            for gain in (.07, .28):
                yield dict(group="calibration", sr=sr, center=center, lag=lag,
                           gain=gain, width=.2, seconds=18, old=old, new=new)
    # Additional cases declared before applying the implementation.
    for sr in (22050, 32000, 44100, 48000, 96000):
        for width in (.06, .6):
            for group, before, after in (
                ("additional_triad", (261.6256, 329.6276, 391.9954),
                 (369.9944, 440., 554.3653)),
                ("octave_only", old, tuple(f * 2 for f in old)),
            ):
                yield dict(group=group, sr=sr, center=8.381, lag=2.,
                           gain=.08, width=width, seconds=16, old=before, new=after)


def run() -> dict:
    rows = []
    for case in cases():
        sr = case["sr"]
        t = np.arange(sr * case["seconds"]) / sr
        a = np.clip((t - (case["center"] - case["width"] / 2)) / case["width"], 0, 1)
        before = sum(np.sin(2 * np.pi * f * t) for f in case["old"])
        after = sum(np.sin(2 * np.pi * f * t) for f in case["new"])
        y = case["gain"] * (before * np.cos(a * np.pi / 2)
                            + after * np.sin(a * np.pi / 2))
        for mode in ("fixed_samples", "duration_default"):
            kw = {"hop": 512, "n_fft": 2048} if mode == "fixed_samples" else {}
            begin = time.perf_counter()
            feat = compute_features(y, sr, chroma_lag_sec=case["lag"], **kw)
            events = detect_struct_boundaries(feat)
            near = [e for e in events if "harm_novelty" in e["support"]
                    and abs(e["time_sec"] - case["center"]) < 3]
            chosen = min(near, key=lambda e: abs(e["time_sec"] - case["center"])) if near else None
            rows.append({
                **case, "mode": mode,
                "hop": feat["meta"]["hop"], "n_fft": feat["meta"]["n_fft"],
                "near_count": len(near), "events": events,
                "error_ms": None if chosen is None else
                    (chosen["source_sample"] / sr - case["center"]) * 1000,
                "raw_novelty_peak": float(feat["harm_novelty"].max()),
                "elapsed_sec": round(time.perf_counter() - begin, 6),
            })
    summary = []
    for group in ("calibration", "additional_triad", "octave_only"):
        for mode in ("fixed_samples", "duration_default"):
            subset = [r for r in rows if r["group"] == group and r["mode"] == mode]
            errors = [abs(r["error_ms"]) for r in subset if r["error_ms"] is not None]
            summary.append({
                "group": group, "mode": mode, "cases": len(subset),
                "missing": sum(r["near_count"] == 0 for r in subset),
                "ambiguous": sum(r["near_count"] > 1 for r in subset),
                "near_detections": sum(r["near_count"] for r in subset),
                "max_abs_error_ms": max(errors) if errors else None,
            })
    return {
        "schema_version": "1.0",
        "interpretation": (
            "Synthetic acoustic crossfade centers, not musical phrase truth. "
            "Octave-only changes preserve pitch classes; their detections show "
            "unresolved register sensitivity, not successful harmonic recognition. "
            "Nearest-peak errors are accompanied by missing/ambiguous counts."
        ),
        "versions": {"python": sys.version.split()[0], "numpy": np.__version__,
                     "scipy": scipy.__version__, "librosa": librosa.__version__},
        "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                        for name in ("p0/features.py", "p0/events.py",
                                     "scripts/calibrate_sample_rate.py")},
        "summary": summary, "records": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="output/calibration-sample-rate.json")
    args = parser.parse_args()
    report = run()
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report["summary"], indent=2))
    # Diagnostic report only: no assertion of musical or production acceptance.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

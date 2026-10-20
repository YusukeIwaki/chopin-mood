#!/usr/bin/env python3
"""Trusted DSP analysis for the plan job: WAV -> candidates + scenes.

Decodes nothing itself (input is the lossless WAV from the download
step), computes p0 features/events at the native rate, ranks diverse
60-120 s windows and deterministic per-candidate scenes. Output is
bounded JSON for the planner; no audio leaves this step.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import hashlib
import json
from pathlib import Path

import soundfile as sf

from p0 import clip as clipmod
from p0 import events as eventsmod
from p0 import features as featmod
from p0 import plan as planmod
from p0.timebase import Timebase

#: Scalar feature-meta keys copied into plan-context.json (bounded, no arrays).
_ANALYSIS_META_KEYS = (
    "sr", "n_source_samples", "hop", "n_fft",
    "hop_duration_sec", "window_duration_sec", "reference_sr",
    "default_hop_at_reference", "default_n_fft_at_reference",
    "default_sizing_policy", "hop_is_override", "n_fft_is_override",
    "slow_sec", "chroma_lag_sec", "novelty_lag_frames",
)


def bounded_analysis_meta(feat: dict) -> dict:
    """Bounded scalar analysis config from feat meta (no feature arrays)."""
    meta = feat.get("meta", {})
    out = {k: meta[k] for k in _ANALYSIS_META_KEYS if k in meta}
    if "flat" in meta:
        out["flat"] = dict(meta["flat"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--creative", default="",
                    help="creative.json (piece/period/art_notes) for the planner")
    ap.add_argument("--seed", type=int, default=20261019)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    y, sr = sf.read(a.wav, dtype="float64", always_2d=True)
    y_mono = y.mean(axis=1) if y.shape[1] > 1 else y.ravel()
    tb = Timebase(int(sr), 30, 1)
    feat = featmod.compute_features(y_mono, int(sr))
    events = eventsmod.detect_all(feat)
    ranked = clipmod.rank_excerpt_candidates(
        feat, events, tb, int(y_mono.size), seed=a.seed)
    # Deterministic scenes per candidate (trusted timings for Muse).
    for cand in ranked["candidates"]:
        clip = {"start_sample": cand["start_sample"],
                "end_sample": cand["end_sample"], "n_frames": cand["n_frames"]}
        scenes = clipmod.plan_scenes(clip, events, feat, tb)
        cand["scenes"] = scenes["scenes"]
        cand["scene_reason"] = scenes["reason"]
        cand["accents"] = [
            {**e, "frame": int(tb.sample_to_frame(cand["start_sample"],
                                                  int(e["source_sample"])))}
            for e in planmod.pick_accents(events, clip, int(sr))]
    digest = hashlib.sha256(Path(a.wav).read_bytes()).hexdigest()
    creative = {}
    if a.creative and Path(a.creative).exists():
        creative = json.loads(Path(a.creative).read_text())
    context = {
        "schema_version": "1.0", "source_sha256": digest,
        "creative": creative,
        "analysis": bounded_analysis_meta(feat),
        "sample_rate": int(sr), "n_source_samples": int(y_mono.size),
        "duration_sec": round(y_mono.size / sr, 3),
        "candidates": ranked["candidates"],
        "ranked_total": ranked["ranked_total"],
        "uncertainty": ranked["selection_caveat"],
        "musical_quality": "not demonstrated; DSP ranking only, no listening",
    }
    (out / "candidates.json").write_text(json.dumps(
        {"candidates": ranked["candidates"]}, indent=2))
    (out / "plan-context.json").write_text(json.dumps(context, indent=2))
    print(json.dumps({"candidates": [c["id"] for c in ranked["candidates"]],
                      "duration_sec": context["duration_sec"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

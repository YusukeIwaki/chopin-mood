"""Clip DP: 2D resource-constrained optimum, frame-grid scenes, endpoints.

- Counterexample where a single-path-per-node DP keeps a costly route and
  loses the feasible optimum (MAX_CUTS=2, positions [0,6,12,18,36]s).
- Randomized brute-force cross-check of _dp_cuts on small instances.
- plan_scenes emits frame-snapped cuts and scenes that satisfy the minimum
  length ON FRAMES (no post-hoc sample floor violation).
- select_clip records per-endpoint evidence, basis and concerns.
"""
import itertools
import random

import numpy as np

from p0 import clip as C
from p0 import events as E
from p0 import features as F
from p0.timebase import Timebase

SR = 22050


def brute_cuts(pos, weights, min_len, max_cuts, penalty):
    m = len(pos)
    best, best_set = -1e18, []
    for r in range(0, min(max_cuts, m - 2) + 1):
        for combo in itertools.combinations(range(1, m - 1), r):
            seq = (0,) + combo + (m - 1,)
            if any(pos[seq[k + 1]] - pos[seq[k]] < min_len for k in range(len(seq) - 1)):
                continue
            val = sum(weights[i] - penalty for i in combo)
            if val > best:
                best, best_set = val, list(combo)
    if best <= -1e17:
        return [], best
    return best_set, best


def test_single_path_dp_would_lose_this_case():
    """Old single-dp[node] scheme keeps 0->A->B (value 10.1, 2 cuts) at B,
    which blocks the C cut, and ends at 10.1. The feasible optimum under
    max 2 cuts is {B, C} = 19.9. The 2D DP must find it."""
    pos = [0, 180, 360, 540, 1080]  # 0/6/12/18/36 s @30fps
    w = [0.0, 0.2, 10.0, 10.0, 0.0]
    chosen, value = C._dp_cuts(pos, w, 180, 2, cut_penalty=0.05)
    assert chosen == [2, 3], (chosen, value)
    assert abs(value - (10.0 - 0.05) * 2) < 1e-9
    bset, bval = brute_cuts(pos, w, 180, 2, 0.05)
    assert [pos[i] for i in chosen] == [pos[i] for i in bset]
    assert abs(value - bval) < 1e-9


def test_dp_matches_brute_force_randomized():
    rng = random.Random(1234)
    for trial in range(60):
        m = rng.randint(3, 7)
        steps = sorted(rng.sample(range(60, 900), m - 2))
        pos = [0] + steps + [1000]
        w = [0.0] + [round(rng.uniform(0, 1), 3) for _ in range(m - 2)] + [0.0]
        min_len = rng.choice([60, 120, 180])
        max_cuts = rng.choice([1, 2, 3])
        chosen, value = C._dp_cuts(pos, w, min_len, max_cuts)
        bset, bval = brute_cuts(pos, w, min_len, max_cuts, C.CUT_PENALTY)
        if bval <= -1e17:
            assert value <= -1e17, (trial, pos)
        else:
            assert abs(value - bval) < 1e-9, (trial, pos, w, chosen, bset)


def _synth_events_with_struct(n, sr, struct_times):
    hop = 512
    centers = np.arange(1 + n // hop, dtype=np.int64) * hop
    feat = {
        "slow_n": np.linspace(0.2, 0.8, len(centers)),
        "rms": np.full(len(centers), 0.2),
        "centers": centers,
        "meta": {"sr": sr, "hop": hop,
                 "flat": {"slow_env": False, "harm_novelty": False}},
    }
    events = {"struct_boundary": [
        {"kind": "struct_boundary", "source_sample": int(t * sr),
         "time_sec": t, "strength": 0.9, "method": "synth",
         "support": ["slow_edge", "harm_novelty"]}
        for t in struct_times],
        "dip": [], "accent": [], "activity_peak": []}
    return feat, events


def test_plan_scenes_frame_grid_and_min_length():
    tb = Timebase(SR, 30, 1)
    n = SR * 40
    feat, events = _synth_events_with_struct(n, SR, [10.0, 20.0, 30.0])
    clip = {"start_sample": 0, "end_sample": tb.clip_end_sample(0, 1200),
            "n_frames": 1200}
    out = C.plan_scenes(clip, events, feat, tb)
    assert len(out["scenes"]) >= 2
    for sc in out["scenes"]:
        assert (sc["end_frame"] - sc["start_frame"]) >= 180, sc  # 6 s in frames
        assert tb.frame_boundary_sample(0, sc["start_frame"]) == sc["start_sample"]
    assert out["scenes"][0]["start_frame"] == 0
    assert out["scenes"][-1]["end_frame"] == 1200
    for i in range(len(out["scenes"]) - 1):
        assert out["scenes"][i]["end_frame"] == out["scenes"][i + 1]["start_frame"]


def test_select_clip_records_endpoint_evidence():
    tb = Timebase(SR, 30, 1)
    n = SR * 60
    t = np.arange(n) / SR
    y = 0.2 * np.sin(2 * np.pi * 440 * t)
    y[int(20 * SR):int(21 * SR)] *= 0.05  # a dip near 20 s
    feat = F.compute_features(y, SR)
    events = E.detect_all(feat)
    sel = C.select_clip(feat, events, tb, n)
    best = sel["best"]
    assert 30.0 - 1e-9 <= best["duration_sec"] <= 45.0 + 1e-9
    ev = best["endpoint_evidence"]
    assert set(ev) == {"start", "end"}
    for edge in ("start", "end"):
        assert {"nearest_dip_s", "nearest_struct_s", "nearest_accent_s",
                "edge_rms_ratio"} <= set(ev[edge])
    assert best["endpoint_basis"], "basis must be recorded"
    assert best["endpoint_concerns"], "concerns must be recorded"
    assert "NOT human-evaluated" in best["selection_reason"]

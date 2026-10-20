"""Midpoint time-coordinate regression: novelty interval contract + calibration spot-check.

Locks the task-1 definition: harm_novelty[i] compares
[start(i), end(i)] = [(i-lag)*hop, i*hop]; the event time is the integer
midpoint mid=(start+end)//2 (t-lag/2). Volume edges and flux attacks keep
the right-edge convention and are NOT shifted.
"""
import numpy as np

from p0 import events as E
from p0.features import compute_features

SR = 22050
HOP = 512


def _triad(freqs, n, amp=0.12):
    t = np.arange(n) / SR
    y = np.zeros(n)
    for f in freqs:
        y += np.sin(2 * np.pi * f * t) * amp
    return y


def _xfade(center, lag_sec=2.0, gain=1.0, dur=12.0, width=0.2):
    n = int(dur * SR)
    t = np.arange(n) / SR
    old = _triad((220.0, 277.1826, 329.6276), n, 0.12 * gain)
    new = _triad((196.0, 246.9417, 293.6648), n, 0.12 * gain)
    alpha = np.clip((t - (center - width / 2)) / width, 0, 1)
    return old * np.cos(alpha * np.pi / 2) + new * np.sin(alpha * np.pi / 2)


def test_novelty_interval_midpoint_contract():
    y = _xfade(8.0)
    feat = compute_features(y, SR, chroma_lag_sec=2.0)
    lag = int(feat["meta"]["novelty_lag_frames"])
    hop = int(feat["meta"]["hop"])
    n_src = int(feat["meta"]["n_source_samples"])
    # Curve-level contract: centers are the interval midpoint.
    starts = np.asarray(feat["novelty_start_samples"])
    ends = np.asarray(feat["novelty_end_samples"])
    mids = np.asarray(feat["novelty_centers"])
    assert (ends - starts == lag * hop).all()
    assert ((starts + ends) // 2 == mids).all()
    ev = E.detect_struct_boundaries(feat)
    harm = [e for e in ev if "harm_novelty" in e["support"]]
    assert harm, "pitch-class change left no novelty candidate"
    for e in harm:
        s, en, mid = e["novelty_start_sample"], e["novelty_end_sample"], e["source_sample"]
        assert isinstance(mid, int) and 0 <= mid <= n_src
        assert isinstance(s, int) and isinstance(en, int)
        assert 0 <= s <= n_src and 0 <= en <= n_src
        assert en - s == lag * hop
        assert mid == (s + en) // 2


def test_midpoint_beats_right_edge_on_synthetic():
    center = 8.0
    y = _xfade(center, lag_sec=2.0)
    feat = compute_features(y, SR, chroma_lag_sec=2.0)
    lag = int(feat["meta"]["novelty_lag_frames"])
    shift = (lag * HOP) // 2
    ev = E.detect_struct_boundaries(feat)
    harm = [e for e in ev if "harm_novelty" in e["support"]]
    det = min(harm, key=lambda e: abs(e["source_sample"] / SR - center))
    new_err = det["source_sample"] / SR - center
    old_err = (det["source_sample"] + shift) / SR - center
    assert abs(new_err) < abs(old_err)
    assert abs(new_err) < 0.10


def test_constant_tone_has_no_harm_candidate_and_silence_has_none():
    y_const = _triad((220.0, 277.1826, 329.6276), SR * 10)
    f_const = compute_features(y_const, SR)
    e_const = E.detect_struct_boundaries(f_const)
    assert all("harm_novelty" not in e["support"] for e in e_const)
    f_sil = compute_features(np.zeros(SR * 5), SR)
    assert E.detect_struct_boundaries(f_sil) == []

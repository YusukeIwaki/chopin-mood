"""Same input + seed -> same analysis/plan/controls (MP4 bytes may differ).

Self-contained synthetic signal so a clean checkout (without the ignored
``assets/reference_audio.wav``) passes CI.
"""
import io
import json

import numpy as np

from p0 import clip as C
from p0 import events as E
from p0 import features as F
from p0 import plan as P
from p0.timebase import Timebase

SR = 22050
SEED = 20261019


def synth_signal(seconds: int = 70) -> np.ndarray:
    """Deterministic piano-like synthetic: chords + attacks, no file I/O."""
    n = SR * seconds
    t = np.arange(n) / SR
    y = 0.08 * np.sin(2 * np.pi * 220 * t) + 0.05 * np.sin(2 * np.pi * 330 * t)
    # chord changes + decaying attacks every ~7 s for structure/accents
    for k, start in enumerate(range(3, seconds - 5, 7)):
        s = int(start * SR)
        L = min(int(2.5 * SR), n - s)
        te = np.arange(L) / SR
        freq = 220.0 * (1.25 ** (k % 4))
        y[s:s + L] += 0.35 * np.exp(-te * 3.0) * np.sin(2 * np.pi * freq * te)
        y[s:s + L] += 0.15 * np.exp(-te * 5.0) * np.sin(2 * np.pi * freq * 2 * te)
    # gentle dynamics arc
    env = 0.7 + 0.3 * np.sin(2 * np.pi * t / seconds)
    return y * env


def run_all(y):
    tb = Timebase(SR, 30, 1)
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    sel = C.select_clip(feat, ev, tb, len(y), seed=SEED)
    clip = sel["best"]
    scenes = C.plan_scenes(clip, ev, feat, tb)
    accents = P.pick_accents(ev, clip, SR)
    ctrl = P.build_controls("structured", clip["n_frames"], tb, clip,
                            scenes["scenes"], accents, feat, SEED)
    buf = io.BytesIO()
    np.savez_compressed(buf, **ctrl)
    canon = json.dumps({"clip": clip, "scenes": scenes, "accents": accents,
                        "events": ev}, sort_keys=True, default=str)
    return canon, buf.getvalue()


def test_determinism_on_synth():
    y = synth_signal(70)
    a = run_all(y)
    b = run_all(y.copy())
    assert a[0] == b[0]
    assert a[1] == b[1]

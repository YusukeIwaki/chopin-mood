"""Planned events must reach ENCODED frames (not just plan.json).

Builds a 3 s synthetic clip, renders a short diagnostic MP4, decodes frames
back from the file and reads the 8x8 diagnostic marker. Also locks
render determinism (same frame twice / any order -> identical pixels).
"""
import numpy as np
import soundfile as sf

from p0 import events as E
from p0 import features as F
from p0 import plan as P
from p0 import qa as Q
from p0 import render as R
from p0.timebase import Timebase

SR = 22050
W, H = 180, 320
SEED = 7


def synth_clip(tmp_path):
    t = np.arange(SR * 3) / SR
    y = 0.15 * np.sin(2 * np.pi * 440 * t)
    for a in (1.0, 2.0):
        s = int(a * SR)
        L = int(0.8 * SR)
        te = np.arange(L) / SR
        y[s:s + L] += 0.5 * np.exp(-te * 7) * np.sin(2 * np.pi * 440 * te)
    wav = tmp_path / "synth.wav"
    sf.write(str(wav), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    return y, wav


def test_marker_reaches_encoded_frames(tmp_path):
    y, wav = synth_clip(tmp_path)
    tb = Timebase(SR, 30, 1)
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    n_frames = 90
    clip = {"start_sample": 0, "end_sample": tb.clip_end_sample(0, n_frames),
            "n_frames": n_frames}
    scenes = [{"id": "scene01", "start_sample": 0, "end_sample": clip["end_sample"],
               "start_frame": 0, "end_frame": n_frames, "duration_sec": 3.0}]
    accents = P.pick_accents({"accent": ev["accent"]}, clip, SR)
    assert len(accents) >= 1, "synthetic attacks must yield accents"
    ctrl = P.build_controls("structured", n_frames, tb, clip, scenes, accents,
                            feat, SEED)
    lay = R._layouts(SEED, W, H)
    mp4 = tmp_path / "diag.mp4"
    R.render_video((R.draw_frame(f, ctrl, "structured", W, H, SEED, lay)
                    for f in range(n_frames)),
                   str(wav), str(mp4), W, H, 30, 1, n_frames)
    # accent frame from the FILE must show the accent marker (G high)
    f_acc = tb.sample_to_frame(0, accents[0]["source_sample"])
    px = Q.decode_frame_rgb(str(mp4), f_acc, W, H).astype(int)
    patch = px[H - 12:H - 4, W - 12:W - 4]
    assert patch[..., 1].mean() > 120, patch[..., 1].mean()
    # a quiet frame far from accents must show G low
    f_quiet = 5
    px2 = Q.decode_frame_rgb(str(mp4), f_quiet, W, H).astype(int)
    patch2 = px2[H - 12:H - 4, W - 12:W - 4]
    assert patch2[..., 1].mean() < 90, patch2[..., 1].mean()


def test_render_deterministic_any_order():
    rng = np.random.RandomState(0)
    yy = rng.rand(SR) * 0.1
    tb = Timebase(SR, 30, 1)
    feat = F.compute_features(yy, SR)
    ev = E.detect_all(feat)
    n_frames = 30
    clip = {"start_sample": 0, "end_sample": tb.clip_end_sample(0, n_frames),
            "n_frames": n_frames}
    scenes = [{"id": "scene01", "start_sample": 0, "end_sample": clip["end_sample"],
               "start_frame": 0, "end_frame": n_frames, "duration_sec": 1.0}]
    ctrl = P.build_controls("structured", n_frames, tb, clip, scenes, [], feat, SEED)
    lay = R._layouts(SEED, W, H)
    a = R.draw_frame(17, ctrl, "structured", W, H, SEED, lay)
    b = R.draw_frame(17, ctrl, "structured", W, H, SEED, lay)
    assert np.array_equal(a, b)
    fwd = [R.draw_frame(f, ctrl, "structured", W, H, SEED, lay) for f in range(10)]
    rev = [R.draw_frame(f, ctrl, "structured", W, H, SEED, lay)
           for f in reversed(range(10))]
    for f in range(10):
        assert np.array_equal(fwd[f], rev[9 - f])

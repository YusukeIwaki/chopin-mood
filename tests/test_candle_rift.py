"""Score-driven spatial events must survive encoding and reject timing faults."""
import copy
import json

import numpy as np
import pytest
import soundfile as sf

from p0 import qa, render
from p0.timebase import Timebase
from studio import candle_rift, nocturne_chapel, rendering

W, H, N, MAJOR, OCTAVE, FAST, ACCENT = 180, 320, 240, 60, 130, 160, 210
SEED = 20261019
PLAN = {"clip": {"n_frames": N}, "scenes": [
    {"id": "scene01", "start_frame": 0, "end_frame": MAJOR},
    {"id": "scene02", "start_frame": MAJOR, "end_frame": OCTAVE},
    {"id": "scene03", "start_frame": OCTAVE, "end_frame": N}],
    "accents": [{"frame": ACCENT}], "semantic_landmarks": [
        {"id": name, "frame": frame} for name, frame in
        (("major_arrival", MAJOR), ("both_hands_octaves", OCTAVE), ("fast_coda", FAST))]}


def controls(major=MAJOR, octave=OCTAVE, fast=FAST, accent=ACCENT, n=N):
    c = {"scene_id": np.array([0]*major+[1]*(octave-major)+[2]*(n-octave)),
         "score_fast_frame": fast, "flame_envelope": np.linspace(.05, 1, n),
         "accent_pulse": np.zeros(n), "light": np.full(n, .5),
         "zoom": np.ones(n), "camx": np.full(n, .5), "camy": np.full(n, .5),
         "particle": np.zeros(n)}
    if accent is not None:
        c["accent_pulse"][accent:accent+15] = np.exp(-np.arange(15)/4)
    return c


def reference(c):
    lay = rendering.make_layouts(SEED, W, H)
    lay["candle_rift"] = candle_rift.make_layout(SEED, W, H)
    return rendering.make_reference_frame_fn(c, W, H, SEED,
        scene_palettes=[nocturne_chapel.BASE_PALETTE]*3,
        scene_params=[{}]*3, layouts=lay)


def test_random_access_and_counterfactuals_move_actual_geometry():
    c = controls(); before = copy.deepcopy(c); fn = reference(c)
    late = fn(FAST+10).copy(); fn(0)
    assert np.array_equal(late, reference(c)(FAST+10))
    for k in c:
        assert np.array_equal(c[k], before[k])
    for b in (MAJOR, OCTAVE, FAST):
        deltas = [np.mean(np.abs(fn(f).astype(float)-
                    fn.shifted_boundary_frame(b, 1, f))) for f in range(b, b+12)]
        assert max(deltas) > .02, (b, deltas)


def test_semantic_probe_covers_internal_fast_cue():
    probes = qa.rendered_probe_frames(PLAN, N)
    assert "semantic_landmark" in probes[FAST]
    assert "semantic_landmark_pre" in probes[FAST-1]
    assert "semantic_landmark_post" in probes[FAST+1]


def test_mandatory_semantic_landmark_cannot_silently_skip_timing(monkeypatch):
    frame = np.zeros((H,W,3), np.uint8)
    monkeypatch.setattr(qa, "decode_frame_rgb", lambda *args: frame)
    def fn(f): return frame
    fn.transition_window = lambda b: None
    fn.shifted_boundary_frame = lambda *args: frame
    result = qa.check_rendered_frames("unused", PLAN, Timebase(22050,30,1),
        W,H,N,fn, require_transition_timing=True)
    assert not result["pass"]
    assert len(result["transition_timing"]) == 3
    assert all(t["reason"] == "mandatory semantic landmark has no timing window"
               for t in result["transition_timing"])


def test_candle_strength_is_an_executable_control():
    c=controls();lay=rendering.make_layouts(SEED,W,H)
    lay["candle_rift"]=candle_rift.make_layout(SEED,W,H)
    def draw(gain):
        return rendering.draw_clean_frame(MAJOR+35,c,W,H,SEED,lay,
            [nocturne_chapel.BASE_PALETTE]*3,[{"candle_strength":gain}]*3)
    assert np.mean(np.abs(draw(1).astype(float)-draw(0))) > .2


def test_late_cloud_layer_keeps_the_full_native_frame_covered():
    c=controls(major=944,octave=1637,fast=1729,accent=None,n=3172)
    lay=rendering.make_layouts(SEED,W,H)
    lay["candle_rift"]=candle_rift.make_layout(SEED,W,H)
    params=[{"camera_zoom_start":1.07,"camera_zoom_end":1.07}]*3
    frame=rendering.draw_clean_frame(3171,c,W,H,SEED,lay,
        [nocturne_chapel.BASE_PALETTE]*3,params)
    # This actual painted sky is lit at its right edge. An unfilled affine
    # region previously produced a ruler-straight black strip here.
    assert frame[:int(H*.36),-2:].mean() > 8


@pytest.mark.parametrize("case,boundary", [
    ("good", None), ("late_major", MAJOR), ("late_octave", OCTAVE),
    ("late_fast", FAST), ("missing_accent", None)])
def test_encoded_spatial_events_reject_faults(tmp_path, case, boundary):
    c = controls(major=MAJOR+(case=="late_major"),
                 octave=OCTAVE+(case=="late_octave"),
                 fast=FAST+(case=="late_fast"),
                 accent=None if case=="missing_accent" else ACCENT)
    wav = tmp_path/"tone.wav"
    sf.write(wav, .1*np.sin(np.arange(22050*8)*2*np.pi*440/22050), 22050)
    path = tmp_path/"test.mp4"; actual = reference(c)
    render.render_video((actual(f) for f in range(N)), str(wav), str(path), W, H, 30, 1, N)
    expected = reference(controls())
    if boundary is not None:
        result = qa.check_transition_timing(str(path), boundary,
            expected.transition_window(boundary), W, H, N, expected)
        passed = result["ok"]
    else:
        result = qa.check_rendered_frames(str(path), PLAN, Timebase(22050,30,1),
            W, H, N, expected, require_transition_timing=True)
        passed = result["pass"]
    assert passed == (case=="good"), json.dumps({"case":case,"result":result},indent=2)

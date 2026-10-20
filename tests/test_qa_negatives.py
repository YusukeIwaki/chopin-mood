"""Negative tests: faults the QA must FAIL (anti-false-pass).

- A +3-frame audio delay must fail audio_sync (search window is wider
  than the 1-frame pass threshold, so this is not circular).
- Drift between segments must fail even when the full-clip lag is small.
- True silence on both sides is N/A-pass; one-sided silence fails.
- A +1-frame shifted plan must fail marker verification on a real
  encoded MP4 (onset edges / boundary steps are single-frame precise).
- Frozen background + per-frame grain must fail content_changes through
  the real H264 encoder, while genuinely moving frames pass.
- black_verdict must fail an all-black file and pass intro-only black.
"""
import subprocess

import numpy as np
import soundfile as sf

from p0 import plan as P
from p0 import qa as Q
from p0 import render as R
from p0.timebase import Timebase

SR = 22050
FPS = 30
FRAME = SR // FPS  # 735 samples = 1 video frame here
W, H = 180, 320
SEED = 7


def _tone(n, freq=440.0, amp=0.3):
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * freq * t) * 32767).astype(np.int16)


def _transient_ref():
    """Non-periodic reference (sine + two decaying bursts): lag is unique."""
    n = SR * 3
    t = np.arange(n) / SR
    y = 0.15 * np.sin(2 * np.pi * 440 * t)
    for a in (1.0, 2.0):
        s = int(a * SR)
        L = int(0.5 * SR)
        te = np.arange(L) / SR
        y[s:s + L] += 0.5 * np.exp(-te * 7) * np.sin(2 * np.pi * 660 * te)
    return (y * 32767).astype(np.int16)


def test_three_frame_audio_delay_fails():
    tb = Timebase(SR, FPS, 1)
    ref = _transient_ref()
    dly = np.concatenate([np.zeros(3 * FRAME, dtype=np.int16), ref])[:len(ref)]
    v = Q.qa_audio_verdict(dly, ref, tb, "audio_sync")
    assert v["verdict"] == "measured"
    assert abs(v["full"]["lag_samples"]) == 3 * FRAME, v["full"]
    assert v["pass"] is False
    # and the aligned pair passes (same data, same code path)
    v0 = Q.qa_audio_verdict(ref, ref, tb, "audio_sync")
    assert v0["pass"] is True, v0


def test_segment_drift_fails_despite_small_full_lag():
    tb = Timebase(SR, FPS, 1)
    ref = _tone(SR * 6)
    dec = ref.copy()
    # delay only the last third by 2 frames -> drift across segments
    tail = int(len(ref) * 2 / 3)
    shifted = np.concatenate([np.zeros(2 * FRAME, dtype=np.int16), dec[tail:]])
    dec[tail:tail + len(shifted)] = shifted[:len(dec) - tail]
    v = Q.qa_audio_verdict(dec, ref, tb, "audio_sync")
    assert v["pass"] is False, v
    lags = [s["lag_samples"] for s in v["segments"].values()]
    assert max(lags) - min(lags) > 0, v


def test_true_silence_is_na_not_failure():
    tb = Timebase(SR, FPS, 1)
    z = np.zeros(SR * 2, dtype=np.int16)
    v = Q.qa_audio_verdict(z, z, tb, "audio_sync")
    assert v["pass"] is True and v["verdict"] == "na_silent_both_match", v
    v2 = Q.qa_audio_verdict(z, _tone(SR * 2), tb, "audio_sync")
    assert v2["pass"] is False and v2["verdict"] == "fail_silence_mismatch", v2


def _render_synth_mp4(tmp_path, n_frames=90):
    from p0 import events as E
    from p0 import features as F

    t = np.arange(SR * 3) / SR
    y = 0.15 * np.sin(2 * np.pi * 440 * t)
    for a in (1.0, 2.0):
        s = int(a * SR)
        L = int(0.8 * SR)
        te = np.arange(L) / SR
        y[s:s + L] += 0.5 * np.exp(-te * 7) * np.sin(2 * np.pi * 440 * te)
    wav = tmp_path / "synth.wav"
    sf.write(str(wav), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    tb = Timebase(SR, FPS, 1)
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    clip = {"start_sample": 0, "end_sample": tb.clip_end_sample(0, n_frames),
            "n_frames": n_frames}
    scenes = [{"id": "scene01", "start_sample": 0, "end_sample": clip["end_sample"],
               "start_frame": 0, "end_frame": n_frames // 2, "duration_sec": 1.5},
              {"id": "scene02", "start_sample": tb.frame_boundary_sample(0, n_frames // 2),
               "end_sample": clip["end_sample"], "start_frame": n_frames // 2,
               "end_frame": n_frames, "duration_sec": 1.5}]
    accents = P.pick_accents({"accent": ev["accent"]}, clip, SR)
    assert len(accents) >= 1
    ctrl = P.build_controls("structured", n_frames, tb, clip, scenes, accents,
                            feat, SEED)
    lay = R._layouts(SEED, W, H)
    mp4 = tmp_path / "neg.mp4"
    R.render_video((R.draw_frame(f, ctrl, "structured", W, H, SEED, lay)
                    for f in range(n_frames)),
                   str(wav), str(mp4), W, H, FPS, 1, n_frames)
    plan = {"clip": {"n_frames": n_frames}, "scenes": scenes,
            "accents": [{**a, "frame": int(tb.sample_to_frame(0, a["source_sample"]))}
                        for a in accents]}
    return str(mp4), plan, tb


def test_marker_pass_and_one_frame_shift_fails(tmp_path):
    mp4, plan, tb = _render_synth_mp4(tmp_path)
    n_frames = plan["clip"]["n_frames"]
    good = Q.check_markers(mp4, plan, tb, W, H, n_frames)
    assert good["pass"] is True, good
    # shift every planned frame by +1: onset/boundary edges must catch it
    import copy

    bad_plan = copy.deepcopy(plan)
    for sc in bad_plan["scenes"]:
        sc["start_frame"] = min(n_frames - 1, sc["start_frame"] + 1)
    for ac in bad_plan["accents"]:
        ac["frame"] = min(n_frames - 1, ac["frame"] + 1)
    bad = Q.check_markers(mp4, bad_plan, tb, W, H, n_frames)
    assert bad["pass"] is False, bad


def _encode_frames(tmp_path, name, frames, wav):
    mp4 = tmp_path / name
    R.render_video(iter(frames), str(wav), str(mp4), W, H, FPS, 1, len(frames))
    return str(mp4)


def test_frozen_grain_fails_but_motion_passes(tmp_path):
    y = np.zeros(SR, dtype=np.float64)
    wav = tmp_path / "sil.wav"
    sf.write(str(wav), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    rng = np.random.RandomState(0)
    base = (rng.rand(H, W, 3) * 40 + 10)
    grng = np.random.RandomState(99)
    frozen = [np.clip(base + grng.randint(-2, 3, size=(H, W, 1)),
                      0, 255).astype(np.uint8) for _ in range(30)]
    mp4f = _encode_frames(tmp_path, "frozen.mp4", frozen, wav)
    vf = Q.content_verdict(mp4f, 30, W, H)
    assert vf["pass"] is False, vf
    # genuinely moving frames (slow pan of the same base) must pass
    moving = [np.roll(base, shift=k * 4, axis=1).astype(np.uint8) for k in range(30)]
    mp4m = _encode_frames(tmp_path, "moving.mp4", moving, wav)
    vm = Q.content_verdict(mp4m, 30, W, H)
    assert vm["pass"] is True, vm


def test_black_verdict_all_black_fails_intro_only_passes():
    all_black = [{"black_start": 0.0, "black_end": 35.0, "black_duration": 35.0}]
    v = Q.black_verdict(all_black)
    assert v["pass"] is False, v
    intro = [{"black_start": 0.0, "black_end": 0.4, "black_duration": 0.4}]
    assert Q.black_verdict(intro)["pass"] is True
    late = [{"black_start": 10.0, "black_end": 10.5, "black_duration": 0.5}]
    assert Q.black_verdict(late)["pass"] is False

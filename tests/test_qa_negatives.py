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


# --- Encoded-AAC format regressions through check_file() -----------------
# Reference crops may be PCM16, PCM24 or FLOAT WAV (compressed sources
# decode to 32-bit FLOAT). Reading a FLOAT crop as int16 casts values in
# [-1, 1] to zero and falsely reports fail_silence_mismatch.

_QA_W, _QA_H = 64, 64
_QA_N_FRAMES = 90


def _float_waveform(n):
    t = np.arange(n) / SR
    y = 0.15 * np.sin(2 * np.pi * 440 * t)
    for a in (1.0, 2.0):
        s = int(a * SR)
        L = int(0.5 * SR)
        te = np.arange(L) / SR
        y[s:s + L] += 0.5 * np.exp(-te * 7) * np.sin(2 * np.pi * 660 * te)
    return y


def _small_frames(n_frames):
    for f in range(n_frames):
        img = np.zeros((_QA_H, _QA_W, 3), dtype=np.uint8)
        img[..., 0] = ((np.arange(_QA_W)[None, :] + f * 4) % 256).astype(np.uint8)
        img[..., 1] = ((np.arange(_QA_H)[:, None] + f * 2) % 256).astype(np.uint8)
        img[..., 2] = 128
        yield img


def _encoded_audio_sync(workdir, subtype, y, tag, encode_y=None):
    import hashlib
    from pathlib import Path

    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    n = len(y)
    asset = workdir / f"asset_{tag}.wav"
    clip = workdir / f"clip_{tag}.wav"
    sf.write(str(asset), y, SR, subtype=subtype)
    sf.write(str(clip), y, SR, subtype=subtype)
    encode_src = clip
    if encode_y is not None:
        encode_src = workdir / f"encode_{tag}.wav"
        sf.write(str(encode_src), encode_y, SR, subtype="PCM_16")
    mp4 = workdir / f"video_{tag}.mp4"
    R.render_video(_small_frames(_QA_N_FRAMES), str(encode_src), str(mp4),
                   _QA_W, _QA_H, FPS, 1, _QA_N_FRAMES)
    tb = Timebase(SR, FPS, 1)
    plan = {
        "_asset_abspath": str(asset),
        "clip": {"n_frames": _QA_N_FRAMES, "start_sample": 0, "end_sample": n},
        "scenes": [{"id": "scene01", "start_frame": 0,
                    "end_frame": _QA_N_FRAMES, "start_sample": 0,
                    "end_sample": n}],
        "accents": [],
    }
    digest = hashlib.sha256(Path(asset).read_bytes()).hexdigest()
    res = Q.check_file(str(mp4), str(clip), plan, tb, _QA_W, _QA_H, digest,
                       str(workdir), tag)
    matches = [c for c in res["checks"] if c["name"] == f"{tag}:audio_sync"]
    assert len(matches) == 1, res["checks"]
    return matches[0]


def test_encoded_alignment_passes_for_pcm16_pcm24_float(tmp_path):
    y = _float_waveform(SR * 3)
    for subtype in ("PCM_16", "PCM_24", "FLOAT"):
        tag = subtype.lower()
        audio = _encoded_audio_sync(tmp_path / tag, subtype, y, tag)
        assert audio["pass"] is True, (subtype, audio)
        assert audio["verdict"] == "measured", (subtype, audio)
        assert audio["full"]["ref_rms"] > 1e-3, (subtype, audio)
        assert audio["full"]["dec_rms"] > 1e-3, (subtype, audio)
        assert abs(audio["full"]["lag_samples"]) <= FRAME, (subtype, audio)
        assert audio["full"]["peak_corr"] >= 0.90, (subtype, audio)
        assert abs(audio["full"]["level_diff_db"]) <= 1.0, (subtype, audio)


def test_delayed_aac_fails_audio_sync(tmp_path):
    y = _float_waveform(SR * 3)
    delayed = np.concatenate([np.zeros(3 * FRAME, dtype=np.float64), y])[:len(y)]
    audio = _encoded_audio_sync(tmp_path, "PCM_16", y, "delayed",
                                encode_y=delayed)
    assert audio["pass"] is False, audio
    assert audio["verdict"] == "measured", audio
    assert audio["full"]["lag_samples"] == 3 * FRAME, audio


def test_true_silence_float_reference_passes_as_na(tmp_path):
    audio = _encoded_audio_sync(tmp_path, "FLOAT", np.zeros(SR * 3), "silence")
    assert audio["pass"] is True, audio
    assert audio["verdict"] == "na_silent_both_match", audio


def test_one_sided_silence_float_reference_fails(tmp_path):
    y = _float_waveform(SR * 3)
    audio = _encoded_audio_sync(tmp_path, "FLOAT", y, "onesided",
                                encode_y=np.zeros(SR * 3))
    assert audio["pass"] is False, audio
    assert audio["verdict"] == "fail_silence_mismatch", audio


def _direct_lag_corr(decoded, ref, max_lag):
    a = decoded.astype(np.float64).copy()
    b = ref.astype(np.float64).copy()
    a -= a.mean()
    b -= b.mean()
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    corr = np.correlate(
        a, np.concatenate([np.zeros(max_lag), b, np.zeros(max_lag)]),
        mode="valid")
    lags = np.arange(-max_lag, max_lag + 1)
    k = int(np.argmax(corr))
    denom = float(np.sqrt((a * a).sum() * (b * b).sum())) + 1e-12
    return int(lags[k]), float(corr[k] / denom)


def test_audio_lag_corr_fft_matches_direct_sign_and_bounds():
    rng = np.random.RandomState(42)
    ref = rng.randn(SR)
    for shift in (-2 * FRAME, -37, 0, 37, FRAME, 3 * FRAME):
        dec = np.zeros_like(ref)
        for i in range(len(ref)):
            j = i - shift
            if 0 <= j < len(ref):
                dec[i] = ref[j]
        got = Q.audio_lag_corr(dec, ref, 4096)
        exp_lag, exp_peak = _direct_lag_corr(dec, ref, 4096)
        assert got["lag_samples"] == exp_lag == shift, (shift, got, exp_lag)
        assert abs(got["peak_corr"] - round(exp_peak, 4)) <= 1e-3, (shift, got)

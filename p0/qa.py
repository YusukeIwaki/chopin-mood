"""File-level QA on real encoded outputs (never plan.json alone).

Checks (each recorded pass/fail with numbers):
  ffprobe per stream: video AND audio stream start_time and duration are
    checked SEPARATELY against the expected clip (n_frames/fps, tolerance
    +-1.5 frames for durations, 1 video frame for start times). No
    "AAC priming" allowance: container timestamps are the reference.
  audio sync: decode MP4 audio, cross-correlate against the source crop.
    The SEARCH window (4096 samples) is deliberately WIDER than the PASS
    threshold (1 video frame, e.g. 735 samples @22050 Hz/30 fps), so the
    check cannot pass circularly. Full-clip lag plus head/mid/tail segment
    lags (drift) must each be within 1 frame; peak correlation >= 0.90;
    |level difference| <= 1.0 dB. AAC is lossy: bit-exactness is never
    demanded. TRUE silence on both sides is an explicit N/A (both-silent
    match passes; one-sided silence fails) instead of a correlation 0 stop.
  crop PCM exactness: the clip WAV must equal the source interval
    (bit-exact for integer PCM subtypes; tight LSB tolerance otherwise).
    The analysis mono downmix must never leak into the output crop.
  black frames: ffmpeg blackdetect over the whole file. Black is allowed
    only inside the first 0.5 s intro; any black ending after that fails,
    INCLUDING a file that is black from t=0 (black_start=0 with a long
    duration is the all-black case, not an exemption).
  content change: central-crop (captions/diagnostic marker excluded),
    blurred + downscaled frame differences. Threshold is calibrated so a
    frozen background with only per-frame grain FAILS while quiet but
    genuinely moving placeholders pass (see tests/test_qa_negatives.py).
  scene invariants: sorted, min length, no gaps, no overlaps, exact frame
    coverage [0, n_frames).
  input hash: source WAV sha256 unchanged vs manifest.
  marker verification: planned accent ONSETS (G low at f-1, high at f) and
    scene BOUNDARIES (R changes exactly at f) are re-read from DECODED
    file frames, including the f-1/f+1 neighbours. A 1-frame plan shift
    fails these edge checks (proven by tests/test_qa_negatives.py). The
    B channel (frame_index % 256) pins coarse frame identity and catches
    large shifts / dropped frames; +-1-frame precision comes from the
    onset/boundary edges, not from B.
"""
from __future__ import annotations

import json
import subprocess

import numpy as np

from .features import SILENCE_RMS

CORR_MIN = 0.90
LEVEL_MAX_DB = 1.0
DUR_TOL_FRAMES = 1.5
SEARCH_LAG_SAMPLES = 4096
BLACK_INTRO_ALLOW_SEC = 0.5
#: motion floor on the blurred/downscaled central crop (calibrated: real
#: placeholder motion scores >> 1, frozen+grain scores << 0.3; see tests).
CONTENT_MOTION_MIN = 0.4
MARKER_R_TOL = 60
MARKER_B_TOL = 12
ACCENT_G_HIGH = 120
ACCENT_G_LOW = 90


def lag_pass_samples(tb) -> int:
    """Independent pass threshold: 1 video frame in source samples (ceil)."""
    spf = tb.samples_per_frame
    return (spf.numerator + spf.denominator - 1) // spf.denominator


def ffprobe(path: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-show_format", "-show_streams",
           "-count_frames", "-of", "json", path]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def decode_audio_wav(mp4: str, out_wav: str, sr: int) -> None:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", mp4, "-map", "0:a",
           "-ac", "1", "-ar", str(sr), "-c:a", "pcm_f32le", out_wav]
    subprocess.run(cmd, check=True)


def decode_frame_rgb(mp4: str, frame_idx: int, w: int, h: int) -> np.ndarray:
    cmd = ["ffmpeg", "-v", "error", "-i", mp4, "-vf", f"select=eq(n\\,{frame_idx})",
           "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    arr = np.frombuffer(out, dtype=np.uint8)
    return arr.reshape(h, w, 3)


def audio_lag_corr(decoded: np.ndarray, ref: np.ndarray, max_lag: int) -> dict:
    from scipy import signal as _sig

    a = decoded.astype(np.float64)
    b = ref.astype(np.float64)
    a -= a.mean()
    b -= b.mean()
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    corr = _sig.correlate(
        a, np.concatenate([np.zeros(max_lag), b, np.zeros(max_lag)]),
        mode="valid", method="fft")
    lags = np.arange(-max_lag, max_lag + 1)
    k = int(np.argmax(corr))
    denom = float(np.sqrt((a * a).sum() * (b * b).sum())) + 1e-12
    return {
        "lag_samples": int(lags[k]),
        "peak_corr": round(float(corr[k] / denom), 4),
        "level_diff_db": round(float(20 * np.log10(
            (np.sqrt((a * a).mean()) + 1e-9) / (np.sqrt((b * b).mean()) + 1e-9))), 3),
        "ref_rms": round(float(np.sqrt((b * b).mean())), 6),
        "dec_rms": round(float(np.sqrt((a * a).mean())), 6),
    }


def _mono(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x)
    return x.mean(axis=1) if x.ndim > 1 else x.ravel()


def qa_audio_verdict(dec: np.ndarray, ref: np.ndarray, tb, tag: str) -> dict:
    """Full + segmented lag verdict with silence-aware N/A handling."""
    dec_m, ref_m = _mono(dec), _mono(ref)
    m = min(len(dec_m), len(ref_m))
    dec_m, ref_m = dec_m[:m], ref_m[:m]
    ref_rms = float(np.sqrt((ref_m.astype(np.float64) ** 2).mean()))
    dec_rms = float(np.sqrt((dec_m.astype(np.float64) ** 2).mean()))
    lag_pass = lag_pass_samples(tb)
    search = max(SEARCH_LAG_SAMPLES, 4 * lag_pass)
    if ref_rms < SILENCE_RMS and dec_rms < SILENCE_RMS:
        return {"name": tag, "pass": True, "verdict": "na_silent_both_match",
                "ref_rms": ref_rms, "dec_rms": dec_rms,
                "note": "true silence: correlation undefined, silence-match only"}
    if (ref_rms < SILENCE_RMS) != (dec_rms < SILENCE_RMS):
        return {"name": tag, "pass": False, "verdict": "fail_silence_mismatch",
                "ref_rms": ref_rms, "dec_rms": dec_rms}
    full = audio_lag_corr(dec_m, ref_m, search)
    segs = {}
    for name, (a, b) in (("head", (0.0, 1 / 3)), ("mid", (1 / 3, 2 / 3)),
                         ("tail", (2 / 3, 1.0))):
        i0, i1 = int(m * a), int(m * b)
        s = audio_lag_corr(dec_m[i0:i1], ref_m[i0:i1], search)
        segs[name] = {"lag_samples": s["lag_samples"], "peak_corr": s["peak_corr"]}
    ok = (
        abs(full["lag_samples"]) <= lag_pass
        and all(abs(s["lag_samples"]) <= lag_pass for s in segs.values())
        and full["peak_corr"] >= CORR_MIN
        and abs(full["level_diff_db"]) <= LEVEL_MAX_DB
    )
    drift = max(s["lag_samples"] for s in segs.values()) - min(
        s["lag_samples"] for s in segs.values())
    return {"name": tag, "pass": bool(ok), "verdict": "measured",
            "lag_pass_samples": lag_pass, "search_samples": search,
            "full": full, "segments": segs, "segment_drift_samples": int(drift)}


def blackdetect(mp4: str) -> list[dict]:
    cmd = ["ffmpeg", "-v", "info", "-i", mp4, "-vf",
           "blackdetect=d=0.2:pix_th=0.10", "-an", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    hits = []
    for line in r.stderr.splitlines():
        if "black_start" in line:
            parts = dict(p.split(":") for p in line.split() if ":" in p and "black" in p)
            hits.append({k: float(v) for k, v in parts.items()})
    return hits


def black_verdict(hits: list[dict]) -> dict:
    """Black allowed only inside the intro; anything ending later fails.

    A file black from t=0 has black_end == duration >> intro: it fails
    (the old black_start > 0.5 filter wrongly exempted exactly this case).
    """
    bad = [x for x in hits if x.get("black_end", 0) > BLACK_INTRO_ALLOW_SEC + 0.05]
    beyond = round(sum(max(0.0, x.get("black_end", 0) - max(
        x.get("black_start", 0), BLACK_INTRO_ALLOW_SEC)) for x in hits), 3)
    return {"pass": len(bad) == 0, "black_segments": hits[:5],
            "black_sec_beyond_intro": beyond}


def _motion_frames(frames: list[np.ndarray]) -> list[float]:
    """Blurred/downscaled central-crop differences (marker/captions cut out)."""
    from scipy import ndimage as ndi

    vals = []
    prev = None
    for img in frames:
        h, w = img.shape[:2]
        crop = img[int(0.10 * h):int(0.85 * h), :, :].astype(np.float64).mean(axis=2)
        blur = ndi.gaussian_filter(crop, sigma=2.0)
        # Trim to a multiple of 4 in both axes: arbitrary frame sizes
        # (e.g. width 290) must not break the block-mean reshape.
        bh, bw = (blur.shape[0] // 4) * 4, (blur.shape[1] // 4) * 4
        if bh == 0 or bw == 0:
            small = blur
        else:
            small = blur[:bh, :bw].reshape(bh // 4, 4, bw // 4, 4).mean(axis=(1, 3))
        if prev is not None:
            vals.append(round(float(np.abs(small - prev).mean()), 4))
        prev = small
    return vals


def content_verdict(mp4: str, n_frames: int, w: int, h: int) -> dict:
    probes = sorted(set([0, n_frames // 3, 2 * n_frames // 3, n_frames - 1]))
    imgs = [decode_frame_rgb(mp4, f, w, h) for f in probes]
    diffs = _motion_frames([im.astype(np.float64) for im in imgs])
    raw = [round(float(np.abs(imgs[i + 1].astype(np.float64)
                              - imgs[i].astype(np.float64)).mean()), 3)
           for i in range(len(imgs) - 1)]
    return {"pass": bool(diffs) and all(d > CONTENT_MOTION_MIN for d in diffs),
            "probe_frames": probes, "motion_diffs_blur_downscaled": diffs,
            "motion_floor": CONTENT_MOTION_MIN, "mean_abs_diffs_raw": raw}


def read_marker(px: np.ndarray, w: int, h: int) -> list[int]:
    patch = px[h - 12:h - 4, w - 12:w - 4].astype(int)
    return [int(patch[..., c].mean()) for c in range(3)]


def check_markers(mp4: str, plan: dict, tb, w: int, h: int, n_frames: int) -> dict:
    """Verify planned events on DECODED frames incl. f-1/f+1 neighbours."""
    marks = []
    accent_frames = sorted({int(ac["frame"]) for ac in plan.get("accents", [])})
    # scene starts: R identity + exact boundary step at f (for k > 0)
    for k, sc in enumerate(plan["scenes"]):
        f = int(sc["start_frame"])
        if not (0 <= f < n_frames):
            continue
        exp_r = (k * 67) % 256
        px = decode_frame_rgb(mp4, f, w, h)
        rgb = read_marker(px, w, h)
        dev_r = abs(rgb[0] - exp_r)
        dev_b = abs(((rgb[2] - f) % 256 + 128) % 256 - 128)
        ok = dev_r < MARKER_R_TOL and dev_b <= MARKER_B_TOL
        note = ""
        if k > 0 and f - 1 >= 0:
            px_pre = decode_frame_rgb(mp4, f - 1, w, h)
            rgb_pre = read_marker(px_pre, w, h)
            exp_prev = ((k - 1) * 67) % 256
            step_ok = (abs(rgb_pre[0] - exp_prev) < MARKER_R_TOL
                       and abs(rgb[0] - exp_r) < MARKER_R_TOL
                       and abs(rgb_pre[0] - rgb[0]) > 30)
            ok = ok and step_ok
            note = f"boundary_step_R {rgb_pre[0]}->{rgb[0]}"
        marks.append({"frame": f, "kind": "scene_start", "expect_R": exp_r,
                      "marker_rgb": rgb, "dev_R": dev_r, "dev_B": dev_b,
                      "ok": bool(ok), "note": note})
    # accent onsets: single-frame edge G(f-1) low -> G(f) high
    for fidx in accent_frames[:6]:
        if not (0 <= fidx < n_frames):
            continue
        px = decode_frame_rgb(mp4, fidx, w, h)
        rgb = read_marker(px, w, h)
        dev_b = abs(((rgb[2] - fidx) % 256 + 128) % 256 - 128)
        if fidx - 1 >= 0:
            rgb_pre = read_marker(decode_frame_rgb(mp4, fidx - 1, w, h), w, h)
            ok = rgb_pre[1] < ACCENT_G_LOW and rgb[1] > ACCENT_G_HIGH and dev_b <= MARKER_B_TOL
            note = f"onset_edge_G {rgb_pre[1]}->{rgb[1]}"
        else:
            ok = rgb[1] > ACCENT_G_HIGH
            note = "no pre-frame (accent at 0)"
        marks.append({"frame": fidx, "kind": "accent_onset", "expect_accent": True,
                      "marker_rgb": rgb, "dev_B": dev_b, "ok": bool(ok), "note": note})
    devs = [m.get("dev_R", 0) for m in marks] + [m.get("dev_B", 0) for m in marks]
    return {"pass": bool(marks) and all(m["ok"] for m in marks),
            "markers": marks[:12],
            "quantization": {"max_abs_dev": int(max(devs)) if devs else None,
                             "n_probed": len(marks)}}


def check_crop_exact(asset_path: str, clip_wav: str, s0: int, s1: int) -> dict:
    """The output crop must equal the source interval (no downmix leakage)."""
    import soundfile as sf

    info = sf.info(asset_path)
    if info.subtype == "PCM_16":
        src, _ = sf.read(asset_path, dtype="int16", start=s0, stop=s1, always_2d=True)
        cw, _ = sf.read(clip_wav, dtype="int16", always_2d=True)
        if src.shape != cw.shape:
            return {"pass": False, "mode": "pcm16_exact",
                    "src_shape": list(src.shape), "crop_shape": list(cw.shape)}
        diff = np.abs(src.astype(np.int32) - cw.astype(np.int32))
        return {"pass": bool((diff == 0).all()), "mode": "pcm16_exact",
                "max_abs_diff_lsb": int(diff.max()) if diff.size else None}
    src, _ = sf.read(asset_path, dtype="float64", start=s0, stop=s1, always_2d=True)
    cw, _ = sf.read(clip_wav, dtype="float64", always_2d=True)
    if src.shape != cw.shape:
        return {"pass": False, "mode": "float_tight",
                "src_shape": list(src.shape), "crop_shape": list(cw.shape)}
    md = float(np.abs(src - cw).max()) if src.size else 0.0
    return {"pass": bool(md <= 2.0 ** -15), "mode": "float_tight",
            "max_abs_diff": md}


def check_file(mp4: str, clip_wav: str, plan: dict, tb, w: int, h: int,
               input_hash: str, workdir: str, tag: str) -> dict:
    """Run all checks for one MP4. Returns check dicts (no exceptions)."""
    import soundfile as sf

    checks: list[dict] = []

    def rec(name: str, ok: bool, **kw):
        checks.append({"name": f"{tag}:{name}", "pass": bool(ok), **kw})

    n_frames = plan["clip"]["n_frames"]
    fps_f = float(tb.fps)
    expected_dur = n_frames / fps_f
    frame_sec = 1.0 / fps_f
    try:
        pb = ffprobe(mp4)
        v = next(s for s in pb["streams"] if s["codec_type"] == "video")
        a = next(s for s in pb["streams"] if s["codec_type"] == "audio")
        nb = int(v.get("nb_read_frames", n_frames))
        rec("stream_kinds", True, video_codec=v.get("codec_name"),
            audio_codec=a.get("codec_name"), w=v.get("width"), h=v.get("height"))
        rec("frame_count", nb == n_frames, nb_read_frames=nb, expected=n_frames)
        for s, nm in ((v, "video"), (a, "audio")):
            try:
                sdur = float(s.get("duration", pb["format"]["duration"]))
            except (KeyError, TypeError, ValueError):
                sdur = float(pb["format"]["duration"])
            start = float(s.get("start_time", 0))
            rec(f"{nm}_stream",
                abs(sdur - expected_dur) <= DUR_TOL_FRAMES / fps_f
                and abs(start) <= frame_sec + 1e-6,
                start_time=round(start, 6), duration=round(sdur, 4),
                expected_duration=round(expected_dur, 4),
                start_tol_1_frame=round(frame_sec, 6),
                dur_tol_frames=DUR_TOL_FRAMES)
    except Exception as e:  # noqa: BLE001
        rec("ffprobe", False, error=str(e)[:300])
        return {"checks": checks, "pass": False}

    try:
        dec_wav = f"{workdir}/qa_dec_{tag}.wav"
        decode_audio_wav(mp4, dec_wav, tb.sr)
        dec, _ = sf.read(dec_wav, dtype="float64", always_2d=True)
        ref, _ = sf.read(clip_wav, dtype="float64", always_2d=True)
        m = min(len(dec), len(ref))
        verdict = qa_audio_verdict(dec[:m], ref[:m], tb, "audio_sync")
        rec("audio_sync", verdict["pass"], **{k: v for k, v in verdict.items()
                                              if k not in ("name", "pass")})
    except Exception as e:  # noqa: BLE001
        rec("audio_sync", False, error=str(e)[:300])

    try:
        crop = check_crop_exact(
            plan["_asset_abspath"], clip_wav,
            plan["clip"]["start_sample"], plan["clip"]["end_sample"])
        rec("crop_pcm_exact", crop.pop("pass"),
            **{k: v for k, v in crop.items()})
    except Exception as e:  # noqa: BLE001
        rec("crop_pcm_exact", False, error=str(e)[:300])

    try:
        bv = black_verdict(blackdetect(mp4))
        rec("no_black_frames", bv.pop("pass"),
            **{k: v for k, v in bv.items()})
    except Exception as e:  # noqa: BLE001
        rec("no_black_frames", False, error=str(e)[:300])

    try:
        cv = content_verdict(mp4, n_frames, w, h)
        rec("content_changes", cv["pass"],
            **{k: v for k, v in cv.items() if k != "pass"})
    except Exception as e:  # noqa: BLE001
        rec("content_changes", False, error=str(e)[:300])

    try:
        scs = plan["scenes"]
        cov_ok = scs and scs[0]["start_frame"] == 0 and scs[-1]["end_frame"] == n_frames
        order_ok = all(
            scs[i]["end_frame"] == scs[i + 1]["start_frame"]
            and scs[i]["start_frame"] < scs[i]["end_frame"]
            for i in range(len(scs) - 1)
        ) if len(scs) > 1 else True
        min_ok = all(
            (s["end_frame"] - s["start_frame"]) / fps_f >= 6.0 - 1e-9 for s in scs
        )
        rec("scene_invariants", bool(cov_ok and order_ok and min_ok),
            n_scenes=len(scs), coverage_ok=bool(cov_ok), contiguous_ok=bool(order_ok),
            min_len_ok=bool(min_ok))
    except Exception as e:  # noqa: BLE001
        rec("scene_invariants", False, error=str(e)[:300])

    try:
        import hashlib

        digest = hashlib.sha256(open(plan["_asset_abspath"], "rb").read()).hexdigest()
        rec("input_hash_unchanged", digest == input_hash, sha256=digest[:16] + "...")
    except Exception as e:  # noqa: BLE001
        rec("input_hash_unchanged", False, error=str(e)[:300])

    try:
        mk = check_markers(mp4, plan, tb, w, h, n_frames)
        rec("marker_spotcheck", mk["pass"],
            **{k: v for k, v in mk.items() if k != "pass"})
    except Exception as e:  # noqa: BLE001
        import traceback

        rec("marker_spotcheck", False, error=(str(e) + " | " + traceback.format_exc())[-600:])

    return {"checks": checks, "pass": all(c["pass"] for c in checks)}

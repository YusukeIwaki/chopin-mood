"""One-command P0: analyze -> clip -> plan -> 2x MP4 -> QA -> timeline viz.

Usage (from repo root):
    python -m p0.pipeline [--audio assets/reference_audio.wav]
                          [--out output] [--seed 20261019]
                          [--width 540] [--height 960]
                          [--fps-num 30] [--fps-den 1]
                          [--skip-tests]

Exit code is nonzero when any QA check, pytest, crop-exactness check, or
post-hoc manifest re-verification fails. Low-confidence situations fall
back explicitly (single scene / fewer accents) with reasons in plan.json;
a fallback is not a failure. True silence is a valid input: correlation
is N/A there (silence-match only), the render goes dim but never black,
and the pipeline still passes its gates.

Original audio is never modified. The output crop preserves the source
subtype/channels bit-exactly (integer PCM) and is verified sample-by-sample
against the source interval; the analysis mono downmix never leaks into it.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import numpy as np

from . import clip as clipmod
from . import events as eventsmod
from . import features as featmod
from . import plan as planmod
from . import qa as qamod
from . import render as rendermod
from . import viz as vizmod
from .timebase import Timebase

#: sha256 of assets/reference_audio.wav; the legacy old-script audit runs
#: ONLY for this input. Any other --audio records the audit inapplicable.
REFERENCE_SHA256 = "a4c2e99c8881cfee5677137081443fab5159ed6367a5288bb717fba1c20cd15e"


class Logger:
    def __init__(self, path: Path):
        self.f = open(path, "w")

    def log(self, msg: str):
        print(msg, flush=True)
        self.f.write(msg + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def repo_relative(p: Path) -> tuple[str, str]:
    """(uri, base): repo-relative path when inside ROOT, else plain name."""
    try:
        return str(p.relative_to(ROOT)), "repo"
    except ValueError:
        return p.name, "out_dir"


def write_crop_lossless(audio_path: Path, clip_wav: Path, s0: int, s1: int,
                        sr: int) -> dict:
    """Cut [s0, s1) preserving subtype and channels (no downmix, no gain).

    Integer PCM subtypes round-trip bit-exactly; float subtypes are written
    back with the original subtype. Returns a description for the manifest.
    """
    import soundfile as sf

    info = sf.info(str(audio_path))
    if info.subtype == "PCM_16":
        raw, _ = sf.read(str(audio_path), dtype="int16", start=s0, stop=s1,
                         always_2d=True)
        sf.write(str(clip_wav), raw, sr, subtype="PCM_16")
        mode = "pcm16_int16_roundtrip"
    elif info.subtype in ("PCM_24", "PCM_32"):
        raw, _ = sf.read(str(audio_path), dtype="int32", start=s0, stop=s1,
                         always_2d=True)
        sf.write(str(clip_wav), raw, sr, subtype=info.subtype)
        mode = "pcm_int32_roundtrip"
    else:
        raw, _ = sf.read(str(audio_path), dtype="float64", start=s0, stop=s1,
                         always_2d=True)
        sf.write(str(clip_wav), raw, sr, subtype=info.subtype)
        mode = f"float64_write_subtype_{info.subtype}"
    return {"mode": mode, "subtype": info.subtype, "channels": info.channels,
            "n_samples": int(s1 - s0)}


def edge_100ms_rms_dbfs(y_mono: np.ndarray, sr: int, s0: int, s1: int) -> dict:
    """Original-source 100 ms RMS around both clip edges (no redesign).

    Windows are taken from the analysis mono downmix (same signal the
    parent audit measured): [s0-0.1s, s0), [s0, s0+0.1s),
    [s1-0.1s, s1), [s1, s1+0.1s), clipped to the source range.
    dBFS = 20*log10(max(rms, 1e-12)). Low energy is NOT a cadence or
    phrase-end annotation; excerpt naturalness is unevaluated by humans.
    No clip time is hardcoded.
    """
    import math as _math

    n = int(y_mono.size)
    win = int(sr // 10)

    def _rms(a: np.ndarray) -> float:
        return float(np.sqrt(np.mean(a * a))) if a.size else 0.0

    def _db(r: float) -> float:
        return round(20.0 * _math.log10(max(r, 1e-12)), 2)

    segs = {
        "start_pre": y_mono[max(0, s0 - win):s0],
        "start_post": y_mono[s0:min(n, s0 + win)],
        "end_pre": y_mono[max(0, s1 - win):s1],
        "end_post": y_mono[s1:min(n, s1 + win)],
    }
    out = {k: {"rms_dbfs": _db(_rms(v)), "n_samples": int(v.size)} for k, v in segs.items()}
    out["note"] = ("100 ms edge energy only; not a cadence/phrase-end "
                   "annotation; excerpt naturalness unevaluated by humans")
    return out


def synth_probe() -> dict:
    """Independent synthetic measurement recorded into qa.json.

    Impulse + piano-like attacks at known SOURCE samples: detection error
    in samples (method must be ~hop, separated from hop/2 grid quantization).
    """
    sr, hop = 22050, 512
    out: dict = {"hop": hop, "sr": sr}
    y = np.zeros(sr * 4)
    y[2 * sr] = 1.0
    feat = featmod.compute_features(y, sr)
    ev = eventsmod.detect_all(feat)
    det = min(ev["accent"], key=lambda e: abs(e["source_sample"] - 2 * sr))
    out["impulse_err_samples"] = int(abs(det["source_sample"] - 2 * sr))
    out["impulse_method"] = det["method"]
    n = sr * 4
    t = np.arange(n) / sr
    y2 = 0.05 * np.sin(2 * np.pi * 440 * t)
    errs = []
    for a in (1.0, 2.5):
        s = int(a * sr)
        L = min(int(1.2 * sr), n - s)
        te = np.arange(L) / sr
        y2[s:s + L] += 0.5 * np.exp(-te * 6.0) * np.sin(2 * np.pi * 440 * te)
    feat2 = featmod.compute_features(y2, sr)
    ev2 = eventsmod.detect_all(feat2)
    for a in (1.0, 2.5):
        d = min(ev2["accent"], key=lambda e: abs(e["source_sample"] - int(a * sr)))
        errs.append(int(abs(d["source_sample"] - int(a * sr))))
    out["piano_attack_err_samples"] = errs
    out["hop_grid_quantization_bound_samples"] = hop // 2
    return out


def canonical_bundle(feat, events, sel, scenes_info, accents, ctrl) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **ctrl)
    canon = json.dumps({"clip": sel["best"], "scenes": scenes_info,
                        "accents": accents, "events": events},
                       sort_keys=True, default=str)
    return canon.encode() + b"\n" + buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", default="assets/reference_audio.wav")
    ap.add_argument("--out", default="output")
    ap.add_argument("--seed", type=int, default=20261019)
    ap.add_argument("--width", type=int, default=540)
    ap.add_argument("--height", type=int, default=960)
    ap.add_argument("--fps-num", type=int, default=30)
    ap.add_argument("--fps-den", type=int, default=1)
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()

    audio_given = Path(args.audio)
    audio_path = audio_given if audio_given.is_absolute() else ROOT / audio_given
    assert audio_path.exists(), f"missing input {audio_path}"
    try:
        audio_rel = str(audio_path.relative_to(ROOT))
        audio_base = "repo"
    except ValueError:
        audio_rel, audio_base = audio_path.name, "out_dir"

    out_given = Path(args.out)
    out = out_given if out_given.is_absolute() else ROOT / out_given
    out.mkdir(parents=True, exist_ok=True)
    log = Logger(out / "run.log")
    stage_t: dict[str, float] = {}

    def stage(name: str):
        t0 = time.perf_counter()
        log.log(f"== stage: {name} ==")

        def done():
            stage_t[name] = round(time.perf_counter() - t0, 2)
            log.log(f"   ({stage_t[name]} s)")

        return done

    rerun_cmd = (
        f"python -m p0.pipeline --audio {args.audio} --out {args.out} "
        f"--seed {args.seed} --width {args.width} --height {args.height} "
        f"--fps-num {args.fps_num} --fps-den {args.fps_den}"
        + (" --skip-tests" if args.skip_tests else "")
    )

    # ---- 1. environment + input measurement ----
    d = stage("env_and_input")
    import librosa, scipy, matplotlib, PIL

    versions = {
        "python": sys.version.split()[0],
        "numpy": np.__version__, "scipy": scipy.__version__,
        "librosa": librosa.__version__, "matplotlib": matplotlib.__version__,
        "Pillow": PIL.__version__,
        "ffmpeg": subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0],
        "ffprobe": subprocess.run(["ffprobe", "-version"], capture_output=True, text=True).stdout.splitlines()[0],
        "ffmpeg_buildconf": subprocess.run(
            ["ffmpeg", "-buildconf"], capture_output=True, text=True).stdout,
    }
    log.log("versions: " + json.dumps({k: v for k, v in versions.items()
                                       if k != "ffmpeg_buildconf"}, ensure_ascii=False))
    import soundfile as sf

    info = sf.info(str(audio_path))
    # analysis-only mono downmix (never used for the output crop)
    y, sr_file = sf.read(str(audio_path), dtype="float64", always_2d=True)
    y_mono = y.mean(axis=1) if y.shape[1] > 1 else y.ravel()
    y64 = np.ascontiguousarray(y_mono, dtype=np.float64)
    input_meas = {
        "audio_given": args.audio,
        "path_repo_relative": audio_rel if audio_base == "repo" else None,
        "path_base": audio_base,
        "note_outside_repo": ("input is outside the repo; no absolute path is "
                              "stored, identity is the sha256 below"
                              if audio_base != "repo" else None),
        "sha256": sha256_file(audio_path),
        "sample_rate": info.samplerate, "channels": info.channels,
        "subtype": info.subtype,
        "n_samples": int(info.frames),
        "duration_sec_display": round(info.frames / info.samplerate, 6),
        "peak_abs": round(float(np.max(np.abs(y64))), 6),
        "mean": float(np.mean(y64)),
        "rms_all": round(float(np.sqrt(np.mean(y64 ** 2))), 6),
    }
    log.log("input: " + json.dumps(input_meas, ensure_ascii=False))
    d()

    # ---- 2. legacy audit: reference input only, old files untouched ----
    d = stage("legacy_audit")
    if input_meas["sha256"] == REFERENCE_SHA256:
        recomputed = out / "legacy_recomputed.json"
        r = subprocess.run(
            [sys.executable, str(ROOT / "scripts/analyze_reference.py"),
             "--audio", str(audio_path), "--out", str(recomputed)],
            capture_output=True, text=True, cwd=str(ROOT),
        )
        log.log(r.stdout[-1500:])
        if r.returncode != 0:
            log.log("legacy script FAILED: " + r.stderr[-1000:])
        ref = json.loads((ROOT / "examples/reference_features_v0.json").read_text())
        new = json.loads(recomputed.read_text())
        peak_match = ref["top_peaks"] == new["top_peaks"]
        maxd = 0.0
        for a, b in zip(ref["windows_5sec"], new["windows_5sec"]):
            for k in ("rms", "onsets", "centroid", "activity"):
                maxd = max(maxd, abs(a[k] - b[k]))
        audit = {
            "applicable": True,
            "legacy_script": "scripts/analyze_reference.py (unmodified)",
            "reference": "examples/reference_features_v0.json (unmodified)",
            "recomputed": repo_relative(recomputed)[0],
            "versions": {k: v for k, v in versions.items() if k != "ffmpeg_buildconf"},
            "peak_order_time_match": peak_match,
            "reference_peaks": ref["top_peaks"], "recomputed_peaks": new["top_peaks"],
            "max_abs_5sec_window_diff": maxd,
            "duration_note": (
                f"legacy prints len(y)/sr at 11025 Hz = {new['duration']}; "
                f"true file duration = {input_meas['duration_sec_display']} s; "
                "46 us difference comes from librosa resampling, not from the file."
            ),
            "legacy_returncode": r.returncode,
        }
    else:
        audit = {
            "applicable": False,
            "reason": ("input sha256 does not match the known reference audio; "
                       "the legacy old-script comparison is defined only for "
                       "assets/reference_audio.wav and is not a quality gate "
                       "for other inputs"),
            "input_sha256": input_meas["sha256"],
            "reference_sha256": REFERENCE_SHA256,
        }
        log.log("legacy_audit: inapplicable to this input (recorded, not a failure)")
    (out / "legacy_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    d()

    # ---- 3. features + events ----
    d = stage("features_events")
    tb = Timebase(info.samplerate, args.fps_num, args.fps_den)
    sr = info.samplerate
    feat = featmod.compute_features(y64, sr)
    featmod.save_features_npz(str(out / "features.npz"), feat)
    events = eventsmod.detect_all(feat)
    log.log("events: " + json.dumps({k: len(v) for k, v in events.items()})
            + f" flat={feat['meta']['flat']}")
    d()

    # ---- 4. clip + scenes + accents + controls ----
    d = stage("clip_plan_controls")
    sel = clipmod.select_clip(feat, events, tb, int(info.frames), seed=args.seed)
    clip = sel["best"]
    clip["edge_100ms_rms_dbfs"] = edge_100ms_rms_dbfs(
        y64, sr, int(clip["start_sample"]), int(clip["end_sample"]))
    clip["edge_energy_note"] = ("clip-edge 100 ms RMS recorded; low energy is not "
                                "a cadence/phrase-end annotation; excerpt naturalness "
                                "unevaluated by humans")
    log.log(f"clip: [{clip['start_sample']}, {clip['end_sample']}) "
            f"n_frames={clip['n_frames']} score={clip['score']}")
    log.log("edge_100ms_rms_dbfs: " + json.dumps(clip["edge_100ms_rms_dbfs"], ensure_ascii=False))
    log.log("selection_reason: " + clip["selection_reason"])
    log.log("endpoint_basis: " + clip.get("endpoint_basis", "?"))
    for c in clip.get("endpoint_concerns", []):
        log.log("endpoint_concern: " + c)
    scenes_info = clipmod.plan_scenes(clip, events, feat, tb)
    log.log(f"scenes: {len(scenes_info['scenes'])} ({scenes_info['reason']})")
    accents = planmod.pick_accents(events, clip, sr)
    log.log(f"accents kept: {len(accents)} of {len(events['accent'])}")
    fallback_notes: list[str] = []
    if len(scenes_info["scenes"]) <= 1:
        fallback_notes.append("single-scene fallback: " + scenes_info["reason"])
    if not accents:
        fallback_notes.append("no accents passed the strength floor; accent layer off")

    params = {"seed": args.seed, "width": args.width, "height": args.height,
              "analysis_hop": int(feat["meta"]["hop"]),
              "analysis_nfft": int(feat["meta"]["n_fft"]),
              "light_floor": planmod.LIGHT_FLOOR}
    features_uri, features_base = repo_relative(out / "features.npz")
    analysis = planmod.build_analysis_json(audio_rel, input_meas["sha256"], feat,
                                           events, int(info.frames), sr, params,
                                           features_uri, features_base)
    analysis["clip_selection"] = {
        "best": clip, "ranked": sel["ranked"][:12],
        "rejected_sample": sel["rejected_sample"],
        "caveat": ("the chosen excerpt maximizes the acoustic grid score; "
                   "its musical naturalness is NOT human-evaluated"),
    }
    plan = planmod.build_plan_json(clip, scenes_info, accents, tb, args.seed,
                                   params, fallback_notes)
    plan["cuts_planned"] = scenes_info["cuts_source_samples"]
    plan["_asset_abspath"] = str(audio_path)
    storyboard = planmod.build_storyboard_json(plan)

    naive_scenes = [{"id": "scene01", "start_sample": clip["start_sample"],
                     "end_sample": clip["end_sample"], "start_frame": 0,
                     "end_frame": clip["n_frames"],
                     "duration_sec": round(clip["n_frames"] / float(tb.fps), 4)}]
    ctrl_s = planmod.build_controls("structured", clip["n_frames"], tb, clip,
                                    scenes_info["scenes"], accents, feat, args.seed)
    ctrl_n = planmod.build_controls("naive", clip["n_frames"], tb, clip,
                                    naive_scenes, [], feat, args.seed)
    np.savez_compressed(out / "control_structured.npz", **ctrl_s)
    np.savez_compressed(out / "control_naive.npz", **ctrl_n)
    plan_n = {**plan, "scenes": [
        {**s, "palette_id": 0, "motion_preset": "slow_parallax",
         "reactivity_profile": "rms_direct"} for s in naive_scenes],
        "accents": [], "cuts_planned": [],
        "scene_reason": "naive baseline: single scene, no cuts"}
    (out / "analysis.json").write_text(json.dumps(analysis, ensure_ascii=False, indent=2))
    (out / "plan.json").write_text(
        json.dumps({k: v for k, v in plan.items() if not k.startswith("_")},
                   ensure_ascii=False, indent=2))
    (out / "plan_naive.json").write_text(
        json.dumps({k: v for k, v in plan_n.items() if not k.startswith("_")},
                   ensure_ascii=False, indent=2))
    (out / "storyboard.json").write_text(json.dumps(storyboard, ensure_ascii=False, indent=2))
    d()

    # ---- 4b. reproducibility: recompute analysis/plan/controls, compare ----
    d = stage("recompute_check")
    feat2 = featmod.compute_features(y64, sr)
    events2 = eventsmod.detect_all(feat2)
    sel2 = clipmod.select_clip(feat2, events2, tb, int(info.frames), seed=args.seed)
    sel2["best"]["edge_100ms_rms_dbfs"] = edge_100ms_rms_dbfs(
        y64, sr, int(sel2["best"]["start_sample"]), int(sel2["best"]["end_sample"]))
    sel2["best"]["edge_energy_note"] = sel["best"].get(
        "edge_energy_note",
        ("clip-edge 100 ms RMS recorded; low energy is not "
         "a cadence/phrase-end annotation; excerpt naturalness "
         "unevaluated by humans"))
    scenes2 = clipmod.plan_scenes(sel2["best"], events2, feat2, tb)
    accents2 = planmod.pick_accents(events2, sel2["best"], sr)
    ctrl2 = planmod.build_controls("structured", sel2["best"]["n_frames"], tb,
                                   sel2["best"], scenes2["scenes"], accents2,
                                   feat2, args.seed)
    bundle1 = canonical_bundle(feat, events, sel, scenes_info, accents, ctrl_s)
    bundle2 = canonical_bundle(feat2, events2, sel2, scenes2, accents2, ctrl2)
    recompute_match = hashlib.sha256(bundle1).hexdigest() == hashlib.sha256(bundle2).hexdigest()
    log.log(f"recompute identical: {recompute_match} "
            f"(analysis+plan+controls twice on the same input; MP4 bytes excluded)")
    d()

    # ---- 5. render both MP4s ----
    d = stage("render")
    clip_wav = out / "clip_audio.wav"
    crop_info = write_crop_lossless(audio_path, clip_wav,
                                    clip["start_sample"], clip["end_sample"], sr)
    log.log(f"clip wav: {crop_info} (output-only derived artifact)")
    lay = rendermod._layouts(args.seed, args.width, args.height)
    for mode, ctrl in (("structured", ctrl_s), ("naive", ctrl_n)):
        mp4 = out / f"poc_{mode}.mp4"

        def gen(m=mode, c=ctrl):
            for f in range(clip["n_frames"]):
                yield rendermod.draw_frame(f, c, m, args.width, args.height,
                                           args.seed, lay)

        stat = rendermod.render_video(gen(), str(clip_wav), str(mp4),
                                      args.width, args.height,
                                      args.fps_num, args.fps_den, clip["n_frames"])
        log.log(f"{mode}: {stat['frames_written']} frames -> {mp4.name}")
    thumbs = out / "thumbs"
    thumbs.mkdir(exist_ok=True)
    for mode in ("structured", "naive"):
        for frac, name in ((0.05, "head"), (0.5, "mid"), (0.95, "tail")):
            fidx = min(clip["n_frames"] - 1, int(clip["n_frames"] * frac))
            subprocess.run(
                ["ffmpeg", "-y", "-v", "error", "-i", str(out / f"poc_{mode}.mp4"),
                 "-vf", f"select=eq(n\\,{fidx})", "-frames:v", "1",
                 str(thumbs / f"{mode}_{name}_f{fidx}.png")],
                check=True,
            )
    d()

    # ---- 6. QA on real files + pytest + synth probe ----
    d = stage("qa")
    qa_all: dict = {"checks": [], "pytest": {}, "pass": True}
    for mode, plan_for in (("structured", plan), ("naive", plan_n)):
        res = qamod.check_file(str(out / f"poc_{mode}.mp4"), str(clip_wav), plan_for,
                               tb, args.width, args.height,
                               input_meas["sha256"], str(out), mode)
        qa_all["checks"].extend(res["checks"])
        log.log(f"qa {mode}: {'PASS' if res['pass'] else 'FAIL'}")
        for c in res["checks"]:
            if not c["pass"]:
                log.log(f"   FAIL {c['name']}: {json.dumps(c, ensure_ascii=False)[:300]}")
    if not args.skip_tests:
        r = subprocess.run([sys.executable, "-m", "pytest", "tests", "-q"],
                           capture_output=True, text=True, cwd=str(ROOT))
        qa_all["pytest"] = {"returncode": r.returncode,
                            "tail": (r.stdout + r.stderr)[-2000:]}
        log.log("pytest returncode=%d" % r.returncode)
        log.log(qa_all["pytest"]["tail"][-1200:])
    else:
        qa_all["pytest"] = {"returncode": None, "tail": "skipped"}
    qa_all["reproducibility"] = {
        "recompute_match": recompute_match,
        "scope": ("analysis+plan+controls computed twice on the same input; "
                  "MP4 bytes excluded (codec-dependent)"),
    }
    qa_all["synth_probe"] = synth_probe()
    qa_all["crop_write"] = crop_info
    qa_all["pass"] = all(c["pass"] for c in qa_all["checks"]) and (
        qa_all["pytest"]["returncode"] == 0 if not args.skip_tests else True)
    qa_all["manual_timeline_edits"] = 0
    qa_all["musical_naturalness"] = (
        "NOT verified: file completion and machine QA do not certify musical "
        "naturalness; human blind comparison is pending (see docs/07)."
    )
    (out / "qa.json").write_text(json.dumps(qa_all, ensure_ascii=False, indent=2))
    d()

    # ---- 7. timeline viz + cost + manifest (manifest LAST, then verify) ----
    d = stage("viz_cost_manifest")
    vizmod.write_timeline_png(str(out / "timeline.png"), feat, events, plan, sel, sr)
    vizmod.write_timeline_html(
        str(out / "timeline.html"),
        {"structured": "poc_structured.mp4", "naive": "poc_naive.mp4"},
        str(out / "timeline.png"), feat, events,
        {"structured": plan, "naive": plan_n}, sr)
    cost = {
        "generation_api_calls": 0, "generation_api_usd": 0.0,
        "muse_development_cost_usd": "unknown (not metered separately)",
        "electricity": "unmeasured",
        "notes": ("P0 used no paid generation APIs. Dev-model cost is tracked "
                  "outside this repo; power was not measured."),
    }
    (out / "cost.json").write_text(json.dumps(cost, ensure_ascii=False, indent=2))
    code_files = (sorted((ROOT / "p0").glob("*.py"))
                  + sorted((ROOT / "tests").glob("test_*.py"))
                  + [ROOT / "scripts/analyze_reference.py",
                     ROOT / "requirements-p0.txt"])
    manifest = {
        "command_rerun": rerun_cmd,
        "argv": sys.argv,
        "seed": args.seed,
        "input": input_meas,
        "code_sha256": {str(p.relative_to(ROOT)): sha256_file(p)
                        for p in code_files if p.exists()},
        "versions": {k: v for k, v in versions.items() if k != "ffmpeg_buildconf"},
        "ffmpeg_buildconf": versions["ffmpeg_buildconf"],
        "timebase": tb.describe(),
        "params": params,
        "stage_seconds": stage_t,
        "outputs_excluded_from_hash": ["run_manifest.json", "run.log",
                                       "manifest_verify.json"],
        "outputs": {},
    }
    for p in sorted(out.iterdir()):
        if p.is_file() and p.name not in ("run_manifest.json", "run.log",
                                          "manifest_verify.json"):
            manifest["outputs"][p.name] = {"bytes": p.stat().st_size,
                                           "sha256": sha256_file(p)}
    (out / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    log.log("PIPELINE " + ("PASS" if qa_all["pass"] else "FAIL"))
    log.close()

    # ---- 8. post-hoc re-verification (after log close, manifest excluded) ----
    saved = json.loads((out / "run_manifest.json").read_text())
    mismatches = [name for name, rec in saved["outputs"].items()
                  if not (out / name).exists()
                  or sha256_file(out / name) != rec["sha256"]]
    code_mismatches = [name for name, h in saved["code_sha256"].items()
                       if not (ROOT / name).exists()
                       or sha256_file(ROOT / name) != h]
    verify = {"verified": not mismatches and not code_mismatches,
              "output_mismatches": mismatches,
              "code_mismatches": code_mismatches,
              "n_outputs_checked": len(saved["outputs"]),
              "note": ("every recorded output+code hash re-read after the run; "
                       "run_manifest.json/run.log/this file hash themselves, "
                       "so they are excluded by construction and listed above")}
    (out / "manifest_verify.json").write_text(json.dumps(verify, ensure_ascii=False, indent=2))
    print("MANIFEST_VERIFY " + ("PASS" if verify["verified"] else
                                f"FAIL {mismatches} {code_mismatches}"))

    ok = qa_all["pass"] and verify["verified"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

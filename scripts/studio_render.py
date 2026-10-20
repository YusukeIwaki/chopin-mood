#!/usr/bin/env python3
"""Trusted renderer: brief + declarative storyboard -> MP4 + QA bundle.

Only this controller code renders. Muse contributes validated JSON;
palettes/params from the storyboard measurably steer the procedural
render (sky/moon/hill colours, figure scale, particles, light, glow).
Output is marked a procedural prototype, not finished anime.
"""
from __future__ import annotations
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from p0 import clip as clipmod
from p0 import events as eventsmod
from p0 import features as featmod
from p0 import plan as planmod
from p0 import qa as qamod
from p0 import render as rendermod
from p0.pipeline import write_crop_lossless
from p0.timebase import Timebase
from studio import storyboard as sbmod
from studio import artifacts as artmod


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True, help="lossless decoded source WAV")
    ap.add_argument("--candidate", required=True, help="candidate JSON file")
    ap.add_argument("--storyboard", required=True)
    ap.add_argument("--brief", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--width", type=int, default=540)
    ap.add_argument("--height", type=int, default=960)
    ap.add_argument("--seed", type=int, default=20261019)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cand = json.loads(Path(a.candidate).read_text())
    if "candidate" in cand:  # brief-wrapped form
        cand = cand["candidate"]
    sb = json.loads(Path(a.storyboard).read_text())
    brief = json.loads(Path(a.brief).read_text())

    y, sr = sf.read(a.wav, dtype="float64", always_2d=True)
    y_mono = y.mean(axis=1) if y.shape[1] > 1 else y.ravel()
    tb = Timebase(int(sr), 30, 1)
    feat = featmod.compute_features(y_mono, int(sr))
    events = eventsmod.detect_all(feat)
    clip = {"start_sample": int(cand["start_sample"]),
            "end_sample": int(cand["end_sample"]),
            "n_frames": int(cand["n_frames"]),
            "selection_reason": "DSP candidate selected by ID; no LLM timing",
            "edge_100ms_rms_dbfs": None, "edge_energy_note": "studio render"}
    det_scenes = cand.get("scenes") or clipmod.plan_scenes(
        clip, events, feat, tb)["scenes"]
    v = sbmod.validate_storyboard(sb, det_scenes)
    if not v["ok"]:
        print(json.dumps({"ok": False, "failures": v["failures"]}))
        return 2
    accents = planmod.pick_accents(events, clip, int(sr))
    params = {"seed": a.seed, "width": a.width, "height": a.height,
              "analysis_hop": int(feat["meta"]["hop"]),
              "analysis_nfft": int(feat["meta"]["n_fft"]),
              "light_floor": planmod.LIGHT_FLOOR}
    plan = planmod.build_plan_json(clip, {"scenes": det_scenes,
                                          "reason": cand.get("scene_reason", "deterministic"),
                                          "candidate_cuts": []},
                                   accents, tb, a.seed, params, [])
    plan["_asset_abspath"] = str(Path(a.wav).resolve())
    plan["storyboard_style"] = sb.get("style_id")
    palettes = [s["palette"] for s in sb["scenes"]]
    sparams = [s.get("params", {}) for s in sb["scenes"]]
    ctrl = planmod.build_controls("structured", clip["n_frames"], tb, clip,
                                  det_scenes, accents, feat, a.seed)
    # Lossless crop of the ORIGINAL decoded stream (no gain/fade/stretch).
    clip_wav = out / "clip_audio.wav"
    write_crop_lossless(Path(a.wav), clip_wav, clip["start_sample"],
                        clip["end_sample"], int(sr))
    lay = rendermod._layouts(a.seed, a.width, a.height)
    mp4 = out / "video.mp4"
    rendermod.render_video(
        (rendermod.draw_frame(f, ctrl, "structured", a.width, a.height,
                              a.seed, lay, scene_palettes=palettes,
                              scene_params=sparams)
         for f in range(clip["n_frames"])),
        str(clip_wav), str(mp4), a.width, a.height, 30, 1, clip["n_frames"])
    # Show every scene to the visual reviewer, including endpoints.
    frames = []
    sample_frames = {
        min(clip["n_frames"] - 1, int(clip["n_frames"] * frac))
        for frac in (0.05, 0.5, 0.95)
    }
    sample_frames.update((int(s["start_frame"]) + int(s["end_frame"]) - 1) // 2
                         for s in det_scenes)
    for fidx in sorted(sample_frames):
        png = out / f"cs_{fidx}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                        "-vf", f"select=eq(n\\,{fidx})", "-frames:v", "1",
                        str(png)], check=True)
        frames.append(png)
    from PIL import Image
    imgs = [Image.open(p).resize((270, 480)) for p in frames]
    columns = min(3, len(imgs))
    sheet = Image.new("RGB", (270 * columns, 480 * ((len(imgs) + columns - 1) // columns)))
    for index, im in enumerate(imgs):
        sheet.paste(im, ((index % columns) * 270, (index // columns) * 480))
    sheet.save(out / "contact-sheet.jpg", quality=88)
    for p in frames:
        p.unlink()
    # Encoded-media QA on the real file.
    qa = qamod.check_file(str(mp4), str(clip_wav), plan, tb, a.width,
                          a.height, sha256_file(Path(a.wav)), str(out),
                          "studio")
    timeline = {"schema_version": "1.0", "candidate_id": cand["id"],
                "clip": plan["clip"], "scenes": plan["scenes"],
                "accents": plan["accents"],
                "note": "deterministic DSP timings; no manual edits"}
    (out / "storyboard.json").write_text(json.dumps(sb, indent=2))
    (out / "brief.json").write_text(json.dumps(brief, indent=2))
    (out / "timeline.html").write_text(
        artmod.build_timeline_html(timeline,
                                   title=f"Studio timeline: {cand['id']}"),
        encoding="utf-8")
    files = {"video.mp4": mp4, "plan.json": out / "plan.json",
             "qa.json": out / "qa.json", "storyboard.json": out / "storyboard.json",
             "brief.json": out / "brief.json",
             "timeline.json": out / "timeline.json",
             "timeline.html": out / "timeline.html",
             "contact-sheet.jpg": out / "contact-sheet.jpg"}
    (out / "plan.json").write_text(json.dumps(
        {k: v for k, v in plan.items() if not k.startswith("_")}, indent=2))
    (out / "timeline.json").write_text(json.dumps(timeline, indent=2))
    (out / "qa.json").write_text(json.dumps(
        {"checks": qa["checks"], "pass": qa["pass"],
         "musical_naturalness": "NOT verified (no listening)"}, indent=2))
    prov = {"schema_version": "1.0", "candidate_id": cand["id"],
            "child_issue": brief.get("child_issue"),
            "parent_issue": brief.get("parent_issue"),
            "source_sha256": sha256_file(Path(a.wav)),
            "source_hashes": brief.get("source_hashes"),
            "planning_fingerprint": brief.get("fingerprint"),
            "brief_fingerprint": brief.get("fingerprint"),
            "prototype": "procedural prototype, not finished anime"}
    # Approving a commit must approve these exact media bytes, even if a
    # later rerender uses identical plans but a different encoder version.
    prov["media_sha256"] = {
        name: sha256_file(out / name)
        for name in ("video.mp4", "contact-sheet.jpg", "timeline.html")
    }
    (out / "provenance.json").write_text(json.dumps(prov, indent=2))
    files["provenance.json"] = out / "provenance.json"
    hashes = {name: sha256_file(p) for name, p in files.items()}
    (out / "hashes.json").write_text(json.dumps(hashes, indent=2))
    # Intermediate audio must not leak into the artifact allowlist.
    for extra in (clip_wav, out / "qa_dec_studio.wav"):
        if Path(extra).exists():
            Path(extra).unlink()
    print(json.dumps({"ok": qa["pass"], "qa_pass": qa["pass"],
                      "hashes": hashes}))
    return 0 if qa["pass"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

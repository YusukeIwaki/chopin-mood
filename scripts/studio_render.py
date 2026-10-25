#!/usr/bin/env python3
"""Trusted renderer: brief + declarative storyboard -> MP4 + QA bundle.

Only this controller code renders. Muse contributes validated JSON;
palettes/params from the storyboard measurably steer the clean
procedural render in ``studio.rendering`` (sky/moon/hill colours,
figure scale, particles, light, glow, plus the optional executable
camera zoom/pan params, which crop/resample the whole frame with
smoothstep easing over each scene's inclusive frame range).
Descriptive motif/camera text is metadata only and does not affect
pixels; camera prose alone never moves the framing. The creative
video carries no captions, QA markers or logos; the
procedural-prototype disclaimer stays in provenance/storyboard
metadata. Output is a procedural prototype, not finished anime.
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

from p0 import qa as qamod
from p0 import render as rendermod
from p0.pipeline import write_crop_lossless
from studio import artifacts as artmod
from studio import rendering as cleanmod
from studio.render_context import prepare_context


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
    ap.add_argument("--visual-approval", default=None,
                    help="optional visual approval file (see "
                         "studio.visual_preflight.validate_approval_file); "
                         "requires --engine-sha")
    ap.add_argument("--engine-sha", default=None,
                    help="engine commit SHA binding the visual approval")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        ctx = prepare_context(a.wav, a.candidate, a.storyboard, a.brief,
                              width=a.width, height=a.height, seed=a.seed)
    except ValueError as e:
        failures = getattr(e, "failures", None) or [str(e)]
        print(json.dumps({"ok": False, "failures": failures}))
        return 2
    cand = ctx.candidate
    sb = ctx.storyboard
    brief = ctx.brief
    clip = ctx.clip
    det_scenes = ctx.det_scenes
    plan = ctx.plan
    tb = ctx.tb
    # Lossless crop of the ORIGINAL decoded stream (no gain/fade/stretch).
    # Optional visual-preflight approval: validated against the live
    # context immediately before encoding (no flag = legacy local
    # render; the approval file is never trusted without rechecking
    # the actual evidence/review files it references).
    visual_approval = None
    if a.visual_approval is not None:
        if not a.engine_sha:
            print(json.dumps({"ok": False, "failures": [
                "--visual-approval requires --engine-sha"]}))
            return 2
        try:
            from studio.visual_preflight import validate_approval_file
            visual_approval = validate_approval_file(
                Path(a.visual_approval), ctx,
                expected_engine_sha=a.engine_sha)
        except ValueError as e:
            print(json.dumps({"ok": False, "failures": [str(e)]}))
            return 2
    clip_wav = out / "clip_audio.wav"
    write_crop_lossless(Path(a.wav), clip_wav, clip["start_sample"],
                        clip["end_sample"], ctx.sample_rate)
    mp4 = out / "video.mp4"
    # Clean creative renderer (no captions/markers); p0.render is only
    # reused for its ffmpeg encode/mux helper, never for Studio pixels.
    rendermod.render_video(
        (ctx.frame(f) for f in range(clip["n_frames"])),
        str(clip_wav), str(mp4), ctx.width, ctx.height, 30, 1, clip["n_frames"])
    # Contact sheet for the visual reviewer: START / MIDDLE / END of
    # EVERY deterministic scene, sampled from the ENCODED MP4, in
    # chronological scene row groups (one row per scene, 3 columns).
    # END is end_frame-1 (end_frame is exclusive and belongs to the
    # next scene); MIDDLE is the middle of the inclusive range. No
    # captions are drawn into the sheet's thumbnails.
    last_frame = int(clip["n_frames"]) - 1
    scene_triples = []
    for s in det_scenes:
        s0 = max(0, min(last_frame, int(s["start_frame"])))
        e1 = max(s0, min(last_frame, int(s["end_frame"]) - 1))
        scene_triples.append((s0, (s0 + e1) // 2, e1))
    frames = []
    frame_pngs: dict[int, Path] = {}
    for fidx in sorted({f for triple in scene_triples for f in triple}):
        png = out / f"cs_{fidx}.png"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4),
                        "-vf", f"select=eq(n\\,{fidx})", "-frames:v", "1",
                        str(png)], check=True)
        frame_pngs[fidx] = png
        frames.append(png)
    from PIL import Image
    sheet = Image.new("RGB", (270 * 3, 480 * len(scene_triples)))
    for row, triple in enumerate(scene_triples):
        for col, fidx in enumerate(triple):
            with Image.open(frame_pngs[fidx]) as im:
                sheet.paste(im.resize((270, 480)), (col * 270, row * 480))
    sheet.save(out / "contact-sheet.jpg", quality=88)
    for p in frames:
        p.unlink()
    # Encoded-media QA on the real file. Clean video has no marker, so
    # the marker spotcheck is replaced by decoded-frame correspondence
    # against the deterministic clean renderer (computed on demand,
    # cached per frame); all other file-level checks run unchanged.
    reference_fn = cleanmod.make_reference_frame_fn(
        ctx.controls, ctx.width, ctx.height, ctx.seed, scene_palettes=ctx.palettes,
        scene_params=ctx.scene_params, layouts=ctx.layouts)
    qa = qamod.check_file(str(mp4), str(clip_wav), plan, tb, ctx.width,
                          ctx.height, ctx.source_sha256, str(out),
                          "studio", reference_frame_fn=reference_fn)
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
    (out / "plan.json").write_text(json.dumps(ctx.public_plan, indent=2))
    (out / "timeline.json").write_text(json.dumps(timeline, indent=2))
    (out / "qa.json").write_text(json.dumps(
        {"checks": qa["checks"], "pass": qa["pass"],
         "musical_naturalness": "NOT verified (no listening)"}, indent=2))
    prov = {"schema_version": "1.0", "candidate_id": cand["id"],
            "child_issue": brief.get("child_issue"),
            "parent_issue": brief.get("parent_issue"),
            "source_sha256": ctx.source_sha256,
            "source_hashes": brief.get("source_hashes"),
            "planning_fingerprint": brief.get("fingerprint"),
            "brief_fingerprint": brief.get("fingerprint"),
            "prototype": "procedural prototype, not finished anime"}
    if visual_approval is not None:
        prov["visual_approval"] = visual_approval
    if "painted_assets" in ctx.layouts:
        prov["prototype"] = "AI-painted assets with deterministic animation; no generated audio"
        prov["visual_assets"] = ctx.layouts["painted_assets"]["provenance"]
    if "ribbon_dialogue" in ctx.layouts:
        prov["prototype"] = "original procedural paper-ribbon animation; no generated audio"
        prov["visual_renderer"] = plan["visual_renderer"]
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

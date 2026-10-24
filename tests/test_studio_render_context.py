"""Synthetic tests for studio.render_context.prepare_context.

Only synthetic audio is used. The oracle for plan/controls is a
direct recomputation with the existing p0 functions (features,
events, clip.plan_scenes, plan.pick_accents/build_plan_json/
build_controls) -- never a second prepare_context call -- so a
wiring mistake in the extraction (wrong mode, seed, palettes...)
fails here.
"""
import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from p0 import clip as clipmod
from p0 import events as eventsmod
from p0 import features as featmod
from p0 import plan as planmod
from p0.timebase import Timebase
from studio import rendering as R
from studio import storyboard as sbmod
from studio.render_context import prepare_context, public_plan

SR, W, H, SEED = 22050, 180, 320, 20261019
N_FRAMES = 90
CLIP_END = N_FRAMES * (SR // 30)  # 66150, exact integer grid @30fps


def _candidate():
    mid_sample = CLIP_END // 2
    mid_frame = N_FRAMES // 2
    return {
        "id": "cand-001",
        "start_sample": 0,
        "end_sample": CLIP_END,
        "n_frames": N_FRAMES,
        "duration_sec": round(CLIP_END / SR, 4),
        "score": 1.0,
        "scene_reason": "deterministic-test",
        "scenes": [
            {"id": "scene01", "start_sample": 0, "end_sample": mid_sample,
             "start_frame": 0, "end_frame": mid_frame,
             "duration_sec": round(mid_sample / SR, 4)},
            {"id": "scene02", "start_sample": mid_sample,
             "end_sample": CLIP_END, "start_frame": mid_frame,
             "end_frame": N_FRAMES,
             "duration_sec": round((CLIP_END - mid_sample) / SR, 4)},
        ],
    }


def _write_wav(path: Path) -> str:
    n = SR * 4
    t = np.arange(n) / SR
    y = 0.08 * np.sin(2 * np.pi * 220 * t) + 0.05 * np.sin(2 * np.pi * 330 * t)
    for k, start in enumerate((0.5, 1.5, 2.5)):
        s = int(start * SR)
        L = min(int(0.8 * SR), n - s)
        te = np.arange(L) / SR
        y[s:s + L] += 0.30 * np.exp(-te * 3.0) * np.sin(
            2 * np.pi * (220.0 * (1.25 ** k)) * te)
    sf.write(str(path), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_bundle(tmp_path: Path):
    wav = tmp_path / "synthetic.wav"
    source_sha = _write_wav(wav)
    cand = _candidate()
    cand_path = tmp_path / "candidate.json"
    cand_path.write_text(json.dumps(cand, indent=2))
    sb = sbmod.default_storyboard(cand["scenes"])
    sb_path = tmp_path / "storyboard.json"
    sb_path.write_text(json.dumps(sb, indent=2))
    brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
             "candidate_id": cand["id"], "fingerprint": "0" * 64,
             "candidate": cand,
             "source_hashes": {"decoded_wav_sha256": source_sha,
                               "attachment_sha256": "0" * 64}}
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(brief, indent=2))
    return {"wav": wav, "candidate": cand_path, "storyboard": sb_path,
            "brief": brief_path, "cand_obj": cand, "sb_obj": sb,
            "brief_obj": brief, "source_sha": source_sha}


def _prepare(bundle, **kw):
    kw.setdefault("width", W)
    kw.setdefault("height", H)
    kw.setdefault("seed", SEED)
    return prepare_context(bundle["wav"], bundle["candidate"],
                           bundle["storyboard"], bundle["brief"], **kw)


def _direct_expected(bundle):
    """Recompute clip/plan/controls straight from p0 (the oracle)."""
    cand = json.loads(Path(bundle["candidate"]).read_text())
    if "candidate" in cand:
        cand = cand["candidate"]
    sb = json.loads(Path(bundle["storyboard"]).read_text())
    y, sr = sf.read(str(bundle["wav"]), dtype="float64", always_2d=True)
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
    accents = planmod.pick_accents(events, clip, int(sr))
    params = {"seed": SEED, "width": W, "height": H,
              "analysis_hop": int(feat["meta"]["hop"]),
              "analysis_nfft": int(feat["meta"]["n_fft"]),
              "light_floor": planmod.LIGHT_FLOOR}
    plan = planmod.build_plan_json(
        clip, {"scenes": det_scenes,
               "reason": cand.get("scene_reason", "deterministic"),
               "candidate_cuts": []}, accents, tb, SEED, params, [])
    plan["_asset_abspath"] = str(Path(bundle["wav"]).resolve())
    plan["storyboard_style"] = sb.get("style_id")
    controls = planmod.build_controls("structured", clip["n_frames"], tb,
                                      clip, det_scenes, accents, feat, SEED)
    return {"clip": clip, "det_scenes": det_scenes, "plan": plan,
            "controls": controls, "tb": tb, "sr": int(sr)}


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _make_bundle(tmp_path_factory.mktemp("render-context"))


@pytest.fixture(scope="module")
def ctx(bundle):
    return _prepare(bundle)


def test_context_scene_clip_sample_frame_consistency(ctx, bundle):
    assert ctx.sample_rate == SR
    assert ctx.tb.sr == SR
    assert (ctx.width, ctx.height, ctx.seed) == (W, H, SEED)
    assert ctx.source_sha256 == bundle["source_sha"]
    assert ctx.candidate == bundle["cand_obj"]
    assert ctx.brief == bundle["brief_obj"]
    assert ctx.storyboard == bundle["sb_obj"]
    # Clip mirrors the candidate window exactly.
    assert ctx.clip["start_sample"] == bundle["cand_obj"]["start_sample"]
    assert ctx.clip["end_sample"] == bundle["cand_obj"]["end_sample"]
    assert ctx.clip["n_frames"] == N_FRAMES
    # Sample<->frame grid: exact integer grid at 22050 Hz / 30 fps.
    s0 = ctx.clip["start_sample"]
    assert ctx.tb.frame_boundary_sample(s0, 0) == s0
    assert ctx.tb.frame_boundary_sample(s0, N_FRAMES) == ctx.clip["end_sample"]
    assert ctx.tb.sample_to_frame(s0, ctx.clip["end_sample"] - 1) == N_FRAMES - 1
    # Scenes tile [0, n_frames) contiguously and align with controls.
    assert ctx.det_scenes[0]["start_frame"] == 0
    assert ctx.det_scenes[-1]["end_frame"] == N_FRAMES
    for a, b in zip(ctx.det_scenes, ctx.det_scenes[1:]):
        assert a["end_frame"] == b["start_frame"]
        assert a["end_sample"] == b["start_sample"]
    for key, arr in ctx.controls.items():
        assert len(arr) == N_FRAMES, key
    assert list(ctx.controls["scene_id"][:45]) == [0] * 45
    assert list(ctx.controls["scene_id"][45:]) == [1] * 45
    # Plan clip/scenes mirror the context clip/scenes.
    assert ctx.plan["clip"]["start_sample"] == s0
    assert ctx.plan["clip"]["end_sample"] == ctx.clip["end_sample"]
    assert ctx.plan["clip"]["n_frames"] == N_FRAMES
    assert [s["id"] for s in ctx.plan["scenes"]] == [
        s["id"] for s in ctx.det_scenes]
    assert ctx.palettes == [s["palette"] for s in ctx.storyboard["scenes"]]
    assert ctx.scene_params == [s.get("params", {})
                                 for s in ctx.storyboard["scenes"]]


def test_frame_out_of_order_deterministic_and_matches_direct(ctx):
    late_first = ctx.frame(50)
    _ = ctx.frame(10)
    _ = ctx.frame(89)
    late_again = ctx.frame(50)
    assert np.array_equal(late_first, late_again)
    assert late_first.shape == (H, W, 3) and late_first.dtype == np.uint8
    # Exact direct rendering call with the context's own prep inputs.
    direct = R.draw_clean_frame(50, ctx.controls, W, H, SEED, ctx.layouts,
                                scene_palettes=ctx.palettes,
                                scene_params=ctx.scene_params)
    assert np.array_equal(late_first, direct)
    # Different frames genuinely differ (check is not vacuous).
    assert not np.array_equal(ctx.frame(10), late_first)


def test_controls_and_plan_match_direct_p0_calls(ctx, bundle):
    exp = _direct_expected(bundle)
    assert ctx.clip == exp["clip"]
    assert ctx.det_scenes == exp["det_scenes"]
    assert ctx.plan == exp["plan"]
    assert ctx.public_plan == public_plan(exp["plan"])
    assert set(ctx.controls) == set(exp["controls"])
    for key in exp["controls"]:
        assert np.array_equal(ctx.controls[key], exp["controls"][key]), key


def test_candidate_mismatch_rejected(bundle, tmp_path):
    bad_brief = copy.deepcopy(bundle["brief_obj"])
    bad_brief["candidate"] = dict(bad_brief["candidate"], start_sample=735)
    p = tmp_path / "brief.json"
    p.write_text(json.dumps(bad_brief, indent=2))
    with pytest.raises(ValueError, match="candidate does not match"):
        prepare_context(bundle["wav"], bundle["candidate"],
                        bundle["storyboard"], p, width=W, height=H, seed=SEED)


def test_source_hash_mismatch_rejected(bundle, tmp_path):
    bad_brief = copy.deepcopy(bundle["brief_obj"])
    bad_brief["source_hashes"] = {"decoded_wav_sha256": "0" * 64}
    p = tmp_path / "brief.json"
    p.write_text(json.dumps(bad_brief, indent=2))
    with pytest.raises(ValueError, match="source WAV sha256"):
        prepare_context(bundle["wav"], bundle["candidate"],
                        bundle["storyboard"], p, width=W, height=H, seed=SEED)


def test_storyboard_validation_retained(bundle, tmp_path):
    bad_sb = copy.deepcopy(bundle["sb_obj"])
    bad_sb["scenes"] = bad_sb["scenes"][:1]  # ids no longer align
    p = tmp_path / "storyboard.json"
    p.write_text(json.dumps(bad_sb, indent=2))
    with pytest.raises(ValueError, match="storyboard validation failed"):
        prepare_context(bundle["wav"], bundle["candidate"], p,
                        bundle["brief"], width=W, height=H, seed=SEED)


def test_wrapped_candidate_form_accepted(bundle, tmp_path):
    wrapped = tmp_path / "candidate.json"
    wrapped.write_text(json.dumps({"candidate": bundle["cand_obj"]}, indent=2))
    c = prepare_context(bundle["wav"], wrapped, bundle["storyboard"],
                        bundle["brief"], width=W, height=H, seed=SEED)
    assert c.candidate == bundle["cand_obj"]
    assert c.clip["n_frames"] == N_FRAMES
    assert c.det_scenes == bundle["cand_obj"]["scenes"]


def test_native_audio_untouched_and_public_plan_hides_abspath(ctx, bundle):
    before = Path(bundle["wav"]).read_bytes()
    _ = ctx.frame(3)
    assert Path(bundle["wav"]).read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == ctx.source_sha256
    # Private absolute path stays internal only.
    assert ctx.plan["_asset_abspath"] == str(Path(bundle["wav"]).resolve())
    assert "_asset_abspath" not in ctx.public_plan
    assert "_asset_abspath" not in public_plan(ctx.plan)
    assert not any(k.startswith("_") for k in ctx.public_plan)
    serialized = json.dumps(ctx.public_plan)
    assert str(Path(bundle["wav"]).resolve()) not in serialized

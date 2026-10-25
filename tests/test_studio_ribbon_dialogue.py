"""Ribbon provider routing, source binding and real encoded timing faults.

Only local synthetic audio is used. Mechanical correspondence here does
not establish musical naturalness or aesthetic quality.
"""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf

from p0 import qa, render
from p0.timebase import Timebase
from studio import rendering, ribbon_dialogue, storyboard
from studio.config import PAINTED_ASSET_BUNDLE_ID, RIBBON_DIALOGUE_RENDERER_ID

ROOT = Path(__file__).resolve().parents[1]
W, H, N, B, A, SR, SEED = 180, 320, 90, 30, 60, 22050, 20261019
SCENES = [{"id": "scene01", "start_frame": 0, "end_frame": B},
          {"id": "scene02", "start_frame": B, "end_frame": N}]
PLAN = {"clip": {"n_frames": N}, "scenes": SCENES,
        "accents": [{"frame": A}]}
SB = storyboard.default_storyboard(SCENES)
SB["renderer"] = RIBBON_DIALOGUE_RENDERER_ID
PALS = [s["palette"] for s in SB["scenes"]]
PARAMS = [dict(SB["scenes"][0]["params"], dialogue_spread=-.6,
               dialogue_twist=-.5, dialogue_lift=.2),
          dict(SB["scenes"][1]["params"], dialogue_spread=.8,
               dialogue_twist=.7, dialogue_lift=.8)]


def controls(boundary=B, accent=A, strength=1.0):
    c = {"light": np.full(N, .55), "zoom": np.ones(N),
         "camx": np.full(N, .5), "camy": np.full(N, .5),
         "particle": np.full(N, .25), "accent_pulse": np.zeros(N),
         "scene_id": np.array([0]*boundary+[1]*(N-boundary))}
    if accent is not None:
        c["accent_pulse"][accent:accent+15] = strength*np.exp(-np.arange(15)/4)
    return c


def layout():
    lay = rendering.make_layouts(SEED, W, H)
    lay["ribbon_dialogue"] = ribbon_dialogue.make_layout(SEED, W, H)
    return lay


def reference(ctrl, params=PARAMS):
    return rendering.make_reference_frame_fn(ctrl, W, H, SEED,
        scene_palettes=PALS, scene_params=params, layouts=layout())


@pytest.mark.parametrize("value", ["unknown", "../ribbon_dialogue.py", None, 1])
def test_unknown_renderer_rejected(value):
    sb = copy.deepcopy(SB)
    sb["renderer"] = value
    assert "unknown renderer" in storyboard.validate_storyboard(sb, SCENES)["failures"]


def test_renderer_and_asset_bundle_cannot_be_combined():
    sb = copy.deepcopy(SB)
    sb["asset_bundle"] = PAINTED_ASSET_BUNDLE_ID
    result = storyboard.validate_storyboard(sb, SCENES)
    assert not result["ok"]
    assert "renderer and asset_bundle are mutually exclusive" in result["failures"]


@pytest.mark.parametrize("key,low,high", [
    ("dialogue_spread", -1.0, 1.0), ("dialogue_twist", -1.0, 1.0),
    ("dialogue_lift", 0.0, 1.0),
])
def test_dialogue_param_contract_matches_schema_and_rejects_invalid_values(key, low, high):
    schema = json.loads((ROOT / ".github/studio/schemas/storyboard.schema.json").read_text())
    spec = schema["properties"]["scenes"]["items"]["properties"]["params"]["properties"][key]
    assert spec["type"] == "number"
    assert (spec["minimum"], spec["maximum"]) == (low, high) == storyboard.PARAM_RANGES[key]
    assert spec["default"] == ribbon_dialogue.DEFAULTS[key]
    assert ribbon_dialogue.RENDERER_ID == RIBBON_DIALOGUE_RENDERER_ID
    assert schema["properties"]["renderer"]["enum"] == [RIBBON_DIALOGUE_RENDERER_ID]
    assert schema["not"] == {"required": ["renderer", "asset_bundle"]}
    for value in (low, high, (low+high)/2):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"][key] = value
        assert storyboard.validate_storyboard(sb, SCENES)["ok"]
    for value in (low-.001, high+.001, True, "0", None, float("nan"), float("inf")):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"][key] = value
        assert not storyboard.validate_storyboard(sb, SCENES)["ok"], (key, value)


def test_renderer_intake_needs_only_standard_library():
    # The Actions intake runs before image/scientific dependencies install.
    code = """from studio import storyboard
scenes = [{'id': 'scene01'}]
sb = storyboard.default_storyboard(scenes)
sb['renderer'] = 'ribbon-dialogue-v1'
sb['scenes'][0]['params']['dialogue_lift'] = .5
assert storyboard.validate_storyboard(sb, scenes)['ok']
"""
    result = subprocess.run([sys.executable, "-S", "-c", code],
                            cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_reference_is_order_independent_and_preserves_controls():
    ctrl = controls()
    before = {key: value.copy() for key, value in ctrl.items()}
    ref = reference(ctrl)
    late = ref(65).copy()
    early = ref(1).copy()
    assert np.array_equal(late, reference(ctrl)(65))
    assert not np.array_equal(late, early)
    ref.shifted_boundary_frame(B, 1, B)
    for key in ctrl:
        assert np.array_equal(ctrl[key], before[key]), key
    assert ref.patch_mae_max == 5.0
    assert late.shape == (H, W, 3) and late.dtype == np.uint8


def test_dialogue_controls_do_not_change_legacy_pixels_or_style_defaults():
    ctrl, lay = controls(), rendering.make_layouts(SEED, W, H)
    legacy = [scene["params"] for scene in SB["scenes"]]
    for frame in (1, B, N-1):
        expected = rendering.draw_clean_frame(frame, ctrl, W, H, SEED, lay,
            scene_palettes=PALS, scene_params=legacy)
        actual = rendering.draw_clean_frame(frame, ctrl, W, H, SEED, lay,
            scene_palettes=PALS, scene_params=PARAMS)
        assert np.array_equal(expected, actual)
        _, params = rendering.visual_style_for_frame(frame, ctrl, PALS, PARAMS)
        assert set(params) == {"figure_scale", "particle_multiplier", "light_bias", "glow_scale"}


@pytest.mark.parametrize("key,low,high", [
    ("dialogue_spread", -1.0, 1.0), ("dialogue_twist", -1.0, 1.0),
    ("dialogue_lift", 0.0, 1.0),
])
def test_declared_dialogue_controls_reach_actual_pixels(key, low, high):
    a, b = copy.deepcopy(PARAMS), copy.deepcopy(PARAMS)
    a[0][key], b[0][key] = low, high
    assert not np.array_equal(reference(controls(), a)(10), reference(controls(), b)(10)), key


def _ribbon_bundle(path):
    from test_studio_render_context import _make_bundle
    bundle = _make_bundle(path)
    sb = json.loads(bundle["storyboard"].read_text())
    sb["renderer"] = RIBBON_DIALOGUE_RENDERER_ID
    for index, scene in enumerate(sb["scenes"]):
        scene["params"] = PARAMS[index]
    bundle["storyboard"].write_text(json.dumps(sb))
    return bundle


def test_context_binds_source_hash_and_invalidates_old_visual_evidence(tmp_path, monkeypatch):
    from test_studio_render_context import _prepare
    from studio import visual_preflight as vp
    bundle = _ribbon_bundle(tmp_path)
    before = _prepare(bundle)
    source = Path(ribbon_dialogue.__file__)
    binding = {"renderer_id": RIBBON_DIALOGUE_RENDERER_ID,
               "source_path": "studio/ribbon_dialogue.py",
               "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert before.public_plan["visual_renderer"] == binding
    assert "ribbon_dialogue" in before.layouts and "painted_assets" not in before.layouts
    assert np.array_equal(before.frame(4), ribbon_dialogue.draw_frame(
        4, before.controls, W, H, SEED, before.layouts, before.palettes, before.scene_params))
    evidence = tmp_path / "evidence"
    vp.generate_evidence(before, evidence, stage="still", engine_sha="a"*40,
                         run_id=123, run_attempt=1, iteration=0)
    assert vp.validate_evidence(evidence, context=before)["ok"]
    # Equivalent pixels alone cannot preserve a previous source approval.
    changed_source = tmp_path / "ribbon_dialogue.py"
    changed_source.write_bytes(source.read_bytes() + b"\n# new renderer revision\n")
    monkeypatch.setattr(ribbon_dialogue, "__file__", str(changed_source))
    after = _prepare(bundle)
    assert after.public_plan["visual_renderer"]["sha256"] != binding["sha256"]
    assert not vp.validate_evidence(evidence, context=after)["ok"]


def test_render_provenance_records_original_provider_and_exact_source(tmp_path):
    bundle = _ribbon_bundle(tmp_path)
    out = tmp_path / "render"
    result = subprocess.run([
        sys.executable, str(ROOT / "scripts/studio_render.py"),
        "--wav", str(bundle["wav"]), "--candidate", str(bundle["candidate"]),
        "--storyboard", str(bundle["storyboard"]), "--brief", str(bundle["brief"]),
        "--out", str(out), "--width", str(W), "--height", str(H),
    ], cwd=ROOT, capture_output=True, text=True, timeout=180)
    # The tiny 3s fixture cannot satisfy the existing 6s minimum scene
    # invariant, but produces provenance after real encoding and QA.
    assert result.returncode in (0, 2), (result.stdout[-1000:], result.stderr[-1000:])
    prov = json.loads((out / "provenance.json").read_text())
    plan = json.loads((out / "plan.json").read_text())
    assert prov["prototype"] == "original procedural paper-ribbon animation; no generated audio"
    assert prov["visual_renderer"] == plan["visual_renderer"]
    assert prov["visual_renderer"]["sha256"] == hashlib.sha256(
        Path(ribbon_dialogue.__file__).read_bytes()).hexdigest()
    assert "visual_assets" not in prov
    assert prov["media_sha256"]["video.mp4"] == hashlib.sha256(
        (out / "video.mp4").read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    out = tmp_path_factory.mktemp("ribbon-encode")
    wav = out / "tone.wav"
    sf.write(wav, .1*np.sin(np.arange(SR*3)*2*np.pi*440/SR), SR)
    cases = {"good": controls(), "scene_early": controls(boundary=B-1),
             "scene_late": controls(boundary=B+1), "accent_missing": controls(accent=None),
             "accent_late": controls(accent=A+1)}
    paths = {}
    for name, ctrl in cases.items():
        path = out / f"{name}.mp4"
        fn = reference(ctrl)
        render.render_video((fn(f) for f in range(N)), str(wav), str(path), W, H, 30, 1, N)
        paths[name] = str(path)
    return paths


@pytest.mark.parametrize("name", [
    "good", "scene_early", "scene_late", "accent_missing", "accent_late",
])
def test_real_encoded_timing_faults(encoded, name):
    result = qa.check_rendered_frames(encoded[name], PLAN, Timebase(SR, 30, 1),
                                      W, H, N, reference(controls()),
                                      require_transition_timing=True)
    assert result["pass"] == (name == "good"), result
    if name != "good":
        target = B if name.startswith("scene_") else A
        failing = {f["frame"]: f for f in result["frames"] if not f["ok"]}
        assert target in failing, (name, result)
        if name.startswith("scene_"):
            timing = failing[target]["transition_timing"]
            assert not timing["ok"]
            assert timing["best_offset_frames"] == (-1 if name == "scene_early" else 1)


@pytest.mark.parametrize("scene_index", range(2))
def test_native_strength_floor_accent_rejects_missing_and_late_encodes(tmp_path, scene_index):
    # pick_accents retains native DSP strengths >= .5. Use that weakest
    # supported pulse, rather than a hand-amplified event, in each palette.
    from p0.plan import ACCENT_STRENGTH_FLOOR
    n, accent = 36, 18
    plan = {"clip": {"n_frames": n},
            "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": n}],
            "accents": [{"frame": accent}]}
    wav = tmp_path / "silence.wav"
    sf.write(wav, np.zeros(SR*n//30), SR)

    def frame_fn(at):
        ctrl = {key: value[:n] for key, value in controls(
            boundary=N, accent=at, strength=ACCENT_STRENGTH_FLOOR).items()}
        return rendering.make_reference_frame_fn(ctrl, W, H, SEED,
            scene_palettes=[PALS[scene_index]], scene_params=[PARAMS[scene_index]],
            layouts=layout())

    for name, at in (("good", accent), ("missing", None), ("late", accent+1)):
        path = tmp_path / f"{name}.mp4"
        fn = frame_fn(at)
        render.render_video((fn(f) for f in range(n)), str(wav), str(path), W, H, 30, 1, n)
        result = qa.check_rendered_frames(str(path), plan, Timebase(SR, 30, 1),
                                          W, H, n, frame_fn(accent),
                                          require_transition_timing=True)
        assert result["pass"] == (name == "good"), (scene_index, name, result)
        if name != "good":
            assert any(f["frame"] == accent and not f["ok"] for f in result["frames"])

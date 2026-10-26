"""Storm recital provider routing, source binding and real encoded timing faults.

Only local synthetic audio is used. Mechanical correspondence here does
not establish musical naturalness or aesthetic quality.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from p0 import qa, render
from p0.timebase import Timebase
from studio import rendering, storm_recital, storyboard
from studio.config import PAINTED_ASSET_BUNDLE_ID, STORM_RECITAL_RENDERER_ID

ROOT = Path(__file__).resolve().parents[1]
W, H, N, B, A, SR, SEED = 180, 320, 90, 30, 60, 22050, 20261019
SCENES = [{"id": "scene01", "start_frame": 0, "end_frame": B},
          {"id": "scene02", "start_frame": B, "end_frame": N}]
PLAN = {"clip": {"n_frames": N}, "scenes": SCENES,
        "accents": [{"frame": A}]}
SB = storyboard.default_storyboard(SCENES)
SB["renderer"] = STORM_RECITAL_RENDERER_ID
PALS = [s["palette"] for s in SB["scenes"]]
PARAMS = [dict(SB["scenes"][0]["params"], storm_shot=0, rain_density=.5,
               rain_wind=-.4, storm_light=.3, reflection_strength=.4),
          dict(SB["scenes"][1]["params"], storm_shot=2, rain_density=1.,
               rain_wind=.5, storm_light=.9, reflection_strength=.9)]


def controls(boundary=B, accent=A, strength=1.0):
    c = {"light": np.full(N, .55), "zoom": np.ones(N),
         "camx": np.full(N, .5), "camy": np.full(N, .5),
         "particle": np.full(N, .25), "accent_pulse": np.zeros(N),
         "scene_id": np.array([0]*boundary+[1]*(N-boundary))}
    if accent is not None:
        c["accent_pulse"][accent:accent+15] = strength*np.exp(-np.arange(15)/4)
    return c


def layout(w=W, h=H):
    lay = rendering.make_layouts(SEED, w, h)
    lay["storm_recital"] = storm_recital.make_layout(SEED, w, h)
    return lay


def reference(ctrl, params=PARAMS):
    return rendering.make_reference_frame_fn(ctrl, W, H, SEED,
        scene_palettes=PALS, scene_params=params, layouts=layout())


@pytest.mark.parametrize("value", ["unknown", "ribbon-dialogue-v1", "../storm_recital.py", None, 1])
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
    ("rain_density", .2, 1.), ("rain_wind", -1., 1.),
    ("storm_light", 0., 1.), ("reflection_strength", 0., 1.),
])
def test_storm_param_contract_matches_schema_and_rejects_invalid_values(key, low, high):
    schema = json.loads((ROOT / ".github/studio/schemas/storyboard.schema.json").read_text())
    spec = schema["properties"]["scenes"]["items"]["properties"]["params"]["properties"][key]
    assert spec["type"] == "number"
    assert (spec["minimum"], spec["maximum"]) == (low, high) == storyboard.PARAM_RANGES[key]
    assert spec["default"] == storm_recital.DEFAULTS[key]
    assert storm_recital.RENDERER_ID == STORM_RECITAL_RENDERER_ID
    assert STORM_RECITAL_RENDERER_ID in schema["properties"]["renderer"]["enum"]
    assert schema["not"] == {"required": ["renderer", "asset_bundle"]}
    for value in (low, high, (low+high)/2):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"][key] = value
        assert storyboard.validate_storyboard(sb, SCENES)["ok"]
    for value in (low-.001, high+.001, True, "0", None, float("nan"), float("inf")):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"][key] = value
        assert not storyboard.validate_storyboard(sb, SCENES)["ok"], (key, value)


def test_shot_is_bounded_integer_in_schema_and_python():
    schema = json.loads((ROOT / ".github/studio/schemas/storyboard.schema.json").read_text())
    spec = schema["properties"]["scenes"]["items"]["properties"]["params"]["properties"]["storm_shot"]
    assert spec["type"] == "integer"
    assert (spec["minimum"], spec["maximum"]) == (0, 2) == storyboard.PARAM_RANGES["storm_shot"]
    assert spec["default"] == storm_recital.DEFAULTS["storm_shot"]
    for value in (0, 1, 2, 1.0):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"]["storm_shot"] = value
        assert storyboard.validate_storyboard(sb, SCENES)["ok"], value
    for value in (-1, 3, .5, 1.99, True, "1", None, float("nan"), float("inf")):
        sb = copy.deepcopy(SB)
        sb["scenes"][0]["params"]["storm_shot"] = value
        assert not storyboard.validate_storyboard(sb, SCENES)["ok"], value


@pytest.mark.parametrize("key", ["dialogue_spread", "dialogue_twist", "dialogue_lift"])
def test_unused_dialogue_params_are_rejected(key):
    schema = json.loads((ROOT / ".github/studio/schemas/storyboard.schema.json").read_text())
    assert key not in schema["properties"]["scenes"]["items"]["properties"]["params"]["properties"]
    sb = copy.deepcopy(SB)
    sb["scenes"][0]["params"][key] = 0
    assert not storyboard.validate_storyboard(sb, SCENES)["ok"]


def test_renderer_intake_needs_only_standard_library():
    # The Actions intake runs before image/scientific dependencies install.
    code = """from studio import storyboard
scenes = [{'id': 'scene01'}]
sb = storyboard.default_storyboard(scenes)
sb['renderer'] = 'storm-recital-v1'
sb['scenes'][0]['params']['storm_light'] = .5
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


def test_shot_dissolve_is_eight_frames_while_weather_and_qa_keep_full_window():
    boundary = 120
    ctrl = {"scene_id": np.repeat([0, 1], boundary)}
    before = ctrl["scene_id"].copy()
    assert storm_recital.SHOT_DISSOLVE_HALF_FRAMES == 4
    weights = {}
    for f in range(boundary-24, boundary+25):
        state, shots = storm_recital._state(f, ctrl, PARAMS)
        weights[f] = dict(shots)
        assert set(weights[f]) <= {0, 2}  # No intermediate medium shot.
        assert sum(weights[f].values()) == pytest.approx(1)
        if f <= boundary-4:
            assert weights[f][0] == 1
        elif f >= boundary+4:
            assert weights[f][2] == 1
        else:
            assert 0 < weights[f][2] < 1
        if f in (boundary-5, boundary+5):
            # Weather still changes outside the shorter image dissolve.
            assert PARAMS[0]["rain_density"] < state["rain_density"] < PARAMS[1]["rain_density"]
    assert weights[boundary] == {0: .5, 2: .5}
    assert np.array_equal(before, ctrl["scene_id"])
    ref = rendering.make_reference_frame_fn(ctrl, W, H, SEED,
        scene_params=PARAMS, layouts=layout())
    assert ref.transition_window(boundary) == (boundary-24, boundary+24, .5)


def test_short_scene_caps_shot_dissolve_without_moving_its_center():
    ctrl = {"scene_id": np.repeat([0, 1], 4)}
    assert dict(storm_recital._state(3, ctrl, PARAMS)[1]) == {0: 1, 2: 0}
    assert dict(storm_recital._state(4, ctrl, PARAMS)[1]) == {0: .5, 2: .5}
    assert dict(storm_recital._state(5, ctrl, PARAMS)[1]) == {0: 0, 2: 1}
    # A one-frame scene retains its explicit shot without a fabricated join.
    ctrl = {"scene_id": np.array([0, 1])}
    assert storm_recital._state(0, ctrl, PARAMS)[1] == [(0, 1.)]
    assert storm_recital._state(1, ctrl, PARAMS)[1] == [(2, 1.)]


def _lightning_inputs():
    base = Image.new("RGB", (W, H), (20, 20, 20))
    pulses = np.zeros(120)
    for onset in (18, 80):
        pulses[onset:onset+15] = .5*np.exp(-np.arange(15)/4)
    return base, {"accent_pulse": pulses}, dict(storm_recital.DEFAULTS, storm_light=.9)


def test_lightning_geometry_is_stable_within_one_dsp_event_and_order_independent():
    base, ctrl, state = _lightning_inputs()
    before = ctrl["accent_pulse"].copy()
    sky = np.ones((H, W), dtype=np.float32)
    def draw(frame):
        return np.asarray(storm_recital._lightning(base.copy(), frame, ctrl, state, sky, SEED))
    later = draw(82)
    peak = draw(18)
    assert np.array_equal(later, draw(82))
    assert not np.array_equal(peak, draw(80))
    assert np.array_equal(ctrl["accent_pulse"], before)
    # Equal pulse values within one event expose geometry independently
    # of its normal exponential fade: a plateau is not a new onset.
    ctrl["accent_pulse"][19] = ctrl["accent_pulse"][18]
    assert np.array_equal(draw(18), draw(19))


def test_lightning_adds_no_events_and_never_crosses_sky_or_overhead_masks():
    base, ctrl, state = _lightning_inputs()
    sky = np.ones((H, W), dtype=np.float32)
    for frame in (17, 79, 110):
        assert np.array_equal(np.asarray(base), np.asarray(storm_recital._lightning(
            base.copy(), frame, ctrl, state, sky, SEED)))
    assert np.array_equal(np.asarray(base), np.asarray(storm_recital._lightning(
        base.copy(), 82, ctrl, state, np.zeros_like(sky), SEED)))
    sky[H//2:] = 0
    masked = np.asarray(storm_recital._lightning(base.copy(), 18, ctrl, state, sky, SEED))
    assert np.array_equal(masked[H//2:], np.asarray(base)[H//2:])


def test_storm_controls_do_not_change_legacy_pixels_or_style_defaults():
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
    ("storm_shot", 0, 2), ("rain_density", .2, 1.),
    ("rain_wind", -1., 1.), ("storm_light", 0., 1.),
    ("reflection_strength", 0., 1.),
])
def test_declared_storm_controls_reach_actual_pixels(key, low, high):
    a, b = copy.deepcopy(PARAMS), copy.deepcopy(PARAMS)
    a[0][key], b[0][key] = low, high
    assert not np.array_equal(reference(controls(), a)(10), reference(controls(), b)(10)), key


def _storm_bundle(path):
    from test_studio_render_context import _make_bundle
    bundle = _make_bundle(path)
    sb = json.loads(bundle["storyboard"].read_text())
    sb["renderer"] = STORM_RECITAL_RENDERER_ID
    for index, scene in enumerate(sb["scenes"]):
        scene["params"] = PARAMS[index]
    bundle["storyboard"].write_text(json.dumps(sb))
    return bundle


def test_context_binds_source_hash_and_invalidates_old_visual_evidence(tmp_path, monkeypatch):
    from test_studio_render_context import _prepare
    from studio import visual_preflight as vp
    bundle = _storm_bundle(tmp_path)
    before = _prepare(bundle)
    source = Path(storm_recital.__file__)
    binding = {"renderer_id": STORM_RECITAL_RENDERER_ID,
               "source_path": "studio/storm_recital.py",
               "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert before.public_plan["visual_renderer"] == binding
    assert "storm_recital" in before.layouts and "painted_assets" not in before.layouts
    assert before.public_plan["visual_assets"] == before.layouts["storm_recital"]["provenance"]
    assert np.array_equal(before.frame(4), storm_recital.draw_frame(
        4, before.controls, W, H, SEED, before.layouts, before.palettes, before.scene_params))
    evidence = tmp_path / "evidence"
    vp.generate_evidence(before, evidence, stage="still", engine_sha="a"*40,
                         run_id=123, run_attempt=1, iteration=0)
    assert vp.validate_evidence(evidence, context=before)["ok"]
    # Equivalent pixels alone cannot preserve a previous source approval.
    changed_source = tmp_path / "storm_recital.py"
    changed_source.write_bytes(source.read_bytes() + b"\n# new renderer revision\n")
    monkeypatch.setattr(storm_recital, "__file__", str(changed_source))
    after = _prepare(bundle)
    assert after.public_plan["visual_renderer"]["sha256"] != binding["sha256"]
    assert not vp.validate_evidence(evidence, context=after)["ok"]


def test_render_provenance_records_painted_provider_and_exact_source_and_assets(tmp_path):
    bundle = _storm_bundle(tmp_path)
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
    assert prov["prototype"] == "AI-painted storm recital shots with deterministic rain, light and reflections; no generated audio"
    assert prov["visual_renderer"] == plan["visual_renderer"]
    assert prov["visual_renderer"]["sha256"] == hashlib.sha256(
        Path(storm_recital.__file__).read_bytes()).hexdigest()
    assert prov["visual_assets"] == plan["visual_assets"]
    assert len(prov["visual_assets"]["files"]) == 3
    assert prov["media_sha256"]["video.mp4"] == hashlib.sha256(
        (out / "video.mp4").read_bytes()).hexdigest()


def test_registered_asset_digest_mismatch_is_rejected(tmp_path, monkeypatch):
    directory = tmp_path / storm_recital.DIRECTORY
    shutil.copytree(storm_recital.ROOT / storm_recital.DIRECTORY, directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    assert len(manifest["files"]) == 3
    image = directory / next(iter(manifest["files"]))
    image.write_bytes(image.read_bytes() + b"changed")
    monkeypatch.setattr(storm_recital, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="digest mismatch"):
        storm_recital.make_layout(SEED, W, H)


def test_registered_overhead_piano_is_excluded_from_water_reflections():
    ground = storm_recital.make_layout(SEED, W, H)["ground"][2]
    # Regression for the reviewed upper-right piano body and seated player
    # in overhead.png. The outer wet stone must still support reflections.
    for x, y in ((.85, .30), (.20, .62), (.50, .50)):
        assert ground[int(y*H), int(x*W)] == 0
    assert ground[int(.90*H), int(.05*W)] > .5


def test_asset_revision_invalidates_old_visual_evidence(tmp_path, monkeypatch):
    from test_studio_render_context import _prepare
    from studio import visual_preflight as vp
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    bundle = _storm_bundle(inputs)
    directory = tmp_path / storm_recital.DIRECTORY
    shutil.copytree(storm_recital.ROOT / storm_recital.DIRECTORY, directory)
    monkeypatch.setattr(storm_recital, "ROOT", tmp_path)
    before = _prepare(bundle)
    evidence = tmp_path / "evidence"
    vp.generate_evidence(before, evidence, stage="still", engine_sha="a"*40,
                         run_id=123, run_attempt=1, iteration=0)
    assert vp.validate_evidence(evidence, context=before)["ok"]
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    image_name = next(iter(manifest["files"]))
    image = directory / image_name
    image.write_bytes(image.read_bytes() + b"\nnew registered asset revision\n")
    manifest["files"][image_name] = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    after = _prepare(bundle)
    assert before.public_plan["visual_assets"] != after.public_plan["visual_assets"]
    assert not vp.validate_evidence(evidence, context=after)["ok"]


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    out = tmp_path_factory.mktemp("storm-encode")
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


def test_encoded_long_scene_join_keeps_full_qa_window_and_rejects_one_frame_shifts(tmp_path):
    # The 3s fixture's short scenes cap the common half-window at seven
    # frames. Exercise the full 24-frame join, with a different observed
    # GOP from the 61-frame reference, to catch an unobservable slow fade.
    boundary, start, n = 120, 60, 121
    ctrl = {key: np.resize(value, 240) for key, value in controls(accent=None).items()}
    ctrl["scene_id"] = np.repeat([0, 1], boundary)
    canonical = rendering.make_reference_frame_fn(ctrl, W, H, SEED,
        scene_palettes=PALS, scene_params=PARAMS, layouts=layout())

    def ref(f):
        return canonical(start+f)

    ref.shifted_boundary_frame = lambda b, shift, f: canonical.shifted_boundary_frame(
        boundary, shift, start+f)
    ref.transition_window = lambda b: (boundary-24-start, boundary+24-start, .5)
    ref.patch_mae_max = canonical.patch_mae_max
    wav = tmp_path / "silence.wav"
    sf.write(wav, np.zeros(SR*n//30), SR)
    plan = {"clip": {"n_frames": n}, "accents": [], "scenes": [
        {"start_frame": 0, "end_frame": boundary-start},
        {"start_frame": boundary-start, "end_frame": n}]}
    for shift in (0, -1, 1):
        path = tmp_path / f"shift-{shift}.mp4"
        frames = (canonical.shifted_boundary_frame(boundary, shift, start+f)
                  for f in range(n))
        render.render_video(frames, str(wav), str(path), W, H, 30, 1, n)
        result = qa.check_rendered_frames(str(path), plan, Timebase(SR, 30, 1),
            W, H, n, ref, require_transition_timing=True)
        assert result["pass"] == (shift == 0), (shift, result)
        timing, = result["transition_timing"]
        assert timing["probe_frames"] == list(range(30, 91))
        assert timing["best_offset_frames"] == shift, timing
        assert timing["separation_ratio"] >= qa.TRANSITION_SEPARATION_RATIO_MIN, timing


@pytest.mark.parametrize("shot_index", range(3))
@pytest.mark.parametrize("w,h", [(W, H), (540, 960)])
def test_native_strength_floor_accent_rejects_missing_and_late_encodes(tmp_path, shot_index, w, h):
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
        return rendering.make_reference_frame_fn(ctrl, w, h, SEED,
            scene_palettes=[PALS[shot_index % len(PALS)]],
            scene_params=[dict(PARAMS[shot_index % len(PARAMS)], storm_shot=shot_index)],
            layouts=layout(w, h))

    for name, at in (("good", accent), ("missing", None), ("late", accent+1)):
        path = tmp_path / f"{name}.mp4"
        fn = frame_fn(at)
        render.render_video((fn(f) for f in range(n)), str(wav), str(path), w, h, 30, 1, n)
        result = qa.check_rendered_frames(str(path), plan, Timebase(SR, 30, 1),
                                          w, h, n, frame_fn(accent),
                                          require_transition_timing=True)
        assert result["pass"] == (name == "good"), (shot_index, name, result)
        if name != "good":
            assert any(f["frame"] == accent and not f["ok"] for f in result["frames"])

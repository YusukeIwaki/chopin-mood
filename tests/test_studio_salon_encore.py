"""Registered salon layers: routing, immutable evidence, encoded timing faults.

All audio is synthetic. These checks establish deterministic correspondence,
not the musical correctness of a performance or an aesthetic endorsement.
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

from p0 import qa, render
from p0.timebase import Timebase
from studio import rendering, salon_encore, storyboard
from studio.config import SALON_ENCORE_RENDERER_ID

ROOT = Path(__file__).resolve().parents[1]
W, H, N, B, A, SR, SEED = 180, 320, 120, 45, 90, 22050, 20261019
SCENES = [{"id": "scene01", "start_frame": 0, "end_frame": B},
          {"id": "scene02", "start_frame": B, "end_frame": N}]
PARAMS = [dict(figure_scale=1, particle_multiplier=0, light_bias=0, glow_scale=1, salon_shot=0),
          dict(figure_scale=1, particle_multiplier=0, light_bias=.02, glow_scale=1, salon_shot=2)]
PALS = [salon_encore.BASE_PALETTE]*2


def controls(boundary=B, accent=A, strength=1.):
    ctrl = {"light": np.full(N, .55), "zoom": np.ones(N),
            "camx": np.full(N, .5), "camy": np.full(N, .5),
            "particle": np.zeros(N), "accent_pulse": np.zeros(N),
            "scene_id": np.array([0]*boundary+[1]*(N-boundary))}
    if accent is not None:
        ctrl["accent_pulse"][accent:accent+15] = strength*np.exp(-np.arange(15)/4)
    return ctrl


def reference(ctrl, w=W, h=H):
    layouts = rendering.make_layouts(SEED, w, h)
    layouts["salon_encore"] = salon_encore.make_layout(SEED, w, h)
    return rendering.make_reference_frame_fn(ctrl, w, h, SEED,
        scene_palettes=PALS, scene_params=PARAMS, layouts=layouts)


def test_intake_is_stdlib_only_and_shot_contract_is_bounded_integer():
    code = """from studio.storyboard import default_storyboard, validate_storyboard
scenes = [{'id': 'scene01'}]
sb = default_storyboard(scenes)
sb['renderer'] = 'salon-encore-v1'
sb['scenes'][0]['params']['salon_shot'] = 2
assert validate_storyboard(sb, scenes)['ok']
"""
    result = subprocess.run([sys.executable, "-S", "-c", code], cwd=ROOT,
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    sb = storyboard.default_storyboard(SCENES)
    sb["renderer"] = SALON_ENCORE_RENDERER_ID
    schema = json.loads((ROOT/".github/studio/schemas/storyboard.schema.json").read_text())
    spec = schema["properties"]["scenes"]["items"]["properties"]["params"]["properties"]["salon_shot"]
    assert spec["type"] == "integer" and (spec["minimum"], spec["maximum"]) == (0, 2)
    for value in (-1, 3, .5, True, "1", None, float("nan")):
        sb["scenes"][0]["params"]["salon_shot"] = value
        assert not storyboard.validate_storyboard(sb, SCENES)["ok"]


def test_registered_alpha_digest_and_source_are_bound(tmp_path, monkeypatch):
    from test_studio_render_context import _make_bundle, _prepare
    from studio import visual_preflight as vp
    inputs = tmp_path/"inputs"
    inputs.mkdir()
    bundle = _make_bundle(inputs)
    sb = json.loads(bundle["storyboard"].read_text())
    sb["renderer"] = SALON_ENCORE_RENDERER_ID
    for i, scene in enumerate(sb["scenes"]):
        scene["palette"] = {k: list(v) for k, v in salon_encore.BASE_PALETTE.items()}
        scene["params"] = PARAMS[i]
    bundle["storyboard"].write_text(json.dumps(sb))
    ctx = _prepare(bundle)
    assert "salon_encore" in ctx.layouts and "storm_recital" not in ctx.layouts
    assert ctx.public_plan["visual_renderer"]["source_path"] == "studio/salon_encore.py"
    assert ctx.public_plan["visual_renderer"]["sha256"] == hashlib.sha256(Path(salon_encore.__file__).read_bytes()).hexdigest()
    assert ctx.public_plan["visual_assets"] == ctx.layouts["salon_encore"]["provenance"]
    sprite = ctx.layouts["salon_encore"]["sprite"]
    assert sprite.mode == "RGBA" and sprite.getchannel("A").getextrema()[0] == 0
    assert hashlib.sha256(bundle["wav"].read_bytes()).hexdigest() == ctx.source_sha256
    evidence = tmp_path/"evidence"
    vp.generate_evidence(ctx, evidence, stage="still", engine_sha="a"*40,
                         run_id=1, run_attempt=1, iteration=0)
    assert vp.validate_evidence(evidence, context=ctx)["ok"]
    source = tmp_path/"salon_encore.py"
    source.write_bytes(Path(salon_encore.__file__).read_bytes()+b"\n# renderer revision\n")
    monkeypatch.setattr(salon_encore, "__file__", str(source))
    assert not vp.validate_evidence(evidence, context=_prepare(bundle))["ok"]

    directory = tmp_path/salon_encore.DIRECTORY
    shutil.copytree(ROOT/salon_encore.DIRECTORY, directory)
    monkeypatch.setattr(salon_encore, "ROOT", tmp_path)
    path = directory/"performance.png"
    path.write_bytes(path.read_bytes()+b"tampered")
    with pytest.raises(ValueError, match="digest mismatch"):
        salon_encore.make_layout(SEED, W, H)


def test_order_independence_anchored_piano_and_no_control_mutation():
    ctrl = controls()
    before = copy.deepcopy(ctrl)
    ref = reference(ctrl)
    later = ref(94).copy()
    ref(2)
    assert np.array_equal(ref(94), later)
    ref.shifted_boundary_frame(B, 1, B)
    for key in ctrl:
        assert np.array_equal(ctrl[key], before[key])
    assert ref.patch_mae_max == 5
    source = salon_encore.make_layout(SEED, W, H)["sprite"].resize((180, 120))
    for frame in (0, 60, 100):
        moved = salon_encore._posture(source, frame, .6, .4)
        # The bottom quarter contains feet, bench legs and piano casters.
        assert np.array_equal(np.asarray(source)[90:], np.asarray(moved)[90:])
    assert not np.array_equal(ref(0), ref(20))


def test_registered_close_framing_is_distinct_from_bounded_user_camera():
    # A coordinate ramp measures the actual crop, independently of art.
    frame = np.broadcast_to(np.arange(W, dtype=np.uint8)[None, :, None],
                            (H, W, 3)).copy()
    close = salon_encore._shot_frame(frame, 2)
    zoom, pan_x, _ = salon_encore.SHOTS[2]
    expected_left = (W-W/zoom)*.5*(1+pan_x)
    assert float(close[H//2, 0, 0]) == pytest.approx(expected_left, abs=1)
    assert float(close[H//2, -1, 0]-close[H//2, 0, 0]) == pytest.approx(W/zoom-1, abs=2)
    assert np.array_equal(salon_encore._shot_frame(frame, 0), frame)


@pytest.fixture(scope="module", params=[1., .5])
def encoded(tmp_path_factory, request):
    out = tmp_path_factory.mktemp("salon-encoded")
    wav = out/"tone.wav"
    sf.write(wav, .1*np.sin(np.arange(SR*4)*2*np.pi*440/SR), SR)
    strength = request.param
    cases = {"good": controls(strength=strength),
             "scene_early": controls(boundary=B-1, strength=strength),
             "scene_late": controls(boundary=B+1, strength=strength),
             "accent_missing": controls(accent=None, strength=strength),
             "accent_late": controls(accent=A+1, strength=strength)}
    paths = {}
    for name, ctrl in cases.items():
        path = out/f"{name}.mp4"
        fn = reference(ctrl)
        render.render_video((fn(f) for f in range(N)), str(wav), str(path), W, H, 30, 1, N)
        paths[name] = str(path)
    return paths, strength


@pytest.mark.parametrize("name", ["good", "scene_early", "scene_late", "accent_missing", "accent_late"])
def test_real_encoded_timing_faults(encoded, name):
    paths, strength = encoded
    plan = {"clip": {"n_frames": N}, "scenes": SCENES, "accents": [{"frame": A}]}
    result = qa.check_rendered_frames(paths[name], plan, Timebase(SR, 30, 1), W, H, N,
                                     reference(controls(strength=strength)), require_transition_timing=True)
    assert result["pass"] == (name == "good"), result
    if name != "good":
        target = B if name.startswith("scene_") else A
        assert target in {f["frame"] for f in result["frames"] if not f["ok"]}

"""Painted provider: asset integrity, deterministic frames and encoded faults."""
import copy
import hashlib
import json
import shutil

import numpy as np
import pytest
import soundfile as sf

from p0 import qa, render
from p0.timebase import Timebase
from studio import painted, rendering, storyboard

W, H, N, B, A, SEED = 180, 320, 90, 30, 60, 20261019
PROJECT = painted.ROOT / "studio/projects/issue-1/storyboard.json"
SB = json.loads(PROJECT.read_text())
PALS = [s["palette"] for s in SB["scenes"][:2]]
PARAMS = [s["params"] for s in SB["scenes"][:2]]
PLAN = {"clip": {"n_frames": N},
        "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": B},
                   {"id": "scene02", "start_frame": B, "end_frame": N}],
        "accents": [{"frame": A}]}


def controls(boundary=B, accent=A):
    c = {"light": np.full(N, .55), "zoom": np.ones(N),
         "camx": np.full(N, .5), "camy": np.full(N, .5),
         "particle": np.full(N, .25), "accent_pulse": np.zeros(N),
         "scene_id": np.array([0]*boundary+[1]*(N-boundary))}
    if accent is not None:
        c["accent_pulse"][accent:accent+15] = np.exp(-np.arange(15)/4)
    return c


def layout():
    lay = rendering.make_layouts(SEED, W, H)
    lay["painted_assets"] = painted.load_bundle(painted.BUNDLE_ID, W, H)
    return lay


def reference(ctrl):
    return rendering.make_reference_frame_fn(ctrl, W, H, SEED,
        scene_palettes=PALS, scene_params=PARAMS, layouts=layout())


def test_asset_id_and_changed_bytes_rejected(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="unknown"):
        painted.load_bundle("../../unexpected", W, H)
    dest = tmp_path / painted.DIRECTORY
    shutil.copytree(painted.ROOT / painted.DIRECTORY, dest)
    with (dest / "background.png").open("ab") as f:
        f.write(b"changed")
    monkeypatch.setattr(painted, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="digest mismatch"):
        painted.load_bundle(painted.BUNDLE_ID, W, H)
    sb = copy.deepcopy(SB)
    sb["asset_bundle"] = "https://untrusted.example/image"
    assert not storyboard.validate_storyboard(sb, SB["scenes"])["ok"]


def test_painted_reference_is_order_independent_and_preserves_controls():
    ctrl = controls()
    before = {k: v.copy() for k, v in ctrl.items()}
    ref = reference(ctrl)
    late = ref(65).copy()
    ref(1)
    assert np.array_equal(late, reference(ctrl)(65))
    ref.shifted_boundary_frame(B, 1, B)
    for key in ctrl:
        assert np.array_equal(ctrl[key], before[key])


def test_replaced_asset_manifest_invalidates_old_visual_evidence(tmp_path, monkeypatch):
    from test_studio_render_context import _make_bundle, _prepare
    from studio import visual_preflight as vp
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    bundle = _make_bundle(inputs)
    sb = json.loads(bundle["storyboard"].read_text())
    sb["asset_bundle"] = painted.BUNDLE_ID
    bundle["storyboard"].write_text(json.dumps(sb))
    dest = tmp_path / painted.DIRECTORY
    shutil.copytree(painted.ROOT / painted.DIRECTORY, dest)
    monkeypatch.setattr(painted, "ROOT", tmp_path)
    before = _prepare(bundle)
    evidence = tmp_path / "evidence"
    vp.generate_evidence(before, evidence, stage="still", engine_sha="a"*40,
                         run_id=123, run_attempt=1, iteration=0)
    assert vp.validate_evidence(evidence, context=before)["ok"]
    image = dest / "background.png"
    with image.open("ab") as f:
        f.write(b"new asset revision")
    manifest_path = dest / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"]["background.png"] = hashlib.sha256(image.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    after = _prepare(bundle)
    assert before.public_plan["visual_assets"] != after.public_plan["visual_assets"]
    assert not vp.validate_evidence(evidence, context=after)["ok"]


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    out = tmp_path_factory.mktemp("painted-encode")
    wav = out / "tone.wav"
    sf.write(wav, .1*np.sin(np.arange(22050*3)*2*np.pi*440/22050), 22050)
    cases = {"good": controls(), "scene_late": controls(boundary=B+1),
             "accent_missing": controls(accent=None), "accent_late": controls(accent=A+1)}
    paths = {}
    for name, ctrl in cases.items():
        path = out / f"{name}.mp4"
        fn = reference(ctrl)
        render.render_video((fn(f) for f in range(N)), str(wav), str(path),
                            W, H, 30, 1, N)
        paths[name] = str(path)
    return paths


@pytest.mark.parametrize("name", ["good", "scene_late", "accent_missing", "accent_late"])
def test_painted_encoded_timing_faults(encoded, name):
    result = qa.check_rendered_frames(encoded[name], PLAN, Timebase(22050, 30, 1),
                                      W, H, N, reference(controls()),
                                      require_transition_timing=True)
    assert result["pass"] == (name == "good"), result
    if name != "good":
        target = B if name == "scene_late" else A
        assert any(f["frame"] == target and not f["ok"] for f in result["frames"])

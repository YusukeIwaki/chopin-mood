"""Executable storyboard camera: validation, trajectory, whole-frame crop."""
import copy
import numpy as np
import pytest
import soundfile as sf
from p0 import qa as Q
from p0 import render as PR
from p0.timebase import Timebase
from studio import rendering as R
from studio import storyboard as sbmod

W, H, SEED = 180, 320, 20261019
SCENES = [{"id": "scene01"}, {"id": "scene02"}]
CAM = {"camera_zoom_start": 1.0, "camera_zoom_end": 1.18,
       "camera_pan_x_start": -1.0, "camera_pan_x_end": 1.0,
       "camera_pan_y_start": 1.0, "camera_pan_y_end": -1.0}

def _sb(params_list):
    sb = sbmod.default_storyboard(SCENES)
    for sc, p in zip(sb["scenes"], params_list):
        sc["params"] = p
    return sb

def _legacy_params():
    return {"figure_scale": 1.0, "particle_multiplier": 1.0,
            "light_bias": 0.0, "glow_scale": 1.0}

def _ctrl(n=90, scene_at=45):
    ctrl = {"light": np.full(n, 0.55), "zoom": np.ones(n),
            "camx": np.full(n, 0.5), "camy": np.full(n, 0.5),
            "particle": np.full(n, 0.25), "accent_pulse": np.zeros(n),
            "scene_id": np.array([0] * scene_at + [1] * (n - scene_at))}
    for k in range(15):
        if 60 + k < n:
            ctrl["accent_pulse"][60 + k] = float(np.exp(-k / 4.0))
    return ctrl

def test_legacy_storyboard_without_camera_keys_valid():
    sb = _sb([_legacy_params(), _legacy_params()])
    assert sbmod.validate_storyboard(sb, SCENES)["ok"]

def test_camera_boundaries_and_int_values_valid():
    p = dict(_legacy_params(), **CAM)
    p["camera_zoom_start"] = 1
    assert sbmod.validate_storyboard(_sb([p, _legacy_params()]), SCENES)["ok"]

@pytest.mark.parametrize("key,bad", [
    ("camera_zoom_start", 0.99), ("camera_zoom_end", 1.19),
    ("camera_pan_x_start", -1.01), ("camera_pan_x_end", 1.01),
    ("camera_pan_y_start", 2.0), ("camera_pan_y_end", -2.0),
    ("camera_zoom_start", True), ("camera_pan_x_start", False),
    ("camera_zoom_end", "1.1"), ("camera_pan_y_end", None),
    ("camera_zoom_start", float("nan")), ("camera_zoom_end", float("inf")),
    ("camera_pan_x_end", float("-inf")),
])
def test_camera_param_rejections(key, bad):
    p = dict(_legacy_params(), **{key: bad})
    r = sbmod.validate_storyboard(_sb([p, _legacy_params()]), SCENES)
    assert not r["ok"] and any(key in f for f in r["failures"]), r

def test_unknown_camera_like_key_rejected():
    p = dict(_legacy_params(), camera_zoom=1.1)
    assert not sbmod.validate_storyboard(_sb([p, _legacy_params()]), SCENES)["ok"]

def test_scene_progress_start_middle_end_and_oneframe():
    ids = np.array([0] * 11 + [1] + [2] * 5)
    assert R.scene_frame_progress(0, ids) == 0.0
    assert R.scene_frame_progress(10, ids) == 1.0
    assert R.scene_frame_progress(5, ids) == 0.5
    assert R.scene_frame_progress(11, ids) == 0.0
    assert R.scene_frame_progress(16, ids) == 1.0

def test_trajectory_smoothstep_endpoints_and_interior():
    assert R.camera_trajectory(CAM, 0.0) == (1.0, -1.0, 1.0)
    assert R.camera_trajectory(CAM, 1.0) == (1.18, 1.0, -1.0)
    z, px, py = R.camera_trajectory(CAM, 0.5)
    assert (z, px, py) == pytest.approx((1.09, 0.0, 0.0))
    assert R.camera_trajectory({}, 0.7) == (1.0, 0.0, 0.0)
    assert R.camera_trajectory(None, 0.7) == (1.0, 0.0, 0.0)

def test_crop_box_bounded_and_pan_noop_at_zoom1():
    for zoom in (1.0, 1.09, 1.18):
        for pan_x in (-1.0, 0.0, 1.0):
            for pan_y in (-1.0, 0.0, 1.0):
                left, top, cw, ch = R.camera_crop_box(W, H, zoom, pan_x, pan_y)
                assert 0.0 <= left and left + cw <= W + 1e-9
                assert 0.0 <= top and top + ch <= H + 1e-9
    assert R.camera_crop_box(W, H, 1.0, 1.0, -1.0) == (0.0, 0.0, float(W), float(H))

def test_apply_camera_no_black_borders_at_extrema():
    grad = np.zeros((H, W, 3), np.uint8)
    grad[:] = (np.arange(W, dtype=np.uint8)[None, :, None] // 2 + 60)
    for pan_x, pan_y in ((-1, -1), (1, 1), (-1, 1), (1, -1)):
        out = R.apply_camera(grad, 1.18, pan_x, pan_y)
        assert out.shape == grad.shape and int(out.min()) > 20
    assert np.array_equal(R.apply_camera(grad, 1.0, 1.0, -1.0), grad)

def _pals_params(cam_first=False):
    sb = sbmod.default_storyboard(SCENES)
    pals = [s["palette"] for s in sb["scenes"]]
    params = [dict(s["params"], particle_multiplier=0.15) for s in sb["scenes"]]
    if cam_first:
        params[0].update(CAM)
    return pals, params

def test_camera_changes_full_world_not_figure_only():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    pals, plain = _pals_params()
    _, camp = _pals_params(cam_first=True)
    f = 44
    a = R.draw_clean_frame(f, ctrl, W, H, SEED, lay, scene_palettes=pals, scene_params=plain).astype(int)
    b = R.draw_clean_frame(f, ctrl, W, H, SEED, lay, scene_palettes=pals, scene_params=camp).astype(int)
    assert np.abs(a[:70, :70] - b[:70, :70]).mean() > 1.0
    assert np.abs(a[:90, 100:] - b[:90, 100:]).mean() > 1.0
    assert np.abs(a[250:, 40:120] - b[250:, 40:120]).mean() > 1.0

def test_camera_order_independence_reference_and_no_mutation():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    pals, camp = _pals_params(cam_first=True)
    ctrl_before = copy.deepcopy(ctrl)
    params_before = copy.deepcopy(camp)
    fn = R.make_reference_frame_fn(ctrl, W, H, SEED, scene_palettes=pals, scene_params=camp, layouts=lay)
    late_first = fn(44)
    _ = fn(3)
    assert np.array_equal(late_first, fn(44))
    assert np.array_equal(late_first, R.draw_clean_frame(44, ctrl, W, H, SEED, lay, scene_palettes=pals, scene_params=camp))
    for k in ctrl:
        assert np.array_equal(ctrl[k], ctrl_before[k]), k
    assert camp == params_before

def test_figure_scale_cache_exact_key_nearby_scales_order_independent():
    ctrl = _ctrl()
    pals, _ = _pals_params()

    def frame(scale, lay):
        params = [dict(_legacy_params(), figure_scale=scale),
                  _legacy_params()]
        return R.draw_clean_frame(0, ctrl, W, H, SEED, lay,
                                  scene_palettes=pals, scene_params=params)

    for first, second in ((1.0, 1.00001), (1.00001, 1.0)):
        shared = R.make_layouts(SEED, W, H)
        reused_first = frame(first, shared)
        reused_second = frame(second, shared)
        fresh_first = frame(first, R.make_layouts(SEED, W, H))
        fresh_second = frame(second, R.make_layouts(SEED, W, H))
        # The nearby scales must actually produce distinct fresh frames,
        # so the reused-vs-fresh equality below is not vacuous.
        assert not np.array_equal(fresh_first, fresh_second)
        assert np.array_equal(reused_first, fresh_first)
        assert np.array_equal(reused_second, fresh_second)

def test_custom_camera_encoded_render_qa_smoke(tmp_path):
    n, sr = 45, 22050
    ctrl = _ctrl(n=n, scene_at=22)
    pals, camp = _pals_params(cam_first=True)
    wav = tmp_path / "tone.wav"
    y = (0.1 * np.sin(2 * np.pi * 440 * np.arange(sr) / sr) * 32767).astype(np.int16)
    sf.write(str(wav), y, sr, subtype="PCM_16")
    lay = R.make_layouts(SEED, W, H)
    mp4 = tmp_path / "cam.mp4"
    PR.render_video((R.draw_clean_frame(f, ctrl, W, H, SEED, lay, scene_palettes=pals, scene_params=camp) for f in range(n)), str(wav), str(mp4), W, H, 30, 1, n)
    plan = {"clip": {"n_frames": n},
            "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": 22}, {"id": "scene02", "start_frame": 22, "end_frame": n}],
            "accents": [{"frame": 30}]}
    fn = R.make_reference_frame_fn(ctrl, W, H, SEED, scene_palettes=pals, scene_params=camp, layouts=R.make_layouts(SEED, W, H))
    res = Q.check_rendered_frames(str(mp4), plan, Timebase(sr, 30, 1), W, H, n, fn)
    assert res["pass"], res["frames"]

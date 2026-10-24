"""Automatic visual joins preserve DSP times and avoid whole-frame tint steps."""
import copy

import numpy as np
import pytest

from studio import rendering as R


def controls():
    n = 400
    return {"light": np.full(n, .5), "zoom": np.ones(n),
            "camx": np.full(n, .5), "camy": np.full(n, .5),
            "particle": np.zeros(n), "accent_pulse": np.zeros(n),
            "scene_id": np.repeat([0, 1], 200)}


def test_transition_center_is_existing_boundary_and_windows_do_not_overlap():
    ids = np.repeat([0, 1, 2], [180, 20, 180])
    for boundary in (180, 200):
        assert R.scene_transition(boundary, ids) == (boundary - 5, boundary + 5, .5)
    assert R.scene_transition(190, ids) is None
    assert R.scene_transition(0, ids) is None
    assert R.scene_transition(len(ids) - 1, ids) is None
    assert R.scene_transition(1, [0, 1, 2]) is None


def test_visual_styles_are_exact_outside_transition_and_inputs_do_not_move():
    ctrl = controls()
    original = copy.deepcopy(ctrl)
    pals = R.PALETTES[:2]
    saved_pals = copy.deepcopy(pals)
    params = [{"glow_scale": .7, "light_bias": -.1},
              {"glow_scale": 1.1, "light_bias": .1}]
    saved = copy.deepcopy(params)
    assert R.visual_style_for_frame(175, ctrl, pals, params)[0] == pals[0]
    assert R.visual_style_for_frame(225, ctrl, pals, params)[0] == pals[1]
    pal, p = R.visual_style_for_frame(200, ctrl, pals, params)
    assert p["glow_scale"] == pytest.approx(.9)
    assert p["light_bias"] == pytest.approx(0)
    for key in pal:
        assert np.asarray(pal[key]) == pytest.approx(
            (np.asarray(pals[0][key]) + pals[1][key]) / 2, abs=.5)
    assert pals == saved_pals
    assert params == saved
    for key in ctrl:
        assert np.array_equal(ctrl[key], original[key])


def test_rendered_boundary_no_longer_has_a_global_tint_step(monkeypatch):
    ctrl = controls()
    pals = R.PALETTES[:2]
    params = [{"particle_multiplier": 0, "glow_scale": .7},
              {"particle_multiplier": 0, "glow_scale": 1.1}]

    def difference():
        lay = R.make_layouts(20261019, 180, 320)
        frames = [R.draw_clean_frame(f, ctrl, 180, 320, 20261019, lay,
                                    scene_palettes=pals, scene_params=params)
                  for f in (199, 200)]
        return float(np.abs(frames[1].astype(float) - frames[0]).mean())

    joined = difference()
    monkeypatch.setattr(R, "scene_transition", lambda *args: None)
    hard_switch = difference()
    assert joined < 3.0
    assert joined < hard_switch * .35, (joined, hard_switch)


def test_camera_join_is_bounded_and_has_no_boundary_position_jump():
    ctrl = controls()
    params = [{"camera_zoom_start": 1.0, "camera_zoom_end": 1.18,
               "camera_pan_x_end": 1.0},
              {"camera_zoom_start": 1.0, "camera_zoom_end": 1.0,
               "camera_pan_x_start": -1.0, "camera_pan_x_end": -1.0}]
    values = np.array([R.camera_for_frame(f, ctrl, params) for f in range(170, 230)])
    assert np.all((values[:, 0] >= 1.0) & (values[:, 0] <= 1.18))
    assert np.all(np.abs(values[:, 1:]) <= 1.0)
    assert np.max(np.abs(np.diff(values[:, 0]))) < .006
    assert np.max(np.abs(np.diff(values[:, 1]))) < .07

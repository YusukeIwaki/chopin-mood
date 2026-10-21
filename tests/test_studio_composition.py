"""Composition geometry invariants for the shaped Studio renderer.

These tests check mechanical properties only (deterministic shaped
depth profiles, monotone occlusion, bounded slope/continuity, exact
figure grounding, darker worn path, no input mutation, cached vs
fresh equality). They do not measure or claim aesthetic quality.
"""
import copy

import numpy as np
import pytest

from studio import rendering as R
from studio import storyboard as sbmod

W, H, SEED = 180, 320, 20261019
_SB = sbmod.default_storyboard([{"id": "scene01"}, {"id": "scene02"}])
PALS = [s["palette"] for s in _SB["scenes"]]
PARAMS = [dict(s["params"], particle_multiplier=0.15)
          for s in _SB["scenes"]]


def _ctrl(n=90, scene_at=45):
    return {"light": np.full(n, 0.55), "zoom": np.ones(n),
            "camx": np.full(n, 0.5), "camy": np.full(n, 0.5),
            "particle": np.full(n, 0.25), "accent_pulse": np.zeros(n),
            "scene_id": np.array([0] * scene_at + [1] * (n - scene_at))}


def _frame(f, lay, ctrl=None, pals=PALS, params=PARAMS, w=W, h=H):
    return R.draw_clean_frame(f, ctrl if ctrl is not None else _ctrl(),
                              w, h, SEED, lay, scene_palettes=pals,
                              scene_params=params)


@pytest.mark.parametrize("seed", [1, 7, SEED, 42])
def test_profiles_deterministic_across_layouts_and_seeds(seed):
    a = R.terrain_profiles(R.make_layouts(seed, W, H), W, H,
                           camx=0.5, camy=0.5, zoom=1.0, drift=0.3)
    b = R.terrain_profiles(R.make_layouts(seed, W, H), W, H,
                           camx=0.5, camy=0.5, zoom=1.0, drift=0.3)
    for pa, pb in zip(a, b):
        assert np.array_equal(pa, pb)
    other = R.terrain_profiles(R.make_layouts(seed + 1000, W, H), W, H,
                               camx=0.5, camy=0.5, zoom=1.0, drift=0.3)
    assert any(float(np.abs(pa - po).max()) > 1.0
               for pa, po in zip(a, other))


def test_profiles_resolution_invariant_in_fraction():
    small = R.terrain_profiles(R.make_layouts(SEED, W, H), W, H,
                               camx=0.4, camy=0.6, zoom=1.05, drift=0.2)
    big = R.terrain_profiles(R.make_layouts(SEED, 2 * W, 2 * H), 2 * W, 2 * H,
                             camx=0.4, camy=0.6, zoom=1.05, drift=0.2)
    t_small = np.linspace(0, 1, W)
    t_big = np.linspace(0, 1, 2 * W)
    for ps, pb in zip(small, big):
        resampled = np.interp(t_small, t_big, pb / (2 * H))
        assert float(np.abs(resampled - ps / H).max()) < 0.003


@pytest.mark.parametrize("seed", [1, 7, SEED])
@pytest.mark.parametrize("camx,camy,zoom,drift", [
    (0.5, 0.5, 1.0, 0.0), (0.0, 0.0, 0.9, 0.0), (1.0, 1.0, 1.1, 5.0),
    (0.0, 1.0, 1.0, 100.0), (1.0, 0.0, 1.05, 2.5)])
def test_profiles_monotone_occlusion_slope_continuity(
        seed, camx, camy, zoom, drift):
    lay = R.make_layouts(seed, W, H)
    far, mid, near = R.terrain_profiles(
        lay, W, H, camx=camx, camy=camy, zoom=zoom, drift=drift)
    assert float((mid - far).min()) > 0.01 * H
    assert float((near - mid).min()) > 0.01 * H
    assert float(far.min()) >= 0.34 * H
    for prof in (far, mid, near):
        relief = float(prof.max() - prof.min()) / H
        assert 0.04 < relief < 0.22, relief
    # C1 continuity: bounded per-column step. The world is analytic
    # and non-periodic, so the left/right edges need not match.
    for prof in (far, mid, near):
        step = np.abs(np.diff(prof))
        assert float(step.max()) <= 0.012 * H
    far2, mid2, near2 = R.terrain_profiles(
        lay, W, H, camx=camx, camy=camy, zoom=zoom, drift=drift + 0.016)
    for pa, pb in zip((far, mid, near), (far2, mid2, near2)):
        assert float(np.abs(pa - pb).max()) < 0.01 * H


def _at(prof, x, w=W):
    return float(np.interp(x * (w - 1), np.arange(w, dtype=float), prof))


@pytest.mark.parametrize("seed", [1, 7, SEED, 42])
def test_profiles_independent_large_scale_directions(seed):
    lay = R.make_layouts(seed, W, H)
    far, mid, near = R.terrain_profiles(
        lay, W, H, camx=0.5, camy=0.5, zoom=1.0, drift=0.3)
    # Disjoint bounded bands at the default camera (occlusion
    # without any layer copying another's shape).
    assert float(far.max()) < float(mid.min())
    assert float(mid.max()) < float(near.min())
    # Far: high left shoulder into a valley, lower right shoulder.
    assert _at(far, 0.12) < _at(far, 0.55) - 0.06 * H
    assert _at(far, 0.82) < _at(far, 0.55) - 0.025 * H
    assert _at(far, 0.82) > _at(far, 0.12) + 0.03 * H
    # Middle: right mass falling toward the left/centre.
    assert _at(mid, 0.20) > _at(mid, 0.50) + 0.02 * H
    assert _at(mid, 0.50) > _at(mid, 0.78) + 0.02 * H
    # Foreground: left ledge, figure shelf, descent to the right;
    # no third full-width valley (the right end is its low end).
    assert _at(near, 0.02) < _at(near, 0.38) - 0.035 * H
    assert 0.79 * H < _at(near, 0.38) < 0.85 * H
    assert _at(near, 0.90) > _at(near, 0.38) + 0.045 * H
    assert _at(near, 0.90) < 0.94 * H
    # The far and middle high points must not line up.
    far_high = float(np.argmin(far)) / (W - 1)
    mid_high = float(np.argmin(mid)) / (W - 1)
    assert abs(far_high - mid_high) > 0.40, (far_high, mid_high)
    # Alternating diagonals: slope directions are not the parallel
    # ribbons the additive construction produced (corr ~0.6-0.95).
    for a, b in ((far, mid), (mid, near), (far, near)):
        corr = float(np.corrcoef(np.diff(a), np.diff(b))[0, 1])
        assert corr < 0.5, corr


def test_slope_facet_factor_broad_soft_and_layered():
    xn = np.linspace(0, 1, 361)
    ranges = []
    for li in range(3):
        fac = R._slope_facet_factor(li, xn, np.full_like(xn, 0.4))
        ranges.append(float(fac.max() - fac.min()))
        assert np.all((fac > 0.88) & (fac < 1.12))
        # Softly bounded: no harsh outline along x or in depth.
        assert float(np.abs(np.diff(fac)).max()) < 0.005
        at_ridge = R._slope_facet_factor(li, xn, np.zeros_like(xn))
        assert np.allclose(at_ridge, 1.0)
        lit = R._slope_facet_factor(li, np.array([0.66]),
                                    np.array([0.4]))[0]
        shallow = R._slope_facet_factor(li, np.array([0.66]),
                                        np.array([0.02]))[0]
        assert lit > shallow + 0.01
    # Far softer/lower contrast than the firmer foreground.
    assert 0.03 < ranges[0] < ranges[2]
    assert ranges[2] > 0.05


def test_figure_support_uses_fixed_world_anchor_projection():
    lay = R.make_layouts(SEED, W, H)
    # No autonomous figure motion: with the drift held fixed, the
    # frame index alone never moves the column, at any resolution
    # the projected anchor is the same world point, and parallax
    # stays smooth and bounded even for extreme drift.
    for w, h in ((W, H), (2 * W, 2 * H), (90, 160)):
        lay_r = R.make_layouts(SEED, w, h)
        txs = [R.figure_support(lay_r, w, h, f, camx=0.5, camy=0.5,
                                zoom=1.0, drift=0.25)[0]
               for f in (0, 500, 5000)]
        assert txs[0] == txs[1] == txs[2]
        assert txs[0] == pytest.approx(
            R.foreground_screen_x(w, camx=0.5, drift=0.25))
        assert txs[0] / (w - 1) == pytest.approx(
            R.FIGURE_WORLD_X + R._parallax_shift(0.5, 0.25, 2))
    tx_a = R.foreground_screen_x(W, camx=0.5, drift=0.25)
    tx_b = R.foreground_screen_x(W, camx=0.5, drift=0.25 + 0.016)
    assert 0.0 < tx_b - tx_a < 1.0
    for drift in (-100.0, 0.0, 100.0):
        tx = R.foreground_screen_x(W, camx=0.5, drift=drift)
        assert 0.30 * (W - 1) < tx < 0.46 * (W - 1), (drift, tx)
    # Feet stay on the same world feature as parallax moves: the
    # ground height at the projected anchor is drift-invariant.
    grounds = []
    for drift in (0.0, 0.2, 0.4):
        _, _, near = R.terrain_profiles(lay, W, H, camx=0.5, camy=0.5,
                                        zoom=1.0, drift=drift)
        _, _, ground = R.figure_support(
            lay, W, H, 0, camx=0.5, camy=0.5, zoom=1.0, drift=drift,
            near=near)
        grounds.append(ground)
    assert max(grounds) - min(grounds) < 0.01 * H, grounds


def test_figure_support_equals_rendered_near_ground():
    lay = R.make_layouts(SEED, W, H)
    for f, camx, camy, zoom in ((0, 0.5, 0.5, 1.0), (30, 0.2, 0.8, 1.05),
                                (80, 0.9, 0.1, 0.95)):
        drift = f * 0.0016
        _, _, near = R.terrain_profiles(lay, W, H, camx=camx, camy=camy,
                                        zoom=zoom, drift=drift)
        tx, feet, ground = R.figure_support(
            lay, W, H, f, camx=camx, camy=camy, zoom=zoom, drift=drift)
        u = min(W, H) / 180.0
        expected = float(np.interp(tx, np.arange(W, dtype=float), near))
        assert ground == pytest.approx(expected, abs=1e-9)
        assert feet == pytest.approx(expected + 3 * u, abs=1e-9)
        assert 0.78 <= feet / H <= 0.88
        assert 0.0 <= tx < W


def _alpha_runs(fs, y_local, pal):
    layer = R._traveller_layer(fs, pal)
    alpha = np.asarray(layer)[:, :, 3]
    row = int(round((57.0 - y_local) * fs))
    cols = np.where(alpha[row] > 128)[0]
    runs = []
    if len(cols):
        start = prev = int(cols[0])
        for c in cols[1:]:
            if int(c) > prev + 1:
                runs.append((start / fs - 17.0, prev / fs - 17.0))
                start = int(c)
            prev = int(c)
        runs.append((start / fs - 17.0, prev / fs - 17.0))
    return [r for r in runs if r[1] - r[0] > 0.5]


def test_figure_head_coat_arms_legs_geometry():
    pal = R._coerce_palette(PALS[0])
    fs = 3.0
    head_runs = _alpha_runs(fs, 47.0, pal)
    assert head_runs, head_runs
    head_w = max(b - a for a, b in head_runs)
    assert 4.5 <= head_w <= 7.6, head_w
    layer = R._traveller_layer(fs, pal)
    alpha = np.asarray(layer)[:, :, 3]
    col = int(round((0.4 + 17.0) * fs))
    rows = np.where(alpha[:, col] > 128)[0]
    head_rows = [r for r in rows if (57.0 - r / fs) > 42.0]
    head_h = (max(head_rows) - min(head_rows) + 1) / fs
    assert 6.5 <= head_h <= 10.0, head_h
    assert head_h > head_w
    coat_runs = _alpha_runs(fs, 24.0, pal)
    coat_w = max(b - a for a, b in coat_runs)
    assert 12.5 <= coat_w <= 17.5, coat_w
    torso_runs = _alpha_runs(fs, 28.0, pal)
    assert min(a for a, _ in torso_runs) >= -9.6
    leg_runs = _alpha_runs(fs, 7.0, pal)
    assert len(leg_runs) >= 2, leg_runs
    widths = sorted((b - a for a, b in leg_runs), reverse=True)
    assert widths[0] > widths[1] + 0.4, leg_runs
    hx = int(round((10.7 + 17.0) * fs))
    hy = int(round((57.0 - 27.4) * fs))
    assert int(alpha[max(0, hy - 1):hy + 2, max(0, hx - 1):hx + 2].max()) > 128


def test_figure_grounded_shadow_and_path_in_render():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    f = 10
    frame = _frame(f, lay, ctrl=ctrl)
    tx, feet, _ = R.figure_support(lay, W, H, f, camx=0.5, camy=0.5,
                                   zoom=1.0, drift=f * 0.0016)
    boot = frame[int(feet) - 3:int(feet), int(tx) - 4:int(tx) + 2]
    assert float(boot.max()) < 90
    lum = frame.astype(float).mean(axis=2)
    y0 = int(feet)
    under = lum[y0:y0 + 3, int(tx) - 6:int(tx) + 7].mean()
    side = lum[y0:y0 + 3, int(tx) + 26:int(tx) + 38].mean()
    assert under < side - 1.0, (under, side)
    u = min(W, H) / 180.0
    span = H - feet
    diffs = []
    for t in (0.35, 0.5, 0.65, 0.8):
        cx, cy = R._cubic((tx, feet - u),
                          (tx + 10 * u, feet + 0.30 * span),
                          (tx - 12 * u, feet + 0.66 * span),
                          (tx + 6 * u, H + 4 * u), t)
        yi, xi = int(cy), int(cx)
        on = lum[max(0, yi - 2):yi + 3, max(0, xi - 2):xi + 3].mean()
        off = lum[max(0, yi - 2):yi + 3, max(0, xi - 20):max(1, xi - 14)].mean()
        diffs.append(float(off - on))
    assert float(np.mean(diffs)) > 0.5, diffs


def _dark_figure_bottom(frame):
    # Figure cloth is neutral-dark (blue barely above red), unlike
    # the blue ground/sky, whose blue channel sits well above red.
    sub = frame[int(H * 0.45):, :, :].astype(int)
    mask = (sub.max(axis=2) < 30) & ((sub[:, :, 2] - sub[:, :, 0]) < 11)
    ys, _ = np.where(mask)
    return int(ys.max()) + int(H * 0.45) if len(ys) else None


def test_camera_does_not_detach_figure():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl(n=30, scene_at=30)
    ctrl["scene_id"] = np.zeros(30, dtype=int)
    plain = [dict(PARAMS[0]), dict(PARAMS[0])]
    cam = [dict(PARAMS[0], camera_zoom_start=1.12, camera_zoom_end=1.12,
                camera_pan_x_start=0.4, camera_pan_x_end=0.4,
                camera_pan_y_start=-0.2, camera_pan_y_end=-0.2),
           dict(PARAMS[0])]
    f = 10
    tx, feet, _ = R.figure_support(lay, W, H, f, camx=0.5, camy=0.5,
                                   zoom=1.0, drift=f * 0.0016)
    a = _frame(f, lay, ctrl=ctrl, pals=[PALS[0], PALS[0]], params=plain)
    bottom_a = _dark_figure_bottom(a)
    assert bottom_a is not None and abs(bottom_a - feet) <= 5, (bottom_a, feet)
    b = _frame(f, lay, ctrl=ctrl, pals=[PALS[0], PALS[0]], params=cam)
    zoom, pan_x, pan_y = R.camera_for_frame(f, ctrl, cam)
    left, top, cw, ch = R.camera_crop_box(W, H, zoom, pan_x, pan_y)
    feet_t = (feet - top) / ch * H
    bottom_b = _dark_figure_bottom(b)
    assert bottom_b is not None and abs(bottom_b - feet_t) <= 7, (
        bottom_b, feet_t)


def test_world_coherent_across_scenes():
    lay = R.make_layouts(SEED, W, H)
    base = _ctrl(n=30, scene_at=30)
    ctrl_a = dict(base, scene_id=np.zeros(30, dtype=int))
    ctrl_b = dict(base, scene_id=np.ones(30, dtype=int))
    pals = [PALS[0], PALS[0]]
    params = [dict(PARAMS[0]), dict(PARAMS[0])]
    fa = _frame(12, lay, ctrl=ctrl_a, pals=pals, params=params)
    fb = _frame(12, lay, ctrl=ctrl_b, pals=pals, params=params)
    assert np.array_equal(fa, fb)


def test_no_input_mutation_and_cached_fresh_out_of_order():
    ctrl = _ctrl()
    pals = copy.deepcopy(PALS)
    params = copy.deepcopy(PARAMS)
    ctrl_before = copy.deepcopy(ctrl)
    shared = R.make_layouts(SEED, W, H)
    terrain_before = copy.deepcopy(shared["terrain"])
    late = _frame(50, shared, ctrl=ctrl, pals=pals, params=params)
    _ = _frame(10, shared, ctrl=ctrl, pals=pals, params=params)
    again = _frame(50, shared, ctrl=ctrl, pals=pals, params=params)
    fresh = _frame(50, R.make_layouts(SEED, W, H), ctrl=ctrl,
                   pals=pals, params=params)
    assert np.array_equal(late, again)
    assert np.array_equal(late, fresh)
    assert shared["terrain"] == terrain_before
    for k in ctrl:
        assert np.array_equal(ctrl[k], ctrl_before[k]), k
    assert pals == copy.deepcopy(PALS)
    assert params == copy.deepcopy(PARAMS)


def test_lantern_anchor_bloom_still_localised():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    ctrl["accent_pulse"][10] = 1.0
    calm = _ctrl()
    fa = _frame(10, lay, ctrl=ctrl).astype(int)
    fc = _frame(10, lay, ctrl=calm).astype(int)
    tx, feet, _ = R.figure_support(lay, W, H, 10, camx=0.5, camy=0.5,
                                   zoom=1.0, drift=10 * 0.0016)
    fs = min(W, H) / 180.0
    lx, ly = int(tx + 11 * fs), int(feet - 27 * fs)
    centre = np.abs(fa[ly - 1:ly + 2, lx - 1:lx + 2]
                    - fc[ly - 1:ly + 2, lx - 1:lx + 2]).mean()
    corner = np.abs(fa[4:8, 4:8] - fc[4:8, 4:8]).mean()
    assert centre > 4.0 and centre > corner + 2.0, (centre, corner)

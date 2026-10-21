"""Decorative star / airborne-particle controls: functional tests only.

These tests verify the zero-multiplier contract and the independent
response of each decorative field. They make no aesthetic claim and
use no brightness floor: subtle decorations are checked by exact
pixel equality / inequality under an identical control, frame and
seeded layout.

Every test builds its layout BEFORE any count patch, so the seeded
RNG draw sequence (placements, radii, phases) is never changed by
patching. ``N_STARS`` / ``N_PARTICLES`` are only disabled at draw
time via pytest monkeypatch; no layout is regenerated while a count
is patched, and field counts / seeded placements are not changed
in the renderer itself.
"""
import copy

import numpy as np
import pytest

from studio import rendering as R
from studio import storyboard as sbmod

W, H, SEED, N = 180, 320, 20261019, 90

_SB = sbmod.default_storyboard([{"id": "scene01"}, {"id": "scene02"}])
PALS = [s["palette"] for s in _SB["scenes"]]
PARAMS = [dict(s["params"], particle_multiplier=0.15)
          for s in _SB["scenes"]]


def _ctrl(particle=0.9, n=N):
    return {"light": np.full(n, 0.55), "zoom": np.ones(n),
            "camx": np.full(n, 0.5), "camy": np.full(n, 0.5),
            "particle": np.full(n, particle),
            "accent_pulse": np.zeros(n),
            "scene_id": np.array([0] * 30 + [1] * (n - 30))}


def _params(mult):
    return [dict(p, particle_multiplier=float(mult)) for p in PARAMS]


def _render(lay, ctrl, mult, f=10):
    return R.draw_clean_frame(f, ctrl, W, H, SEED, lay,
                              scene_palettes=PALS,
                              scene_params=_params(mult))


def test_multiplier_zero_equals_both_counts_zero(monkeypatch):
    lay = R.make_layouts(SEED, W, H)  # before any patch
    ctrl = _ctrl(particle=0.9)
    fz = _render(lay, ctrl, 0.0)
    fo = _render(lay, ctrl, 1.0)
    assert not np.array_equal(fz, fo)
    monkeypatch.setattr(R, "N_STARS", 0)
    monkeypatch.setattr(R, "N_PARTICLES", 0)
    both_off_one = _render(lay, ctrl, 1.0)
    both_off_zero = _render(lay, ctrl, 0.0)
    # With other pixels exactly equal: multiplier-zero must be the
    # same image as disabling both decorative counts at draw time.
    assert np.array_equal(fz, both_off_one)
    assert np.array_equal(fz, both_off_zero)


def test_stars_isolated_multiplier_controls_pixels(monkeypatch):
    lay = R.make_layouts(SEED, W, H)  # before any patch
    ctrl = _ctrl(particle=0.9)
    # Isolate stars: airborne particles disabled at draw time only.
    monkeypatch.setattr(R, "N_PARTICLES", 0)
    zero = _render(lay, ctrl, 0.0)
    half = _render(lay, ctrl, 0.5)
    one = _render(lay, ctrl, 1.0)
    assert not np.array_equal(zero, one)
    assert not np.array_equal(zero, half)
    assert not np.array_equal(half, one)
    # Determinism under identical control/frame/layout.
    assert np.array_equal(one, _render(lay, ctrl, 1.0))
    # Audio density does not drive the star field: with particles
    # disabled, changing only ctrl["particle"] leaves stars alone.
    assert np.array_equal(
        one, _render(lay, _ctrl(particle=0.25), 1.0))
    # Disabling the star count as well collapses to multiplier-zero.
    monkeypatch.setattr(R, "N_STARS", 0)
    assert np.array_equal(zero, _render(lay, ctrl, 1.0))


def test_particles_isolated_multiplier_and_density_control_pixels(
        monkeypatch):
    lay = R.make_layouts(SEED, W, H)  # before any patch
    ctrl = _ctrl(particle=0.9)
    # Isolate airborne particles: stars disabled at draw time only.
    monkeypatch.setattr(R, "N_STARS", 0)
    zero = _render(lay, ctrl, 0.0)
    half = _render(lay, ctrl, 0.5)
    one = _render(lay, ctrl, 1.0)
    assert not np.array_equal(zero, one)
    assert not np.array_equal(zero, half)
    assert not np.array_equal(half, one)
    assert np.array_equal(one, _render(lay, ctrl, 1.0))
    # Audio-linked density controls this field on its own: changing
    # density changes pixels, and density zero disables particles
    # even at multiplier one (exactly the multiplier-zero image).
    low = _render(lay, _ctrl(particle=0.25), 1.0)
    assert not np.array_equal(low, one)
    assert np.array_equal(zero, _render(lay, _ctrl(particle=0.0), 1.0))
    # Setting the particle count to zero eliminates this field too.
    monkeypatch.setattr(R, "N_PARTICLES", 0)
    assert np.array_equal(zero, _render(lay, ctrl, 1.0))


def test_count_zero_eliminates_only_its_field():
    lay = R.make_layouts(SEED, W, H)  # before any patch
    ctrl = _ctrl(particle=0.9)
    both_on = _render(lay, ctrl, 1.0)
    fz = _render(lay, ctrl, 0.0)
    assert not np.array_equal(both_on, fz)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "N_STARS", 0)
        particles_only = _render(lay, ctrl, 1.0)
        # Only stars are gone: particles still respond, and turning
        # the multiplier off under this patch returns to fz.
        assert not np.array_equal(particles_only, both_on)
        assert not np.array_equal(particles_only, fz)
        assert np.array_equal(_render(lay, ctrl, 0.0), fz)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "N_PARTICLES", 0)
        stars_only = _render(lay, ctrl, 1.0)
        assert not np.array_equal(stars_only, both_on)
        assert not np.array_equal(stars_only, fz)
        assert np.array_equal(_render(lay, ctrl, 0.0), fz)
    # The two single-field images are themselves different fields.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "N_STARS", 0)
        particles_only = _render(lay, ctrl, 1.0)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "N_PARTICLES", 0)
        stars_only = _render(lay, ctrl, 1.0)
    assert not np.array_equal(stars_only, particles_only)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(R, "N_STARS", 0)
        mp.setattr(R, "N_PARTICLES", 0)
        assert np.array_equal(_render(lay, ctrl, 1.0), fz)


def test_decorations_deterministic_and_control_unchanged():
    lay = R.make_layouts(SEED, W, H)  # before any patch
    ctrl = _ctrl(particle=0.9)
    before = copy.deepcopy(ctrl)
    params_before = copy.deepcopy(_params(1.0))
    params = _params(1.0)
    first = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=params)
    _ = _render(lay, ctrl, 0.0)
    again = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=params)
    assert np.array_equal(first, again)
    assert params == params_before
    for k in ctrl:
        assert np.array_equal(ctrl[k], before[k]), k

"""Figure alpha compositing: fabric shading must not punch holes.

The traveller layer is an RGBA figure drawn over an opaque coat.
Translucent fabric shading (dark/light folds, scarf tail) painted
over that coat must leave the covered torso opaque: composited over
any background, the torso interior must show the figure, never the
background showing through as stripes. The space outside the figure
must stay transparent and the silhouette edge must keep partial
(antialiased / rim) alpha values.
"""
import numpy as np
import pytest
from PIL import Image

from studio import rendering as R
from studio import storyboard as sbmod

W, H, SEED = 180, 320, 20261019

_SB = sbmod.default_storyboard([{"id": "scene01"}, {"id": "scene02"}])
PALS = [R._coerce_palette(s["palette"]) for s in _SB["scenes"]]


def _torso_interior(arr: np.ndarray, fs: float,
                    x_range=(-4.0, 2.0)) -> np.ndarray:
    """Interior torso block in layer pixels for figure scale ``fs``.

    Layer coordinates map local figure units (x in [-17, 17],
    y in [-3, 57] upward from the feet) by x_px = (x + 17) * fs,
    y_px = (57 - y) * fs. The default block x in [-4.0, 2.0],
    y in [22, 34] sits well inside the coat at those heights (coat
    edges are near x = -7 and x = +6), away from any silhouette
    antialias margin, and spans the dark fabric fold.
    """
    x0 = int(round((x_range[0] + 17.0) * fs))
    x1 = int(round((x_range[1] + 17.0) * fs))
    y0 = int(round((57.0 - 34.0) * fs))
    y1 = int(round((57.0 - 22.0) * fs))
    return arr[y0:y1, x0:x1]


@pytest.mark.parametrize("fs", [1.0, 1.5, 2.0, 2.97, 3.0, 3.03, 4.0])
@pytest.mark.parametrize("pal", PALS)
def test_torso_interior_is_opaque_across_scales_and_palettes(fs, pal):
    layer = R._traveller_layer(fs, pal)
    interior = _torso_interior(np.asarray(layer), fs)
    assert interior.shape[0] >= 4 and interior.shape[1] >= 4
    assert np.all(interior[:, :, 3] == 255), (
        fs, int((interior[:, :, 3] < 255).sum()),
        int(interior[:, :, 3].min()))


@pytest.mark.parametrize("fs", [2.0, 2.97, 3.0, 3.03, 4.0])
@pytest.mark.parametrize("pal", PALS)
def test_light_fold_torso_interior_is_opaque(fs, pal):
    # The light fold sits nearer the coat's right edge, so exact
    # opacity is asserted at scales where that block is clear of
    # the downscale antialias margin.
    layer = R._traveller_layer(fs, pal)
    interior = _torso_interior(np.asarray(layer), fs,
                               x_range=(2.6, 4.0))
    assert np.all(interior[:, :, 3] == 255), (
        fs, int((interior[:, :, 3] < 255).sum()),
        int(interior[:, :, 3].min()))


def test_torso_shading_is_still_present():
    # Fixing the holes must not delete the fabric shading: the torso
    # interior is not one flat colour (coat plus darker/lighter fold
    # tones remain distinguishable).
    layer = R._traveller_layer(3.0, PALS[0])
    interior = _torso_interior(np.asarray(layer), 3.0,
                               x_range=(-4.0, 4.5))
    colours = np.unique(interior[:, :, :3].reshape(-1, 3), axis=0)
    assert len(colours) >= 3, colours
    assert int(interior[:, :, 0].max()) - int(interior[:, :, 0].min()) >= 5


def _composite_over(layer: Image.Image, background: Image.Image):
    return np.asarray(Image.alpha_composite(
        background.convert("RGBA"), layer.convert("RGBA")))


def test_torso_interior_is_background_independent():
    fs = 3.0
    layer = R._traveller_layer(fs, PALS[0])
    w, h = layer.size
    white = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
    yy, xx = np.mgrid[0:h, 0:w]
    checker_rgb = np.where(((xx // 6) + (yy // 6)) % 2 == 0,
                           255, 0).astype(np.uint8)
    checker = Image.fromarray(
        np.dstack([checker_rgb] * 3 + [np.full((h, w), 255, np.uint8)]),
        "RGBA")
    interiors = [_torso_interior(_composite_over(layer, bg), fs,
                                 x_range=(-4.0, 4.5))[:, :, :3]
                 for bg in (white, black, checker)]
    assert np.array_equal(interiors[0], interiors[1])
    assert np.array_equal(interiors[0], interiors[2])


def test_outer_background_transparent_and_edge_alpha_preserved():
    layer = R._traveller_layer(3.0, PALS[0])
    alpha = np.asarray(layer)[:, :, 3]
    # Corners / far outside the silhouette stay fully transparent.
    assert np.all(alpha[:5, :5] == 0)
    assert np.all(alpha[:5, -5:] == 0)
    assert np.all(alpha[-5:, :5] == 0)
    assert np.all(alpha[-5:, -5:] == 0)
    assert float((alpha == 0).mean()) > 0.3
    # Antialiased contours and the moon rim keep partial alpha: the
    # layer is not simply forced to an opaque rectangle.
    partial = int(((alpha > 0) & (alpha < 255)).sum())
    assert partial > 100, partial


def test_traveller_layer_deterministic_across_interleaved_calls():
    first = np.asarray(R._traveller_layer(3.0, PALS[0])).copy()
    _ = R._traveller_layer(2.97, PALS[1])
    _ = R._traveller_layer(3.03, PALS[0])
    again = np.asarray(R._traveller_layer(3.0, PALS[0]))
    assert np.array_equal(first, again)


def test_renderer_figure_cache_order_independent_nearby_scales():
    ctrl = {"light": np.full(4, 0.55), "zoom": np.ones(4),
            "camx": np.full(4, 0.5), "camy": np.full(4, 0.5),
            "particle": np.full(4, 0.25), "accent_pulse": np.zeros(4),
            "scene_id": np.zeros(4, dtype=int)}
    pals = [s["palette"] for s in _SB["scenes"]]

    def frame(scale, lay):
        params = [{"figure_scale": scale, "particle_multiplier": 0.15,
                   "light_bias": 0.0, "glow_scale": 1.0}] * 2
        return R.draw_clean_frame(0, ctrl, W, H, SEED, lay,
                                  scene_palettes=pals, scene_params=params)

    for first_scale, second_scale in ((1.0, 1.00001), (1.00001, 1.0)):
        shared = R.make_layouts(SEED, W, H)
        reused_first = frame(first_scale, shared)
        reused_second = frame(second_scale, shared)
        fresh_first = frame(first_scale, R.make_layouts(SEED, W, H))
        fresh_second = frame(second_scale, R.make_layouts(SEED, W, H))
        assert not np.array_equal(fresh_first, fresh_second)
        assert np.array_equal(reused_first, fresh_first)
        assert np.array_equal(reused_second, fresh_second)

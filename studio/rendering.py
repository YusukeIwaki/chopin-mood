"""Clean Studio renderer: original procedural illustration, no diagnostics.

This is the Studio (creative) counterpart of :mod:`p0.render`. The P0
renderer rasterises a validation caption and an 8x8 diagnostic marker;
those are useful for the offline P0 comparison and must never reach a
creative Studio video, so Studio frames are drawn here instead. The P0
``draw_frame`` behaviour is untouched.

What the storyboard can actually steer (honest capability list):

* the six RGB palette colours per scene (sky/moon/hill/glow colours),
  applied only at the deterministic DSP scene ids;
* ``figure_scale``, ``particle_multiplier``, ``light_bias`` and
  ``glow_scale`` per scene;
* the optional executable camera per scene: ``camera_zoom_start/end``
  (1.0-1.18, default 1.0) and ``camera_pan_x_start/end`` /
  ``camera_pan_y_start/end`` (-1.0-1.0, default 0.0). The camera
  transforms the ENTIRE rendered world (sky, moon, terrain, figure) by
  a deterministic subpixel crop/resample. Zoom/pan are interpolated
  with smoothstep easing over the current scene's inclusive frame
  range ``[start_frame, end_frame-1]``, derived from
  ``ctrl['scene_id']`` without moving any event or boundary. Pan is a
  fraction of the available crop margin, so pan has no effect at zoom
  1.0, and zoom >= 1.0 guarantees the crop stays inside the frame (no
  black/edge fill at any bound);
* the per-frame control arrays built by DSP (``light``, ``zoom``,
  ``camx``/``camy``, ``particle``, ``accent_pulse``, ``scene_id``),
  which continue to drive accents/light/parallax and are never
  rewritten by the storyboard camera.

Descriptive storyboard fields such as ``motif`` and ``camera`` are text
metadata: they are *not* executable instructions and have no effect on
the pixels. Camera *prose* alone changes nothing; only the numeric
camera params above move the framing. Timings, scene boundaries and
accents are deterministic inputs; this module never moves them, adds
cuts, or imposes a BPM grid.

Imagery is original and procedural (Pillow/NumPy only): a soft night
sky with diffuse moonlight, deliberately shaped layered landforms
with a narrow worn earth path, a grounded traveller figure (head,
coat, arms, legs), and a very sparse drifting particle/star field.
There are no captions, logos, QA markers, expanding rings or
concentric halos in any frame. The "procedural prototype, not
finished anime" disclaimer lives in provenance/storyboard metadata,
never in the raster.

Determinism: ``draw_clean_frame(f, ...)`` is a pure function of
``(f, ctrl, seed, palettes, params)``. All layouts come from
``RandomState(seed)`` and per-frame grain reseeds from ``(seed, f)``,
so frames may be evaluated in any order with identical pixels.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter

#: Fallback palettes (same night hues as the legacy placeholder scenes).
PALETTES = [
    {"sky_top": (8, 12, 38), "sky_bot": (38, 52, 96), "far": (30, 44, 78),
     "near": (16, 26, 52), "moon": (235, 238, 245), "glow": (255, 214, 150)},
    {"sky_top": (16, 10, 34), "sky_bot": (64, 40, 88), "far": (48, 34, 74),
     "near": (26, 18, 46), "moon": (240, 230, 240), "glow": (255, 190, 170)},
    {"sky_top": (6, 20, 30), "sky_bot": (24, 68, 84), "far": (20, 56, 70),
     "near": (10, 32, 44), "moon": (232, 244, 244), "glow": (190, 255, 230)},
]

N_STARS = 110
N_PARTICLES = 36
_PALETTE_KEYS = ("sky_top", "sky_bot", "far", "near", "moon", "glow")


def _coerce_palette(raw: dict) -> dict:
    out = {}
    for k in _PALETTE_KEYS:
        v = raw[k]
        out[k] = tuple(int(max(0, min(255, int(c)))) for c in v)
    return out


def _smooth_wrap(vals: np.ndarray, passes: int = 3) -> np.ndarray:
    """Circular smoothing so ridge control points form irregular hills."""
    out = np.asarray(vals, dtype=np.float64)
    kernel = np.array([1.0, 2.0, 3.0, 2.0, 1.0])
    kernel /= kernel.sum()
    for _ in range(passes):
        ext = np.concatenate([out[-2:], out, out[:2]])
        out = np.convolve(ext, kernel, mode="valid")
    return out


def make_layouts(seed: int, w: int, h: int) -> dict:
    """Seed-only layouts (normalised coords; texture sized to frame)."""
    rng = np.random.RandomState(seed)
    stars = rng.rand(N_STARS, 2)
    stars[:, 1] *= 0.58
    lay = {
        "stars": stars,
        "star_r": 0.6 + rng.rand(N_STARS) * 1.3,
        "star_phase": rng.rand(N_STARS) * 6.283,
        "parts": rng.rand(N_PARTICLES, 2),
        "part_speed": 0.4 + rng.rand(N_PARTICLES) * 0.9,
        "part_r": 0.7 + rng.rand(N_PARTICLES) * 1.1,
        "ridge_far": _smooth_wrap(rng.rand(28)),
        "ridge_mid": _smooth_wrap(rng.rand(24)),
        "ridge_near": _smooth_wrap(rng.rand(20)),
        "speckles": rng.rand(260, 2),
        "clouds": np.column_stack([rng.rand(7), rng.rand(7) * 0.4,
                                   0.10 + rng.rand(7) * 0.16]),
        "streaks": rng.rand(36, 2),
        "seed": int(seed),
    }
    tex_small = rng.randn(36, 64).astype(np.float32)
    tex_img = Image.fromarray(
        np.clip((tex_small - tex_small.min())
                / (tex_small.max() - tex_small.min() + 1e-9) * 255,
                0, 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
    # Down/up resample: broad tonal variation only, never the tall
    # thin block streaks a direct 36x64 -> frame upscale would leave.
    tex_img = tex_img.resize((max(8, w // 6), max(8, h // 6)),
                             Image.BICUBIC).resize((w, h), Image.BICUBIC)
    tex_img = tex_img.filter(
        ImageFilter.GaussianBlur(radius=max(2.0, min(w, h) * 0.006)))
    lay["_texture"] = (np.asarray(tex_img, dtype=np.float32) / 255.0 - 0.5)
    # Fine dry-ground mottling (cached, seed/resolution only): small
    # soft features, applied to the foreground slope so the ground
    # has low-contrast texture following the land, not flat fill.
    gtex_small = rng.randn(max(4, h // 4), max(4, w // 4)).astype(np.float32)
    gtex_img = Image.fromarray(
        np.clip((gtex_small - gtex_small.min())
                / (gtex_small.max() - gtex_small.min() + 1e-9) * 255,
                0, 255).astype(np.uint8)).resize((w, h), Image.BICUBIC)
    gtex_img = gtex_img.filter(
        ImageFilter.GaussianBlur(radius=max(0.8, min(w, h) * 0.003)))
    lay["_ground_texture"] = (
        np.asarray(gtex_img, dtype=np.float32) / 255.0 - 0.5)
    lay["_w"], lay["_h"] = int(w), int(h)
    lay["terrain"] = _terrain_seed_params(int(seed))
    return lay


def _ridge_y(x_norm: np.ndarray, ctrl_pts: np.ndarray, base: float,
             amp: float, shift: float) -> np.ndarray:
    """Irregular ridge height (pixels) from wrapped control points."""
    n = len(ctrl_pts)
    pos = (x_norm * (n - 1) + shift) % n
    i0 = np.floor(pos).astype(int) % n
    i1 = (i0 + 1) % n
    frac = pos - np.floor(pos)
    s = frac * frac * (3.0 - 2.0 * frac)  # smoothstep: continuous ridge
    vals = ctrl_pts[i0] * (1.0 - s) + ctrl_pts[i1] * s
    return base + amp * (vals - 0.5) * 2.0


def _mix(a, b, t: float) -> tuple:
    return tuple(int(round(a[i] * (1 - t) + b[i] * t)) for i in range(3))


# --- shaped depth profiles (terrain composition) ---------------------------
#
# The three landforms are independent analytic silhouettes with
# distinctly different large-scale directions, in disjoint bounded
# height ranges so occlusion (far above middle above foreground at
# every x) holds by construction without copying one layer's shape
# into the next and without any clipping:
#
# * far: a high left shoulder (x ~0.12, y ~0.44) descending to a
#   valley (x ~0.55), plus a lower right distant shoulder;
# * middle: a stronger mass on the right (x ~0.78) falling toward
#   the left/centre (x ~0.20) -- its high point deliberately does
#   not line up with the far high point;
# * foreground: a ledge entering from the left (x 0, y ~0.75),
#   supporting the figure near the world anchor (x ~0.38, y ~0.82),
#   then sloping down to the right (y ~0.89-0.92), never echoing a
#   third full-width valley.
#
# Each curve is a smooth (C1) analytic form -- Gaussians and
# smoothstep ramps on the real line, not wrapped periodic bumps --
# with only small seed-only variation, so there is no wrap seam to
# hide. Parallax is a smooth bounded shift (see _parallax_shift),
# the same seeded world serves every scene, and parameters come
# only from the seed, cached in the layout by make_layouts.

def _gauss(t: np.ndarray, center: float, sigma: float) -> np.ndarray:
    """Gaussian bump on the real line (C1, no wrap seam)."""
    d = (np.asarray(t, dtype=np.float64) - float(center)) / float(sigma)
    return np.exp(-0.5 * d * d)


def _sramp(t: np.ndarray, a: float, b: float) -> np.ndarray:
    """Clamped smoothstep ramp, 0 below ``a`` and 1 above ``b`` (C1)."""
    r = np.clip((np.asarray(t, dtype=np.float64) - float(a))
                / max(1e-9, float(b) - float(a)), 0.0, 1.0)
    return r * r * (3.0 - 2.0 * r)


#: Fixed foreground world anchor (normalised x) carrying the figure.
FIGURE_WORLD_X = 0.38
_LAYER_PARALLAX = (0.45, 0.75, 1.0)


def _parallax_shift(camx: float, drift: float, layer: int) -> float:
    """Smooth bounded parallax shift (normalised x) for one layer.

    A tanh-bounded function of the audio-linked camx and the slow
    drift, scaled per depth (far moves least). Bounded for any
    drift, so the non-periodic world never slides off the frame,
    and C1-smooth so no frame-to-frame step can appear.
    """
    raw = (float(camx) - 0.5) * 0.04 + float(drift) * 0.05
    bounded = 0.06 * float(np.tanh(raw / 0.06))
    return bounded * _LAYER_PARALLAX[int(layer)]


def foreground_screen_x(w: int, camx: float = 0.5,
                        drift: float = 0.0) -> float:
    """Screen x (pixels) of the fixed foreground world anchor.

    The anchor ``FIGURE_WORLD_X`` is a world coordinate: the
    foreground profile is evaluated at ``t = x_norm - shift``, so
    the screen column projecting that world point is
    ``(FIGURE_WORLD_X + shift) * (w - 1)`` with the same foreground
    parallax shift. Feet placed here stay on the same world feature
    however the camera/drift moves.
    """
    return (FIGURE_WORLD_X + _parallax_shift(camx, drift, 2)) \
        * float(max(0, int(w) - 1))


def _terrain_seed_params(seed: int) -> dict:
    """Seed-only shape parameters for the independent profiles."""
    rng = np.random.RandomState((int(seed) ^ 0x5EEDCAFE) & 0xFFFFFFFF)

    def u(lo, hi):
        return float(rng.uniform(lo, hi))

    return {
        "far": {"base": u(0.564, 0.572), "hl": u(0.128, 0.140),
                "cl": u(0.105, 0.135), "sl": u(0.145, 0.175),
                "hr": u(0.062, 0.074), "cr": u(0.795, 0.845),
                "sr": u(0.130, 0.160), "a1": u(0.003, 0.006),
                "p1": u(0.0, 1.0), "a2": u(0.0015, 0.003),
                "p2": u(0.0, 1.0)},
        "mid": {"low": u(0.706, 0.714), "drop": u(0.090, 0.098),
                "pk": u(0.009, 0.013), "cr": u(0.755, 0.805),
                "sr": u(0.155, 0.185), "a1": u(0.0025, 0.005),
                "p1": u(0.0, 1.0), "a2": u(0.001, 0.0025),
                "p2": u(0.0, 1.0)},
        "near": {"base": u(0.757, 0.763), "d1": u(0.057, 0.063),
                 "d2": u(0.083, 0.090), "a1": u(0.002, 0.004),
                 "p1": u(0.0, 1.0), "a2": u(0.001, 0.002),
                 "p2": u(0.0, 1.0)},
        "centers": (0.505, 0.655, 0.830),
    }


def _far_frac(t: np.ndarray, p: dict) -> np.ndarray:
    """Far silhouette: high left shoulder, valley, lower right shoulder."""
    y = np.full_like(np.asarray(t, dtype=np.float64), p["base"])
    y = y - p["hl"] * _gauss(t, p["cl"], p["sl"])
    y = y - p["hr"] * _gauss(t, p["cr"], p["sr"])
    y = y + p["a1"] * np.cos(2.0 * np.pi * (t - p["p1"]))
    y = y + p["a2"] * np.cos(4.0 * np.pi * (t - p["p2"]))
    return y


def _mid_frac(t: np.ndarray, p: dict) -> np.ndarray:
    """Middle silhouette: right mass falling toward the left/centre."""
    y = np.full_like(np.asarray(t, dtype=np.float64), p["low"])
    y = y - p["drop"] * _sramp(t, 0.18, 0.82)
    y = y - p["pk"] * _gauss(t, p["cr"], p["sr"])
    y = y + p["a1"] * np.cos(2.0 * np.pi * (t - p["p1"]))
    y = y + p["a2"] * np.cos(4.0 * np.pi * (t - p["p2"]))
    return y


def _near_frac(t: np.ndarray, p: dict) -> np.ndarray:
    """Foreground ledge: left entry, figure shelf, descent to the right."""
    y = np.full_like(np.asarray(t, dtype=np.float64), p["base"])
    y = y + p["d1"] * _sramp(t, 0.02, 0.38)
    y = y + p["d2"] * _sramp(t, 0.40, 0.95)
    y = y + p["a1"] * np.cos(2.0 * np.pi * (t - p["p1"]))
    y = y + p["a2"] * np.cos(4.0 * np.pi * (t - p["p2"]))
    return y


def terrain_profiles(lay: dict, w: int, h: int, camx: float = 0.5,
                     camy: float = 0.5, zoom: float = 1.0,
                     drift: float = 0.0):
    """(far, mid, near) ridge heights in pixels for this frame.

    Pure function of (lay, w, h, camx, camy, zoom, drift): the seeded
    layout is only read, and identical inputs give identical arrays
    in any evaluation order. The three silhouettes are independent
    analytic curves in disjoint bounded ranges, so far lies strictly
    above mid strictly above near at every column with no clipping,
    and the foreground passes through the figure shelf near the
    world anchor. Parallax shifts each depth by a smooth bounded
    offset (no wrap seam), so ordering and continuity survive
    x/time/camera motion at any drift.
    """
    params = lay.get("terrain") or _terrain_seed_params(lay.get("seed", 0))
    x_norm = np.arange(int(w), dtype=np.float64) / max(1, int(w) - 1)
    fracs = []
    for li, fn in enumerate((_far_frac, _mid_frac, _near_frac)):
        t = x_norm - _parallax_shift(camx, drift, li)
        fracs.append(fn(t, params[("far", "mid", "near")[li]]))
    amp = float(min(1.15, max(0.85, float(zoom))))
    off = (float(camy) - 0.5) * 0.03
    out = []
    for frac, center in zip(fracs, params["centers"]):
        y = (center + (frac - center) * amp + off) * float(h)
        out.append(np.asarray(y, dtype=np.float64))
    return out[0], out[1], out[2]


def figure_support(lay: dict, w: int, h: int, f: int, camx: float = 0.5,
                   camy: float = 0.5, zoom: float = 1.0,
                   drift: float | None = None, near=None):
    """(tx, feet_y, ground_y): figure anchor on the foreground.

    ``ground_y`` is the rendered near profile interpolated at the
    figure column and ``feet_y`` is exactly ``ground_y + 3u``; the
    contact shadow and boots use the same ``feet_y``, so the figure
    can never float above or sink into its ledge. The figure has no
    autonomous motion: its column is the fixed world anchor
    ``FIGURE_WORLD_X`` projected by the same foreground parallax
    mapping that evaluates its support (see foreground_screen_x),
    so the feet stay on the same world feature and never slide
    relative to the ground. The frame index only supplies the
    default drift; it never translates the figure by itself. The
    position does not depend on the scene id: one world, one
    stance, all scenes.
    """
    if drift is None:
        drift = float(f) * 0.0016
    u = min(int(w), int(h)) / 180.0
    if near is None:
        _, _, near = terrain_profiles(lay, w, h, camx=camx, camy=camy,
                                      zoom=zoom, drift=drift)
    tx_c = float(min(max(foreground_screen_x(w, camx=camx, drift=drift),
                         0.0), float(w) - 1.0))
    ground = float(np.interp(tx_c, np.arange(int(w), dtype=np.float64),
                             near))
    return tx_c, ground + 3.0 * u, ground


# (lit centre, lit width, lit amp, shadow centre, shadow width, shadow amp)
_FACETS = ((0.64, 0.30, 0.045, 0.30, 0.24, -0.035),
           (0.70, 0.28, 0.065, 0.26, 0.22, -0.050),
           (0.66, 0.26, 0.090, 0.24, 0.22, -0.070))


def _slope_facet_factor(layer_index: int, x_norm, depth) -> np.ndarray:
    """Broad tonal facet factor (1.0 = neutral) for one landform.

    Two large softly bounded internal facets per layer -- a moonlit
    plane toward the moon side and a shadow plane away from it --
    whose vertical envelope is a smooth function of depth below the
    ridge, so the facets follow the slope instead of running as a
    column gradient. The far layer is softer/lower contrast and the
    foreground firmer, with no hard outline anywhere (Gaussian in
    x, smoothstep in depth). Pure and deterministic.
    """
    lc, lw, la, sc, sw, sa = _FACETS[int(layer_index)]
    xn, dep = np.broadcast_arrays(
        np.asarray(x_norm, dtype=np.float64),
        np.asarray(depth, dtype=np.float64))
    env = _sramp(dep, 0.06, 0.30) * (1.0 - _sramp(dep, 0.62, 0.95))
    spatial = la * _gauss(xn, lc, lw) + sa * _gauss(xn, sc, sw)
    return 1.0 + env * spatial


# --- executable storyboard camera (pure helpers) --------------------------
#
# The camera is a bounded crop/resample of the whole rendered world.
# Zoom is >= 1.0, so the crop window always lies inside the source
# frame: there is never any black or edge-replicated fill, even at the
# pan extrema. Pan is expressed as a fraction of the available crop
# margin ((frame - crop) / 2 on each axis), so pan at zoom 1.0 is an
# exact no-op. Trajectories are eased with smoothstep over the current
# scene's inclusive frame range [start_frame, end_frame-1]; scene
# bounds are derived from ctrl['scene_id'] runs only, so no event,
# plan or boundary is ever moved and no structure is mutated.

CAMERA_ZOOM_RANGE = (1.0, 1.18)
CAMERA_PAN_RANGE = (-1.0, 1.0)
_CAMERA_KEYS = ("camera_zoom_start", "camera_zoom_end",
                "camera_pan_x_start", "camera_pan_x_end",
                "camera_pan_y_start", "camera_pan_y_end")


def smoothstep01(t: float) -> float:
    """Clamped smoothstep easing on [0, 1]."""
    x = float(min(1.0, max(0.0, float(t))))
    return x * x * (3.0 - 2.0 * x)


def scene_frame_progress(frame: int, scene_ids) -> float:
    """Progress of ``frame`` through its scene's inclusive range.

    The current scene is the maximal run of equal ``scene_ids`` values
    containing ``frame``; progress is ``(f - start) / (end-1 - start)``
    over the inclusive range [start, end-1]. A one-frame scene has
    progress 0.0. Pure: inputs are never mutated and the result does
    not depend on evaluation order.
    """
    ids = np.asarray(scene_ids)
    f = int(frame)
    cur = int(ids[f])
    start = f
    while start > 0 and int(ids[start - 1]) == cur:
        start -= 1
    end = f + 1
    while end < len(ids) and int(ids[end]) == cur:
        end += 1
    last = end - 1
    if last <= start:
        return 0.0
    return float(min(1.0, max(0.0, (f - start) / (last - start))))


def camera_trajectory(params: dict | None, progress: float):
    """(zoom, pan_x, pan_y) at ``progress``, smoothstep-eased.

    Missing keys default to zoom 1.0 / pan 0.0 (legacy storyboards).
    Values are defensively clamped to the validated ranges. The input
    dict is only read, never mutated.
    """
    p = params or {}
    e = smoothstep01(progress)

    def _lerp(start_key, end_key, lo, hi, default):
        a = float(p.get(start_key, default))
        b = float(p.get(end_key, default))
        return float(min(hi, max(lo, a + (b - a) * e)))

    zoom = _lerp("camera_zoom_start", "camera_zoom_end",
                 CAMERA_ZOOM_RANGE[0], CAMERA_ZOOM_RANGE[1], 1.0)
    pan_x = _lerp("camera_pan_x_start", "camera_pan_x_end",
                  CAMERA_PAN_RANGE[0], CAMERA_PAN_RANGE[1], 0.0)
    pan_y = _lerp("camera_pan_y_start", "camera_pan_y_end",
                  CAMERA_PAN_RANGE[0], CAMERA_PAN_RANGE[1], 0.0)
    return zoom, pan_x, pan_y


def camera_crop_box(w: int, h: int, zoom: float, pan_x: float,
                    pan_y: float):
    """Float crop box (left, top, crop_w, crop_h) inside the frame.

    ``crop_w = w / zoom``; the centre is offset by ``pan * margin``
    where ``margin = (w - crop_w) / 2`` (likewise vertically), and the
    box is clamped inside [0, w-crop_w] x [0, h-crop_h]. At zoom 1.0
    the box is exactly the full frame for any pan.
    """
    z = float(min(CAMERA_ZOOM_RANGE[1],
                  max(CAMERA_ZOOM_RANGE[0], float(zoom))))
    cw = float(w) / z
    ch = float(h) / z
    px = float(min(1.0, max(-1.0, float(pan_x))))
    py = float(min(1.0, max(-1.0, float(pan_y))))
    left = (float(w) - cw) / 2.0 + px * (float(w) - cw) / 2.0
    top = (float(h) - ch) / 2.0 + py * (float(h) - ch) / 2.0
    left = float(min(max(left, 0.0), max(0.0, float(w) - cw)))
    top = float(min(max(top, 0.0), max(0.0, float(h) - ch)))
    return left, top, cw, ch


def camera_for_frame(f: int, ctrl: dict, scene_params=None):
    """(zoom, pan_x, pan_y) for frame ``f`` from its scene's params.

    Uses the same per-scene params (indexed by ``ctrl['scene_id']``)
    as the rest of the renderer, so the creative render and the QA
    reference callback always agree. Pure and order-independent.
    """
    scene_id = int(ctrl["scene_id"][f])
    spar: dict = {}
    if scene_params:
        spar = scene_params[scene_id % len(scene_params)] or {}
    progress = scene_frame_progress(f, ctrl["scene_id"])
    return camera_trajectory(spar, progress)


def apply_camera(frame: np.ndarray, zoom: float, pan_x: float,
                 pan_y: float) -> np.ndarray:
    """Deterministic subpixel crop+resample of the whole frame.

    Returns the input unchanged (full-frame box, e.g. zoom 1.0 with
    any pan) so default storyboards keep their exact pixels and cost.
    Otherwise resamples the float crop box back to (w, h) with
    bicubic interpolation; the box never leaves the source frame, so
    no black/edge fill can appear.
    """
    h, w = int(frame.shape[0]), int(frame.shape[1])
    left, top, cw, ch = camera_crop_box(w, h, zoom, pan_x, pan_y)
    if cw >= w - 1e-9 and ch >= h - 1e-9:
        return frame
    img = Image.fromarray(np.ascontiguousarray(frame, dtype=np.uint8),
                          "RGB")
    out = img.resize((w, h), Image.BICUBIC,
                     box=(left, top, left + cw, top + ch))
    return np.asarray(out, dtype=np.uint8)


# --- smooth curve helpers (figure silhouette / path) ----------------------

def _cubic(p0, p1, p2, p3, t: float):
    mt = 1.0 - t
    a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
            a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _bezier_chain(start, segments, steps: int = 14):
    """Sample a chain of cubic Bezier segments into a point list."""
    pts = [(float(start[0]), float(start[1]))]
    cur = (float(start[0]), float(start[1]))
    for c1, c2, end in segments:
        for i in range(1, steps + 1):
            pts.append(_cubic(cur, c1, c2, end, i / steps))
        cur = (float(end[0]), float(end[1]))
    return pts


def _tapered_stroke(start, segments, w_start: float, w_end: float,
                    steps: int = 14):
    """Closed polygon around a Bezier centreline with tapered width."""
    centre = _bezier_chain(start, segments, steps=steps)
    left, right = [], []
    n = len(centre)
    for i, (x, y) in enumerate(centre):
        j0, j1 = max(0, i - 1), min(n - 1, i + 1)
        dx, dy = centre[j1][0] - centre[j0][0], centre[j1][1] - centre[j0][1]
        norm = max(1e-9, float(np.hypot(dx, dy)))
        nx, ny = -dy / norm, dx / norm
        half = 0.5 * (w_start + (w_end - w_start) * (i / max(1, n - 1)))
        left.append((x + nx * half, y + ny * half))
        right.append((x - nx * half, y - ny * half))
    return left + right[::-1]


def _traveller_layer(fs: float, pal: dict) -> Image.Image:
    """Static weary traveller silhouette as a small RGBA layer.

    Drawn on a locally supersampled mask (4x, figure-sized only, so
    there is no full-frame supersampling cost) from sampled cubic
    Bezier outlines, then downscaled. The pose is still: a small
    head (about 6-7 units wide, 8 tall) tilted forward and down with
    a subtle rear hair contour, sloped asymmetric shoulders on an
    A-line coat with an uneven hem near y=14, a rear arm held close
    to the body, the near arm bent to the lantern hand anchor, and a
    contrapposto stance (weight on one planted boot, the other leg
    narrower, receding, at a different angle). No face, no walking
    or bobbing. Coat shading is 2-3 opaque tonal planes plus a
    restrained moon-side rim; translucent accents are source-over
    composited so they can never punch holes. Local coordinates are
    in ``fs`` units: x in [-17, 17], y in [-3, 57] upward from feet.
    """
    ss = 4
    wf = max(2, int(np.ceil(34 * fs)))
    hf = max(2, int(np.ceil(60 * fs)))
    img = Image.new("RGBA", (wf * ss, hf * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def pts(shape, dx=0.0, dy_up=0.0):
        return [((x + 17.0 + dx) * fs * ss, (57.0 - (y + dy_up)) * fs * ss)
                for x, y in shape]

    body = (10, 10, 17)
    coat_col, leg_col = (13, 13, 21), (9, 9, 15)
    hair_col, neck_col = (6, 6, 11), (12, 10, 14)
    scarf_col = _mix(pal["glow"], pal["near"], 0.85)
    rim_col = _mix(pal["glow"], body, 0.50)

    # A-line coat: rounded higher back shoulder, front shoulder
    # sloping lower, sides flaring gently to an uneven hem near
    # y=14 (front corner higher than the back). About 15 units wide.
    coat = _bezier_chain((-7.8, 13.8), [
        ((-8.4, 20.0), (-8.0, 28.0), (-6.8, 36.0)),
        ((-6.2, 39.5), (-4.8, 41.2), (-2.6, 42.0)),
        ((-1.2, 42.6), (0.6, 42.3), (2.2, 41.6)),
        ((3.4, 40.8), (4.4, 39.0), (5.2, 36.5)),
        ((6.4, 28.0), (7.0, 20.0), (6.6, 14.6)),
        ((4.2, 13.2), (0.5, 15.0), (-2.0, 13.5)),
        ((-4.5, 12.6), (-6.4, 14.8), (-7.8, 13.8))])
    # Small bowed head, about 6-7 wide and 8 tall: crown tipped
    # forward/down toward the lantern side, chin tucked, and hair
    # falling in one subtle contour down the back to the neck. One
    # leaning mass, never a round ball; no face is drawn.
    head = _bezier_chain((1.4, 42.9), [
        ((3.0, 43.8), (3.4, 45.4), (3.1, 47.6)),
        ((2.9, 49.3), (1.9, 50.6), (0.4, 51.1)),
        ((-1.2, 51.0), (-2.3, 50.2), (-2.7, 48.6)),
        ((-3.0, 46.8), (-2.9, 45.0), (-2.5, 43.6)),
        ((-1.8, 42.8), (-0.4, 42.7), (1.4, 42.9))])
    # Contrapposto legs: the back leg bears the weight, near
    # vertical and planted flat; the front leg is narrower, angled
    # away and receding, its trouser ending at a different height.
    leg_planted = _bezier_chain((-5.0, 15.6), [
        ((-5.2, 10.0), (-4.9, 5.0), (-4.3, 2.6)),
        ((-3.6, 2.1), (-2.7, 2.1), (-2.2, 2.5)),
        ((-1.7, 6.0), (-1.5, 11.0), (-1.4, 15.2)),
        ((-3.0, 16.0), (-4.1, 15.9), (-5.0, 15.6))])
    leg_receding = _bezier_chain((0.6, 15.2), [
        ((1.4, 10.0), (2.8, 5.5), (3.8, 3.0)),
        ((4.4, 2.6), (5.0, 2.6), (5.3, 3.0)),
        ((4.9, 7.0), (4.0, 11.0), (3.4, 14.8)),
        ((2.2, 15.5), (1.3, 15.4), (0.6, 15.2))])

    def boot(x0):
        return _bezier_chain((x0, 0.2), [
            ((x0 - 0.1, 1.6), (x0 + 0.4, 2.6), (x0 + 1.8, 2.8)),
            ((x0 + 3.2, 2.6), (x0 + 3.8, 1.4), (x0 + 4.1, 0.5)),
            ((x0 + 4.0, 0.1), (x0 + 2.1, 0.0), (x0, 0.2))])

    boot_planted = boot(-5.0)
    boot_receding = _bezier_chain((3.3, 0.2), [
        ((3.2, 1.4), (3.6, 2.3), (4.6, 2.5)),
        ((5.6, 2.3), (6.1, 1.3), (6.3, 0.5)),
        ((6.2, 0.1), (4.8, 0.0), (3.3, 0.2))])
    # Rear arm held close against the coat edge (drawn behind the
    # coat, so only its outer sliver reads); near arm bent at the
    # elbow toward the lantern hand anchor (10.7, 27.4).
    arm_rear = _tapered_stroke((-5.9, 37.4),
                               [((-7.0, 31.0), (-6.7, 25.0),
                                 (-5.4, 20.6))],
                               2.7, 1.9)
    arm_front = _tapered_stroke((4.9, 36.8), [
        ((6.2, 34.0), (7.6, 32.0), (8.0, 30.2)),
        ((9.2, 29.0), (10.0, 28.0), (10.7, 27.4))],
        3.1, 2.1)
    neck = [(-1.2, 43.2), (1.5, 43.0), (1.2, 40.4), (-0.9, 40.5)]
    scarf = _bezier_chain((-2.9, 42.5), [
        ((-1.4, 43.1), (0.9, 42.9), (2.7, 42.0)),
        ((2.4, 41.0), (1.9, 40.5), (1.2, 40.2)),
        ((-0.4, 39.9), (-2.0, 40.3), (-2.9, 41.1)),
        ((-3.1, 41.7), (-3.0, 42.1), (-2.9, 42.5))])
    tail = _tapered_stroke((-2.3, 41.0),
                           [((-3.9, 39.6), (-4.7, 37.6), (-4.1, 35.3))],
                           1.5, 0.8)
    # Opaque coat tonal planes (back shadow / moonlit front), with
    # irregular diagonal boundaries: shading follows the coat form
    # instead of running as long parallel torso seams.
    plane_shadow = [(-7.2, 14.2), (-6.6, 30.0), (-5.2, 38.5),
                    (-2.4, 41.6), (0.2, 41.0), (-0.8, 30.0),
                    (-2.4, 14.8)]
    plane_light = [(1.6, 38.0), (4.9, 36.2), (6.3, 20.0),
                   (6.2, 15.0), (3.2, 13.9), (2.6, 26.0),
                   (1.9, 34.0)]

    silhouette = [leg_planted, leg_receding, boot_planted,
                  boot_receding, coat, arm_rear, arm_front, head]
    # Restrained moon-side edge light. This is the base layer (only
    # transparency underneath), drawn as one uniform union offset
    # toward the moon; the opaque body shapes below overwrite its
    # interior part, leaving a thin rim following the contours.
    for shape in silhouette:
        d.polygon(pts(shape, dx=0.55, dy_up=0.35), fill=rim_col + (115,))
    # Rear arm first, so the coat overlaps its inner edge and it
    # reads as held close to the body, not a separated tube.
    for shape, col in ((leg_planted, leg_col), (leg_receding, leg_col),
                       (boot_planted, leg_col), (boot_receding, leg_col),
                       (arm_rear, coat_col), (coat, coat_col)):
        d.polygon(pts(shape), fill=col + (255,))
    # Opaque tonal planes over the coat base: a darker back plane
    # and a moonlit front plane. Both are fully opaque, so the torso
    # interior stays solid at every scale (no new transparent holes).
    d.polygon(pts(plane_shadow), fill=(11, 11, 18, 255))
    d.polygon(pts(plane_light),
              fill=_mix(coat_col, pal["glow"], 0.12) + (255,))
    d.polygon(pts(arm_front), fill=coat_col + (255,))
    d.polygon(pts(neck), fill=neck_col + (255,))
    # The scarf tail is also translucent over the coat, so composite
    # it the same way; unclipped, because its free-hanging part is a
    # real garment shape that must keep its own alpha over the
    # background while the part over the coat becomes opaque.
    tail_overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(tail_overlay).polygon(
        pts(tail), fill=scarf_col + (235,))
    img = Image.alpha_composite(img, tail_overlay)
    d = ImageDraw.Draw(img)
    d.polygon(pts(scarf), fill=scarf_col + (255,))
    d.polygon(pts(head), fill=hair_col + (255,))
    return img.resize((wf, hf), Image.LANCZOS)


def draw_clean_frame(f: int, ctrl: dict, w: int, h: int, seed: int,
                     lay: dict, scene_palettes=None,
                     scene_params=None) -> np.ndarray:
    """Render one clean RGB frame. Pure function of (f, ctrl, seed)."""
    scene_id = int(ctrl["scene_id"][f])
    if scene_palettes:
        pal = _coerce_palette(scene_palettes[scene_id % len(scene_palettes)])
    else:
        pal = PALETTES[scene_id % len(PALETTES)]
    spar = {}
    if scene_params:
        spar = scene_params[scene_id % len(scene_params)] or {}
    particle_mult = float(spar.get("particle_multiplier", 1.0))
    light = float(np.clip(float(ctrl["light"][f])
                          + float(spar.get("light_bias", 0.0)), 0.0, 1.0))
    zoom = float(ctrl["zoom"][f])
    camx = float(ctrl["camx"][f])
    camy = float(ctrl["camy"][f])
    dens = float(np.clip(float(ctrl["particle"][f]) * particle_mult, 0.0, 1.0))
    acc = float(np.clip(float(ctrl["accent_pulse"][f]), 0.0, 1.0))
    glow_scale = float(spar.get("glow_scale", 1.0))
    figure_scale = float(spar.get("figure_scale", 1.0))

    u = min(w, h) / 180.0  # coordinate unit: adapts figure/detail scale
    xs = np.arange(w, dtype=np.float64)
    x_norm = xs / max(1, w - 1)
    yy = np.linspace(0, 1, h, dtype=np.float64)

    # sky: soft vertical gradient, gently lifted by the slow light
    top = np.array(pal["sky_top"], np.float64)
    bot = np.array(pal["sky_bot"], np.float64)
    grad = (yy ** 1.15)[:, None, None]
    img = np.broadcast_to(
        top[None, None, :] * (1 - grad) + bot[None, None, :] * grad,
        (h, w, 3)).copy()
    img *= (0.93 + 0.10 * light)
    # atmospheric haze toward the horizon (depth, not a band edge)
    haze = np.exp(-((yy - 0.58) ** 2) / (2 * 0.10 ** 2))[:, None, None]
    img = img * (1 - 0.18 * haze) \
        + np.array(pal["far"], float)[None, None, :] * (0.18 * haze)

    # moon: one diffuse falloff (no rings, no concentric halos)
    mx, my = w * 0.72, h * 0.17
    dx = xs[None, :] - mx
    dy = yy[:, None] * h - my
    dist = np.sqrt(dx * dx + dy * dy)
    sigma = (0.20 * min(w, h)) * glow_scale * (0.9 + 0.25 * light)
    glow_amt = (0.16 + 0.10 * light + 0.10 * acc)
    img += np.array(pal["glow"], float)[None, None, :] * (
        np.exp(-(dist ** 2) / (2 * sigma ** 2)) * glow_amt)[:, :, None]
    moon_r = 0.055 * min(w, h) * (0.9 + 0.2 * glow_scale)
    disc = np.clip((moon_r - dist) / max(1.0, 0.25 * moon_r) + 0.5, 0, 1)
    img = img * (1 - disc[:, :, None]) \
        + np.array(pal["moon"], float)[None, None, :] * disc[:, :, None]

    tex = lay.get("_texture")
    if tex is None or tex.shape != (h, w):
        tex = np.zeros((h, w), dtype=np.float32)
    gtex = lay.get("_ground_texture")
    if gtex is None or gtex.shape != (h, w):
        gtex = np.zeros((h, w), dtype=np.float32)

    # Layered terrain: deliberately shaped landforms (see
    # terrain_profiles) with slow parallax drift. Distant layers
    # dissolve into the haze with soft feathered edges; nearer layers
    # keep firmer edges. Directional tonal planes (slope facing the
    # moon plus a broad brightening toward the moon side) keep the
    # slopes from reading as flat parallel colour bands.
    drift = f * 0.0016  # slow environment motion even if camx is still
    ypix = yy[:, None] * h
    haze_arr = np.array(_mix(pal["sky_bot"], pal["far"], 0.45), float)
    far_r, mid_r, near_r = terrain_profiles(
        lay, w, h, camx=camx, camy=camy, zoom=zoom, drift=drift)
    layers = []
    for li, (ridge, col) in enumerate((
        (far_r, pal["far"]),
        (mid_r, _mix(pal["far"], pal["near"], 0.45)),
        (near_r, pal["near"]),
    )):
        layers.append((ridge, np.array(col, float)))
        depth = np.clip((ypix - ridge[None, :])
                        / np.maximum(1.0, h - ridge[None, :]), 0, 1)
        atmo = (0.42, 0.22, 0.06)[li]  # far layers sit deeper in haze
        col_arr = np.array(col, float) * (1.0 - atmo) + haze_arr * atmo
        slope = np.gradient(ridge)
        k = np.ones(17) / 17.0  # smooth so shading cannot stripe
        slope = np.convolve(np.pad(slope, 8, mode="edge"), k,
                            mode="valid")[:len(slope)]
        shade = np.clip(1.0 - slope * 0.24, 0.93, 1.07)[None, :, None]
        moon_grad = (0.95 + 0.08 * x_norm)[None, :, None]
        # Foreground ground deepens more strongly (dry slope falling
        # into shadow toward the bottom); upper depths stay gentle.
        if li < 2:
            dshade = 0.25 * depth[:, :, None]
        else:
            dshade = 0.55 * (depth[:, :, None] ** 1.5)
        layer_col = (col_arr[None, None, :] * (1 - dshade)
                     + np.array((4, 5, 10), float)[None, None, :] * dshade)
        facet = _slope_facet_factor(li, x_norm, depth)
        layer_col = layer_col * shade * moon_grad * facet[:, :, None] \
            + tex[:, :, None] * 6.0
        if li == 2:
            layer_col = layer_col + gtex[:, :, None] \
                * (5.0 + 10.0 * depth[:, :, None])
        feather = (0.020, 0.011, 0.005)[li] * h
        alpha = np.clip((ypix - ridge[None, :] + feather * 0.5)
                        / max(1.0, feather), 0.0, 1.0)
        alpha = alpha * alpha * (3.0 - 2.0 * alpha)
        img = layer_col * alpha[:, :, None] + img * (1 - alpha[:, :, None])

    # Traveller ground position: feet and contact shadow sit on the
    # exact rendered foreground surface (see figure_support).
    tx_c, feet_y, _ground_y = figure_support(
        lay, w, h, f, camx=camx, camy=camy, zoom=zoom, drift=drift,
        near=near_r)
    fs = figure_scale * u
    # The figure is static (no bob, no walking loop): feet stay planted
    # at feet_y. The lantern hangs from the raised hand; this ONE
    # position drives both the accent bloom below and the lantern dot
    # drawn with the figure, so they can never drift apart.
    fy = feet_y
    lx, ly = tx_c + 11 * fs, fy - 27 * fs

    # Worn earth path: a narrow track grounded exactly at the
    # traveller's feet, widening gradually in perspective down the
    # foreground slope. Its colour is the surrounding ground made
    # slightly darker and desaturated (never a pale luminous ribbon
    # or spotlight), with low-contrast texture following the land.
    if h - feet_y > 4:
        span = h - feet_y
        p0 = (tx_c, feet_y - 1 * u)
        p1 = (tx_c + 10 * u, feet_y + 0.30 * span)
        p2 = (tx_c - 12 * u, feet_y + 0.66 * span)
        p3 = (tx_c + 6 * u, h + 4 * u)
        centre = [_cubic(p0, p1, p2, p3, t / 28.0) for t in range(29)]
        left, right = [], []
        for i, (cxp, cyp) in enumerate(centre):
            t = i / 28.0
            half = (1.6 + 8.5 * t ** 1.35) * u \
                * (1.0 + 0.07 * np.sin(t * 7.0))
            left.append((cxp - half, cyp))
            right.append((cxp + half, cyp))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).polygon(left + right[::-1], fill=255)
        mask = mask.filter(
            ImageFilter.GaussianBlur(radius=max(1.0, 1.4 * u)))
        pa = np.asarray(mask, dtype=np.float64) / 255.0
        fade = 1.0 - 0.25 * np.clip((ypix - feet_y)
                                    / max(1.0, span), 0, 1)
        pa = pa * fade * (0.85 + 0.30 * (tex.astype(np.float64) + 0.5))
        pa = np.clip(pa, 0.0, 1.0) * 0.62
        _pn = _mix(pal["near"], pal["far"], 0.18)
        _lum = int(round(0.299 * _pn[0] + 0.587 * _pn[1]
                         + 0.114 * _pn[2]))
        _desat = _mix(_pn, (_lum, _lum, _lum), 0.40)
        path_col = np.array(tuple(int(round(c * 0.62)) for c in _desat),
                            float)
        # The worn surface follows the ground's own deepening shade
        # downslope, so it stays darker than its surroundings at
        # every depth instead of flattening into a ribbon.
        row_shade = 1.0 - 0.30 * np.clip((ypix - feet_y)
                                         / max(1.0, span), 0, 1)
        img = img * (1 - pa[:, :, None]) \
            + (path_col[None, None, :] * row_shade[:, :, None]) \
            * pa[:, :, None]

    # soft accent bloom at the lantern (diffuse, localised; not a ring,
    # not a whole-frame flash). Shares (lx, ly) with the lantern dot.
    if acc > 0.01:
        d2 = (xs[None, :] - lx) ** 2 + ((yy[:, None] * h) - ly) ** 2
        img += np.array(pal["glow"], float)[None, None, :] * (
            np.exp(-d2 / (2 * (0.10 * min(w, h)) ** 2))
            * (0.38 * acc))[:, :, None]

    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(canvas, "RGBA")

    # No moonlit edge line on the path: regularly spaced bright
    # dashes read as stitching/road markings. The worn path stays
    # grounded by its dark, low-contrast surface alone.

    # soft drifting clouds (blurred blobs -> atmospheric depth)
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cloud_col = _mix(pal["sky_bot"], pal["moon"], 0.25)
    for i, (cx, cy, cr) in enumerate(lay["clouds"]):
        px = ((cx + 0.008 * f * (0.4 + 0.13 * i)
               + camx * 10 * (i + 1)) % 1.15 - 0.07) * w
        py = cy * h
        r = cr * min(w, h)
        od.ellipse([px - r, py - r * 0.32, px + r, py + r * 0.32],
                   fill=cloud_col + (16,))
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"),
        overlay.filter(ImageFilter.GaussianBlur(radius=max(1.0, 6 * u)))
    ).convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")

    # stars: part of the decorative field, gated by particle_multiplier
    # (a near-zero multiplier truly empties the sky decoration)
    star_frac = float(np.clip(particle_mult, 0.0, 1.0))
    n_star = int(round(N_STARS * star_frac
                       * (0.35 + 0.65 * (0.4 + 0.6 * light))))
    if n_star > 0:
        sx = (lay["stars"][:n_star, 0] * w + camx * 8 * u + drift * 30) % w
        sy = lay["stars"][:n_star, 1] * h
        for x, y, r, ph in zip(sx, sy, lay["star_r"][:n_star],
                               lay["star_phase"][:n_star]):
            tw = 0.4 + 0.6 * abs(np.sin(0.03 * f + ph))
            a = int(24 + 40 * tw)
            rr = r * u * 0.24
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(228, 233, 250, a))

    # terrain speckles (texture on the slopes, parallax-shifted)
    sp = lay["speckles"]
    spx = (sp[:, 0] * w + camx * 14 * u) % w
    for x, yn in zip(spx[::6], sp[::6, 1]):
        ridge_here = float(np.interp(x, xs, layers[1][0]))
        y = ridge_here + yn * (h - ridge_here) * 0.85
        if y < h - 2:
            d.ellipse([x - u, y - u * 0.6, x + u, y + u * 0.6],
                      fill=(210, 215, 235, 11))

    # (The worn earth path was integrated into the terrain above as
    # a feathered, texture-modulated mask; no wedge, bright ribbon
    # or floating water shimmer is drawn in the dry foreground.)

    # traveller: static silhouette (see _traveller_layer),
    # grounded by a soft contact shadow. No bob, no walking loop.
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse(
        [tx_c - 9.5 * fs, fy - 0.8 * fs, tx_c + 10.5 * fs, fy + 2.8 * fs],
        fill=(0, 0, 0, 95))
    canvas = Image.alpha_composite(
        canvas.convert("RGBA"),
        shadow.filter(ImageFilter.GaussianBlur(
            radius=max(1.0, 1.1 * u)))).convert("RGB")
    fig_key = (float(fs), pal["glow"], pal["near"])
    fig_cache = lay.setdefault("_fig_cache", {})
    if fig_cache.get("key") != fig_key:
        fig_cache.clear()
        fig_cache["key"] = fig_key
        fig_cache["layer"] = _traveller_layer(fs, pal)
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(
        fig_cache["layer"],
        dest=(int(round(tx_c - 17 * fs)), int(round(fy - 57 * fs))))
    canvas = canvas_rgba.convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    # lantern dot at the SAME (lx, ly) as the accent bloom above
    # (brightens with the accent pulse; stays a subdued point, never
    # a ring/flash). A hairline links it to the raised hand.
    d.line([tx_c + 10.7 * fs, fy - 27.4 * fs, lx, ly - 1.2 * fs],
           fill=(10, 10, 17, 200), width=max(1, int(0.35 * fs)))
    lr = (1.7 + 1.5 * acc) * fs
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr],
              fill=pal["glow"] + (int(145 + 95 * acc),))
    cr = 0.6 * fs
    d.ellipse([lx - cr, ly - cr, lx + cr, ly + cr],
              fill=_mix(pal["glow"], pal["moon"], 0.4) + (190,))

    # sparse drifting particles (count follows audio-linked density)
    n_part = int(round(dens * N_PARTICLES))
    if n_part > 0:
        px = (lay["parts"][:n_part, 0] * w
              + 0.10 * f * lay["part_speed"][:n_part] * u) % w
        py = (lay["parts"][:n_part, 1] * h - 0.22 * f * u) % h
        for x, y, r in zip(px, py, lay["part_r"][:n_part]):
            rr = r * u * 0.35
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(255, 240, 210, 32))

    # executable storyboard camera: transform the ENTIRE rendered
    # world (sky/moon/terrain/path/figure) by the bounded subpixel
    # crop/resample for this frame's scene. Identity (exact pixels,
    # no cost) for legacy/default params. Grain is added afterwards
    # so it stays frame grain, not stretched world texture.
    base = np.asarray(canvas, dtype=np.uint8)
    cam_zoom, cam_pan_x, cam_pan_y = camera_for_frame(
        f, ctrl, scene_params)
    base = apply_camera(base, cam_zoom, cam_pan_x, cam_pan_y)

    # deterministic per-frame grain (order-independent, as in P0)
    grng = np.random.RandomState(
        (seed ^ (0x9E3779B9 + f * 0x85EBCA6B)) & 0xFFFFFFFF)
    grain = grng.randint(-2, 3, size=(h, w, 1)).astype(np.int16)
    arr = np.clip(base.astype(np.int16) + grain, 0, 255)
    return arr.astype(np.uint8)


def make_reference_frame_fn(ctrl: dict, w: int, h: int, seed: int,
                            scene_palettes=None, scene_params=None,
                            layouts: dict | None = None):
    """Deterministic reference-frame callback for encoded QA.

    Returns ``fn(frame_index) -> RGB ndarray`` with an internal cache
    so repeated probes of the same frame are drawn only once. Frames
    are computed on demand; nothing is written to disk.
    """
    lay = layouts if layouts is not None else make_layouts(seed, w, h)
    cache: dict[int, np.ndarray] = {}

    def fn(frame_index: int) -> np.ndarray:
        f = int(frame_index)
        if f not in cache:
            cache[f] = draw_clean_frame(f, ctrl, w, h, seed, lay,
                                         scene_palettes=scene_palettes,
                                         scene_params=scene_params)
        return cache[f]

    fn.cache = cache  # type: ignore[attr-defined]
    return fn

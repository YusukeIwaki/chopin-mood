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
sky with diffuse moonlight, irregular layered terrain with a winding
path and water shimmer, a grounded traveller figure (head, coat, arms,
legs), and a very sparse drifting particle/star field. There are no
captions, logos, QA markers, expanding rings or concentric halos in
any frame. The "procedural prototype, not finished anime" disclaimer
lives in provenance/storyboard metadata, never in the raster.

Determinism: ``draw_clean_frame(f, ...)`` is a pure function of
``(f, ctrl, seed, palettes, params)``. All layouts come from
``RandomState(seed)`` and per-frame grain reseeds from ``(seed, f)``,
so frames may be evaluated in any order with identical pixels.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

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
    lay["_w"], lay["_h"] = int(w), int(h)
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
    """Refined static traveller silhouette as a small RGBA layer.

    Drawn on a locally supersampled mask (4x, figure-sized only, so
    there is no full-frame supersampling cost) from sampled cubic
    Bezier outlines, then downscaled: edges are smooth and considered,
    not circle/polygon tokens. The pose is still and asymmetrical:
    bowed head/neck with a hair profile (no face, no expression),
    uneven coat shoulders and hem, one arm hanging, one raised to the
    lantern, tapered trousers and grounded boots (feet planted; the
    contact shadow is drawn by the caller). A restrained rim offset
    toward the moon gives subtle edge lighting. Local coordinates are
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

    # Hunched, asymmetrical coat: higher back (left) shoulder, lower
    # front shoulder, straighter sides, longer back hem than front.
    coat = _bezier_chain((-7.6, 13.6), [
        ((-8.6, 21.0), (-7.8, 30.0), (-5.2, 39.2)),
        ((-4.6, 41.2), (-3.2, 42.2), (-1.8, 42.5)),
        ((-0.2, 43.0), (1.0, 42.7), (2.4, 42.0)),
        ((3.9, 41.2), (4.8, 39.6), (5.0, 37.6)),
        ((6.2, 30.0), (6.3, 22.0), (5.7, 15.4)),
        ((4.0, 14.0), (1.4, 16.8), (-1.6, 14.2)),
        ((-4.0, 12.4), (-6.2, 15.4), (-7.6, 13.6))])
    # Bowed head and hair as one mass: crown thrown back, forehead
    # sloping steeply to a tucked chin at the front, hair falling at
    # the back over the neck almost to the shoulder. The outline is
    # long and leaning, never a plain circle; no face is drawn.
    head = _bezier_chain((2.8, 42.4), [
        ((5.8, 44.6), (6.2, 47.6), (4.8, 50.4)),
        ((3.2, 51.4), (0.4, 52.2), (-2.4, 51.4)),
        ((-5.0, 50.4), (-6.1, 47.6), (-5.8, 44.6)),
        ((-5.4, 42.6), (-4.6, 40.2), (-3.2, 39.2)),
        ((-1.6, 40.6), (0.2, 41.2), (1.4, 41.0)),
        ((2.2, 41.4), (2.6, 41.8), (2.8, 42.4))])
    leg_l = _bezier_chain((-5.0, 15.8), [
        ((-5.3, 10.0), (-5.0, 5.0), (-4.7, 2.4)),
        ((-3.9, 2.0), (-3.0, 2.0), (-2.5, 2.4)),
        ((-2.0, 6.0), (-1.8, 11.0), (-1.7, 15.4)),
        ((-3.2, 16.2), (-4.2, 16.1), (-5.0, 15.8))])
    leg_r = _bezier_chain((0.9, 15.4), [
        ((0.7, 10.0), (0.9, 5.0), (1.1, 2.4)),
        ((1.9, 2.0), (2.8, 2.0), (3.3, 2.4)),
        ((3.9, 6.0), (4.1, 11.0), (4.0, 15.0)),
        ((2.6, 15.8), (1.6, 15.7), (0.9, 15.4))])

    def boot(x0):
        return _bezier_chain((x0, 0.2), [
            ((x0 - 0.1, 1.6), (x0 + 0.4, 2.6), (x0 + 1.8, 2.8)),
            ((x0 + 3.2, 2.6), (x0 + 3.8, 1.4), (x0 + 4.1, 0.5)),
            ((x0 + 4.0, 0.1), (x0 + 2.1, 0.0), (x0, 0.2))])

    arm_l = _tapered_stroke((-5.2, 36.6),
                            [((-8.8, 30.5), (-10.0, 24.5), (-9.2, 19.6))],
                            3.3, 2.2)
    arm_r = _tapered_stroke((4.7, 37.0),
                            [((7.3, 33.2), (9.5, 29.8), (10.7, 27.4))],
                            3.3, 2.3)
    neck = [(-2.1, 42.7), (2.3, 42.5), (1.9, 39.7), (-1.7, 39.9)]
    scarf = _bezier_chain((-3.8, 42.9), [
        ((-2.0, 43.6), (1.0, 43.4), (3.6, 42.4)),
        ((3.2, 41.2), (2.6, 40.6), (1.8, 40.3)),
        ((-0.5, 39.9), (-2.6, 40.4), (-3.9, 41.2)),
        ((-4.1, 41.9), (-4.0, 42.4), (-3.8, 42.9))])
    tail = _tapered_stroke((-3.4, 41.3),
                           [((-5.6, 39.8), (-6.8, 37.8), (-6.2, 35.6))],
                           1.7, 0.9)
    fold_dark = _tapered_stroke((-2.2, 36.0),
                                [((-2.8, 29.0), (-2.4, 21.0), (-3.0, 15.5))],
                                0.55, 0.4)
    fold_light = _tapered_stroke((3.4, 35.0),
                                 [((3.0, 28.0), (3.6, 20.0), (3.0, 15.0))],
                                 0.45, 0.3)

    silhouette = [leg_l, leg_r, boot(-4.6), boot(1.4), coat, arm_l,
                  arm_r, head]
    for shape in silhouette:  # restrained moon-side edge light
        d.polygon(pts(shape, dx=0.6, dy_up=0.4), fill=rim_col + (120,))
    for shape, col in ((leg_l, leg_col), (leg_r, leg_col),
                       (boot(-4.6), leg_col), (boot(1.4), leg_col),
                       (coat, coat_col), (arm_l, coat_col),
                       (arm_r, coat_col)):
        d.polygon(pts(shape), fill=col + (255,))
    d.polygon(pts(fold_dark), fill=(0, 0, 0, 40))
    d.polygon(pts(fold_light), fill=tuple(pal["glow"]) + (14,))
    d.polygon(pts(neck), fill=neck_col + (255,))
    d.polygon(pts(tail), fill=scarf_col + (235,))
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

    # layered terrain: irregular ridges with slow parallax drift.
    # Distant layers dissolve into the haze (soft feathered edges,
    # reduced contrast); nearer layers keep firmer but still
    # antialiased edges. Slope and texture shading keep the slopes
    # from reading as flat colour bands.
    drift = f * 0.0016  # slow environment motion even if camx is still
    ypix = yy[:, None] * h
    haze_arr = np.array(_mix(pal["sky_bot"], pal["far"], 0.45), float)
    layers = []
    for li, (pts, base_f, amp_f, col, shift_k) in enumerate((
        (lay["ridge_far"], 0.60, 0.055, pal["far"], 1.0),
        (lay["ridge_mid"], 0.70, 0.065, _mix(pal["far"], pal["near"], 0.45), 1.8),
        (lay["ridge_near"], 0.80, 0.055, pal["near"], 2.8),
    )):
        ridge = _ridge_y(x_norm, pts, base_f * h + (camy - 0.5) * 0.03 * h,
                         amp_f * h * zoom, camx * shift_k + drift * shift_k)
        layers.append((ridge, np.array(col, float)))
        depth = np.clip((ypix - ridge[None, :])
                        / np.maximum(1.0, h - ridge[None, :]), 0, 1)
        atmo = (0.42, 0.22, 0.06)[li]  # far layers sit deeper in haze
        col_arr = np.array(col, float) * (1.0 - atmo) + haze_arr * atmo
        slope = np.gradient(ridge)
        k = np.ones(17) / 17.0  # smooth so shading cannot stripe
        slope = np.convolve(np.pad(slope, 8, mode="edge"), k,
                            mode="valid")[:len(slope)]
        shade = np.clip(1.0 - slope * 0.16, 0.96, 1.04)[None, :, None]
        layer_col = (col_arr[None, None, :] * (1 - 0.25 * depth[:, :, None])
                     + np.array((4, 5, 10), float)[None, None, :]
                     * (0.25 * depth[:, :, None]))
        layer_col = layer_col * shade + tex[:, :, None] * 7.0
        feather = (0.016, 0.010, 0.005)[li] * h
        alpha = np.clip((ypix - ridge[None, :] + feather * 0.5)
                        / max(1.0, feather), 0.0, 1.0)
        alpha = alpha * alpha * (3.0 - 2.0 * alpha)
        img = layer_col * alpha[:, :, None] + img * (1 - alpha[:, :, None])

    # water sheen in the lowest foreground: blend toward the sky colour
    water = np.clip((yy - 0.93) / 0.07, 0, 1)[:, None, None]
    img = img * (1 - 0.22 * water) + bot[None, None, :] * (0.22 * water)

    # traveller ground position on the near ridge
    tx = w * (0.38 + 0.03 * np.sin(0.0011 * f + scene_id * 1.7)) \
        + (camx - 0.5) * 6 * u
    near_ridge = layers[-1][0]
    tx_c = float(np.clip(tx, 0, w - 1))
    feet_y = float(np.interp(tx_c, xs, near_ridge)) + 3 * u
    fs = figure_scale * u
    # The figure is static (no bob, no walking loop): feet stay planted
    # at feet_y. The lantern hangs from the raised hand; this ONE
    # position drives both the accent bloom below and the lantern dot
    # drawn with the figure, so they can never drift apart.
    fy = feet_y
    lx, ly = tx_c + 11 * fs, fy - 27 * fs

    # terrain-following path: a naturally curving worn track from the
    # traveller's feet down the foreground slope. Drawn as a feathered,
    # texture-broken mask (soft integrated edges, shading that fades
    # with distance) -- never a hard translucent wedge/spotlight.
    if h - feet_y > 4:
        span = h - feet_y
        p0 = (tx_c, feet_y - 1 * u)
        p1 = (tx_c + 15 * u, feet_y + 0.30 * span)
        p2 = (tx_c - 20 * u, feet_y + 0.66 * span)
        p3 = (tx_c + 11 * u, h + 4 * u)
        centre = [_cubic(p0, p1, p2, p3, t / 28.0) for t in range(29)]
        left, right = [], []
        for i, (cxp, cyp) in enumerate(centre):
            t = i / 28.0
            half = (2.4 + 13.0 * t ** 1.3) * u \
                * (1.0 + 0.16 * np.sin(t * 9.0 + scene_id * 1.3))
            left.append((cxp - half, cyp))
            right.append((cxp + half, cyp))
        mask = Image.new("L", (w, h), 0)
        ImageDraw.Draw(mask).polygon(left + right[::-1], fill=255)
        mask = mask.filter(
            ImageFilter.GaussianBlur(radius=max(1.0, 1.7 * u)))
        pa = np.asarray(mask, dtype=np.float64) / 255.0
        fade = 1.0 - 0.45 * np.clip((ypix - feet_y)
                                    / max(1.0, span), 0, 1)
        pa = pa * fade * (0.70 + 0.60 * (tex.astype(np.float64) + 0.5))
        pa = np.clip(pa, 0.0, 1.0) * 0.38
        path_col = np.array(_mix(_mix(pal["near"], pal["glow"], 0.18),
                                 pal["moon"], 0.10), float)
        img = img * (1 - pa[:, :, None]) + path_col[None, None, :] \
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
            a = int(46 + 66 * tw)
            rr = r * u * 0.55
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(228, 233, 250, a))

    # terrain speckles (texture on the slopes, parallax-shifted)
    sp = lay["speckles"]
    spx = (sp[:, 0] * w + camx * 14 * u) % w
    for x, yn in zip(spx[::4], sp[::4, 1]):
        ridge_here = float(np.interp(x, xs, layers[1][0]))
        y = ridge_here + yn * (h - ridge_here) * 0.85
        if y < h - 2:
            d.ellipse([x - u, y - u * 0.6, x + u, y + u * 0.6],
                      fill=(210, 215, 235, 14))

    # (The worn path was integrated into the terrain above as a
    # feathered, texture-broken mask; no wedge is drawn here.)

    # water shimmer streaks in the foreground
    for i, (sx0, sy0) in enumerate(lay["streaks"]):
        y = h * (0.94 + 0.055 * sy0)
        x = ((sx0 + 0.0006 * f * (0.5 + sy0)) % 1.0) * w
        ln = (6 + 14 * sx0) * u
        d.line([x, y, x + ln, y], fill=pal["glow"] + (int(13 + 10 * light),),
               width=max(1, int(u)))

    # traveller: refined static silhouette (see _traveller_layer),
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
            rr = r * u
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(255, 240, 210, 62))

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

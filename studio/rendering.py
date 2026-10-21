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
* the per-frame control arrays built by DSP (``light``, ``zoom``,
  ``camx``/``camy``, ``particle``, ``accent_pulse``, ``scene_id``).

Descriptive storyboard fields such as ``motif`` and ``camera`` are text
metadata: they are *not* executable instructions and have no effect on
the pixels. Timings, scene boundaries and accents are deterministic
inputs; this module never moves them, adds cuts, or imposes a BPM grid.

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

    # layered terrain: irregular ridges with slow parallax drift
    drift = f * 0.0016  # slow environment motion even if camx is still
    layers = []
    for pts, base_f, amp_f, col, shift_k in (
        (lay["ridge_far"], 0.60, 0.055, pal["far"], 1.0),
        (lay["ridge_mid"], 0.70, 0.065, _mix(pal["far"], pal["near"], 0.45), 1.8),
        (lay["ridge_near"], 0.80, 0.055, pal["near"], 2.8),
    ):
        ridge = _ridge_y(x_norm, pts, base_f * h + (camy - 0.5) * 0.03 * h,
                         amp_f * h * zoom, camx * shift_k + drift * shift_k)
        layers.append((ridge, np.array(col, float)))
        below = (yy[:, None] * h) >= ridge[None, :]
        depth = np.clip(((yy[:, None] * h) - ridge[None, :])
                        / np.maximum(1.0, h - ridge[None, :]), 0, 1)
        col_arr = np.array(col, float)
        layer_col = (col_arr[None, None, :] * (1 - 0.25 * depth[:, :, None])
                     + np.array((4, 5, 10), float)[None, None, :]
                     * (0.25 * depth[:, :, None]))
        layer_col = layer_col + tex[:, :, None] * 9.0
        img = np.where(below[:, :, None], layer_col, img)

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

    # soft accent bloom at the lantern (diffuse, localised; not a ring,
    # not a whole-frame flash)
    if acc > 0.01:
        lx, ly = tx_c + 11 * fs, feet_y - 27 * fs
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
            rr = r * u
            d.ellipse([x - rr, y - rr, x + rr, y + rr],
                      fill=(228, 233, 250, a))

    # terrain speckles (texture on the slopes, parallax-shifted)
    sp = lay["speckles"]
    spx = (sp[:, 0] * w + camx * 14 * u) % w
    for x, yn in zip(spx[::3], sp[::3, 1]):
        ridge_here = float(np.interp(x, xs, layers[1][0]))
        y = ridge_here + yn * (h - ridge_here) * 0.85
        if y < h - 2:
            d.ellipse([x - u, y - u * 0.6, x + u, y + u * 0.6],
                      fill=(210, 215, 235, 14))

    # winding path from the traveller's feet to the bottom edge
    path_col = _mix(pal["near"], pal["glow"], 0.30)
    steps = 9
    left, right = [], []
    for i in range(steps + 1):
        t = i / steps
        y = feet_y + t * (h - feet_y)
        cxp = tx_c + np.sin(t * 2.4 + scene_id) * 9 * u * t + t * t * 14 * u
        half = (3.5 + 24 * t) * u
        left.append((cxp - half, y))
        right.append((cxp + half, y))
    d.polygon(left + right[::-1], fill=path_col + (64,))
    inner_l = [(x + (tx_c - x) * 0.45, y) for x, y in left]
    inner_r = [(x + (tx_c - x) * 0.45, y) for x, y in right]
    d.polygon(inner_l + inner_r[::-1],
              fill=_mix(path_col, pal["moon"], 0.2) + (36,))

    # water shimmer streaks in the foreground
    for i, (sx0, sy0) in enumerate(lay["streaks"]):
        y = h * (0.94 + 0.055 * sy0)
        x = ((sx0 + 0.0006 * f * (0.5 + sy0)) % 1.0) * w
        ln = (6 + 14 * sx0) * u
        d.line([x, y, x + ln, y], fill=pal["glow"] + (int(13 + 10 * light),),
               width=max(1, int(u)))

    # traveller: head, coat, arms, legs, grounded on the near ridge
    body = (10, 10, 17)
    bob = 0.7 * u * np.sin(2 * np.pi * f / 110.0)
    fy = feet_y + bob
    for side in (-1, 1):  # legs + boots
        hip_x = tx_c + side * 3.2 * fs
        foot_x = tx_c + side * 5.0 * fs
        d.polygon([(hip_x - 1.7 * fs, fy - 17 * fs),
                   (hip_x + 1.7 * fs, fy - 17 * fs),
                   (foot_x + 1.6 * fs, fy - 1.5 * fs),
                   (foot_x - 1.6 * fs, fy - 1.5 * fs)], fill=body + (255,))
        d.rectangle([foot_x - 2.4 * fs, fy - 1.8 * fs,
                     foot_x + 2.8 * fs, fy], fill=body + (255,))
    # coat: rim light offset toward the moon, then the coat itself
    coat = [(tx_c - 6.5 * fs, fy - 39 * fs), (tx_c + 6.5 * fs, fy - 39 * fs),
            (tx_c + 10.5 * fs, fy - 15 * fs), (tx_c + 4.0 * fs, fy - 14 * fs),
            (tx_c, fy - 18 * fs), (tx_c - 4.0 * fs, fy - 14 * fs),
            (tx_c - 10.5 * fs, fy - 15 * fs)]
    rim = [(x + 1.1 * fs, y) for x, y in coat]
    d.polygon(rim, fill=_mix(pal["glow"], body, 0.45) + (255,))
    d.polygon(coat, fill=body + (255,))
    # arms (one hangs, one carries the lantern)
    d.polygon([(tx_c - 6.2 * fs, fy - 37 * fs), (tx_c - 3.8 * fs, fy - 37 * fs),
               (tx_c - 6.8 * fs, fy - 20 * fs), (tx_c - 9.0 * fs, fy - 20 * fs)],
              fill=body + (255,))
    d.polygon([(tx_c + 3.8 * fs, fy - 37 * fs), (tx_c + 6.2 * fs, fy - 37 * fs),
               (tx_c + 11.5 * fs, fy - 27 * fs),
               (tx_c + 9.6 * fs, fy - 24.5 * fs)], fill=body + (255,))
    # scarf + head + hair cap
    scarf = _mix(pal["glow"], pal["near"], 0.35)
    d.polygon([(tx_c - 4.6 * fs, fy - 40.5 * fs),
               (tx_c + 4.6 * fs, fy - 40.5 * fs),
               (tx_c + 2.5 * fs, fy - 36.5 * fs),
               (tx_c - 2.5 * fs, fy - 36.5 * fs)], fill=scarf + (255,))
    d.ellipse([tx_c - 6.0 * fs, fy - 52 * fs, tx_c + 6.0 * fs, fy - 40 * fs],
              fill=(16, 14, 20, 255))
    d.pieslice([tx_c - 6.2 * fs, fy - 53.5 * fs, tx_c + 6.2 * fs, fy - 41 * fs],
               start=180, end=360, fill=(7, 7, 12, 255))
    # lantern dot (brightens with the accent pulse; its diffuse bloom
    # was added above, so this stays a point, never a ring/flash)
    lx, ly = tx_c + 11 * fs, fy - 27 * fs
    lr = (1.8 + 1.6 * acc) * fs
    d.ellipse([lx - lr, ly - lr, lx + lr, ly + lr],
              fill=pal["glow"] + (int(150 + 100 * acc),))

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

    # deterministic per-frame grain (order-independent, as in P0)
    grng = np.random.RandomState(
        (seed ^ (0x9E3779B9 + f * 0x85EBCA6B)) & 0xFFFFFFFF)
    grain = grng.randint(-2, 3, size=(h, w, 1)).astype(np.int16)
    arr = np.clip(np.asarray(canvas).astype(np.int16) + grain, 0, 255)
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

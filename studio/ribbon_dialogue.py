"""Original paper sculptures: two continuous gestures in an unresolved dialogue.

No assets, generated media, typography, or timing annotations are embedded.
The trusted DSP controls supply scene boundaries, slow energy and accents.
Geometry is analytic and independent of render order; the same mesh supplies
the artwork and encoded-media reference frames.
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RENDERER_ID = "ribbon-dialogue-v1"
DEFAULTS = {"dialogue_spread": 0.0, "dialogue_twist": 0.0, "dialogue_lift": 0.35}


def make_layout(seed, w, h):
    rng = np.random.RandomState(seed)
    # Still paper fibres; never independent flicker masquerading as animation.
    texture = rng.normal(0, .56, (h, w, 1)).astype(np.float32)
    x = (np.arange(w, dtype=np.float32)[None, :] + .5) / w
    y = (np.arange(h, dtype=np.float32)[:, None] + .5) / h
    return {"texture": texture, "x": x, "y": y}


def _state(f, ctrl, scene_params):
    from .rendering import scene_transition

    def at(frame):
        sid = int(ctrl["scene_id"][frame])
        p = (scene_params[sid % len(scene_params)] or {}) if scene_params else {}
        return {key: float(p.get(key, value)) for key, value in DEFAULTS.items()}

    trans = scene_transition(f, ctrl["scene_id"])
    if trans is None:
        return at(f)
    left, right, blend = trans
    a, b = at(left), at(right)
    return {key: a[key] + (b[key] - a[key]) * blend for key in DEFAULTS}


def _mesh(side, f, energy, state):
    """A tapered sheet, folded across and along a gently moving spatial curve."""
    s = np.linspace(0., 1., 121)
    v = np.linspace(-1., 1., 13)
    spread, twist, lift = (state[key] for key in DEFAULTS)
    # Ambient movement has no asserted beat or phrase semantics. Slow acoustic
    # energy changes breadth; scene state changes the relation of the sheets.
    breath = np.sin(f / 30. * .215 + side * .7)
    phase = side * (.30 + .20 * twist) + .17 * breath
    theta = s * (2.15 + .30 * twist) * np.pi + phase
    amplitude = .105 + .039 * energy + .045 * lift
    center = np.stack([
        side * (.108 + .056 * spread) + amplitude * np.sin(theta),
        (s - .5) * (1.15 + .16 * lift) + side * (.018 + .020 * breath),
        .105 * np.cos(theta) + side * .025,
    ], axis=-1)
    # Gentle diagonal lift gives the two voices different postures without
    # swapping their identity or making the last pose a victory emblem.
    angle = side * (.12 + .14 * lift + .045 * breath)
    old = center[:, :2].copy()
    center[:, 0] = np.cos(angle) * old[:, 0] - np.sin(angle) * old[:, 1]
    center[:, 1] = np.sin(angle) * old[:, 0] + np.cos(angle) * old[:, 1]
    tangent = np.gradient(center, axis=0)
    tangent /= np.linalg.norm(tangent, axis=1, keepdims=True)
    across = np.stack([tangent[:, 1], -tangent[:, 0], np.zeros_like(s)], axis=-1)
    across /= np.linalg.norm(across, axis=1, keepdims=True)
    normal = np.cross(tangent, across)
    roll = (s * (2.10 + .55 * twist) * np.pi + side * .8
            + .30 * breath + .20 * energy)
    direction = across * np.cos(roll)[:, None] + normal * np.sin(roll)[:, None]
    folded_normal = np.cross(tangent, direction)
    width = (.063 + .014 * lift) * np.sin(np.pi * s) ** .62
    width *= .88 + .12 * np.cos(theta - side)
    mesh = center[:, None, :] + direction[:, None, :] * (width[:, None] * v)[..., None]
    # A shallow concave cross section creates visible paper edges and a ridge.
    mesh += folded_normal[:, None, :] * (width[:, None] * .24 * v ** 2)[..., None]
    return mesh


def _project(mesh, w, h):
    # Fixed perspective and a portrait composition with breathing room.
    scale = 1. / (1.0 + mesh[..., 2] * .36)
    return np.stack([(mesh[..., 0] * scale + .5) * w,
                     (mesh[..., 1] * scale * .47 + .49) * h], axis=-1)


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R

    pal, params = R.visual_style_for_frame(f, ctrl, scene_palettes, scene_params)
    state = _state(f, ctrl, scene_params)
    paper = lay["ribbon_dialogue"]
    x, y = paper["x"], paper["y"]
    energy = float(np.mean(ctrl["light"][max(0, f - 23):f + 1]))
    light = float(np.clip(energy + params.get("light_bias", 0), 0, 1))
    accent = float(np.clip(ctrl["accent_pulse"][f], 0, 1))
    top, bottom = np.asarray(pal["sky_top"]), np.asarray(pal["sky_bot"])
    background = np.broadcast_to((1 - y[..., None]) * top + y[..., None] * bottom,
                                 (h, w, 3)).copy()
    vignette = ((x - .46) ** 2 + ((y - .44) * .80) ** 2)
    background += (1.7 * light - 12 * vignette)[..., None] + paper["texture"]
    canvas = Image.fromarray(np.clip(background, 0, 255).astype(np.uint8)).resize(
        (w * 2, h * 2), Image.Resampling.BILINEAR).convert("RGB")
    ww, hh = canvas.size
    meshes = [_mesh(side, f, energy, state) for side in (-1, 1)]
    points = [_project(mesh, ww, hh) for mesh in meshes]

    # The cast shadow grounds the gestures in a shared space. A paper-white
    # separation remains readable even when the two sheets cross.
    shadow = Image.new("RGBA", (ww, hh))
    sd = ImageDraw.Draw(shadow)
    for pts in points:
        outline = np.concatenate([pts[:, 0], pts[::-1, -1]])
        outline += np.array([ww * .025, hh * .023])
        sd.polygon([tuple(p) for p in outline], fill=(39, 28, 27, 30))
    shadow = shadow.filter(ImageFilter.GaussianBlur(ww * .018))
    canvas.paste(shadow, (0, 0), shadow)
    draw = ImageDraw.Draw(canvas)
    lamp = np.array([-.45, -.50, -.74])
    lamp /= np.linalg.norm(lamp)
    depths, polygons, colors = [], [], []
    for voice, (mesh, pts) in enumerate(zip(meshes, points)):
        base = np.asarray(pal["far"] if voice == 0 else pal["near"], dtype=float)
        lining = np.asarray(pal["moon"], dtype=float)
        verts = np.stack([mesh[:-1, :-1], mesh[1:, :-1], mesh[1:, 1:], mesh[:-1, 1:]], axis=2)
        polys = np.stack([pts[:-1, :-1], pts[1:, :-1], pts[1:, 1:], pts[:-1, 1:]], axis=2)
        norm = np.cross(verts[:, :, 1] - verts[:, :, 0], verts[:, :, 3] - verts[:, :, 0])
        norm /= np.maximum(np.linalg.norm(norm, axis=-1, keepdims=True), 1e-12)
        surface = np.where((norm[..., 2] < 0)[..., None], base, base * .45 + lining * .55)
        diffuse = .62 + .36 * np.abs(norm @ lamp)
        edge = np.abs((np.arange(mesh.shape[1] - 1) + .5) / (mesh.shape[1] - 1) * 2 - 1) ** 10
        color = surface * diffuse[..., None] + lining * (.035 + .055 * edge[None, :, None])
        # A local reflection at the retained DSP accents, on the sheet itself.
        location = (np.arange(mesh.shape[0] - 1) + .5) / (mesh.shape[0] - 1)
        glint = accent * np.exp(-((location - (.48 + .10 * voice)) / .12) ** 2)
        color += np.asarray(pal["glow"]) * (.48 * glint[:, None, None])
        depths.append(verts[..., 2].mean(axis=2).ravel())
        polygons.append(polys.reshape(-1, 4, 2))
        colors.append(np.clip(color, 0, 255).astype(np.uint8).reshape(-1, 3))
    order = np.argsort(-np.concatenate(depths), kind="stable")
    polygons, colors = np.concatenate(polygons), np.concatenate(colors)
    for index in order:
        draw.polygon([tuple(p) for p in polygons[index]], fill=tuple(colors[index]))
    canvas = canvas.resize((w, h), Image.Resampling.LANCZOS)
    return R.apply_camera(np.asarray(canvas), *R.camera_for_frame(f, ctrl, scene_params))

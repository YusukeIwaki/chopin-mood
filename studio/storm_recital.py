"""Three registered recital paintings with deterministic moving weather.

The source images depict an imagined recital, not documentary performance.
The renderer animates weather, light and camera, never the pianist's fingers.
Only trusted DSP controls determine scene/lighting-event timing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

RENDERER_ID = "storm-recital-v1"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual") / RENDERER_ID
DEFAULTS = {"storm_shot": 0, "rain_density": .7, "rain_wind": 0.,
            "storm_light": .5, "reflection_strength": .6}
BASE_PALETTE = {"sky_top": (12, 20, 32), "sky_bot": (65, 76, 88),
                "far": (40, 53, 68), "near": (24, 28, 35),
                "moon": (225, 222, 210), "glow": (240, 190, 116)}
SHOTS = ("wide.png", "medium.png", "overhead.png")
SHOT_DISSOLVE_HALF_FRAMES = 4


def make_layout(seed, w, h):
    directory = ROOT / DIRECTORY
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("bundle_id") != RENDERER_ID or set(manifest["files"]) != set(SHOTS):
        raise ValueError("unexpected storm recital visual asset files")
    images = []
    for name in SHOTS:
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]:
            raise ValueError(f"visual asset digest mismatch: {name}")
        with Image.open(path) as image:
            images.append(np.asarray(image.convert("RGB").resize(
                (w, h), Image.Resampling.LANCZOS), dtype=np.float32))
    rng = np.random.RandomState(seed)
    x = (np.arange(w, dtype=np.float32)[None, :] + .5) / w
    y = (np.arange(h, dtype=np.float32)[:, None] + .5) / h
    # Different spatial masks respect each photographed/painted viewpoint.
    sky = [np.broadcast_to(1 - np.clip((y-.40)/.18, 0, 1), (h, w)),
           np.broadcast_to(1 - np.clip((y-.23)/.21, 0, 1), (h, w)),
           np.zeros((h, w), dtype=np.float32)]
    ground = [np.broadcast_to(np.clip((y-.78)/.13, 0, 1), (h, w)),
              np.broadcast_to(np.clip((y-.90)/.10, 0, 1), (h, w)),
              np.clip((np.abs(x-.52)/.40 + np.abs(y-.57)/.43 - 1.15)*4, 0, 1)]
    # Conservatively exclude the complete overhead piano, player and bench,
    # including the upper-right body beyond the coarse diamond floor mask.
    occupied = (np.clip((x-.10)/.03, 0, 1)*np.clip((.99-x)/.03, 0, 1)
                * np.clip((y-.19)/.03, 0, 1)*np.clip((.70-y)/.03, 0, 1))
    ground[2] = ground[2]*(1-occupied)
    flashes = [np.exp(-((x-.68)/.30)**2-((y-.24)/.20)**2),
               np.exp(-((x-.70)/.30)**2-((y-.25)/.20)**2),
               np.exp(-((x-.75)/.23)**2-((y-.81)/.17)**2)]
    # Keep the low-resolution preview's subpixel weather from collapsing
    # into dense one-pixel noise. Production dimensions retain 1250 drops.
    area_scale = w*h/(540*960)
    return {"images": images, "x": x, "y": y, "sky": sky, "ground": ground,
            "flashes": flashes, "rain": rng.uniform(size=(max(1, round(1250*area_scale)), 6)),
            "ripples": rng.uniform(size=(max(1, round(95*area_scale)), 5)),
            "provenance": {"bundle_id": RENDERER_ID,
                "manifest_path": (DIRECTORY / "manifest.json").as_posix(),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "files": {(DIRECTORY / name).as_posix(): manifest["files"][name]
                          for name in SHOTS}}}


def _state(f, ctrl, scene_params):
    from .rendering import scene_transition, smoothstep01

    def at(frame):
        sid = int(ctrl["scene_id"][frame])
        params = (scene_params[sid % len(scene_params)] or {}) if scene_params else {}
        return {key: float(params.get(key, value)) for key, value in DEFAULTS.items()}

    trans = scene_transition(f, ctrl["scene_id"])
    if trans is None:
        state = at(f)
        return state, [(int(state["storm_shot"]), 1.)]
    left, right, blend = trans
    a, b = at(left), at(right)
    state = {key: a[key] + (b[key]-a[key])*blend for key in DEFAULTS}
    # A short shot dissolve keeps different piano scales from lingering as
    # double images. Its 50% point remains the original DSP boundary;
    # weather controls, palette and camera retain their slower easing.
    boundary = (left + right) // 2
    radius = min(SHOT_DISSOLVE_HALF_FRAMES, (right-left)//2)
    shot_blend = smoothstep01((f-boundary+radius)/(2*radius))
    # Blend actual shots; rounding an interpolated shot index would cut at
    # an arbitrary intermediate index, especially for a wide/overhead pair.
    return state, [(int(a["storm_shot"]), 1-shot_blend),
                   (int(b["storm_shot"]), shot_blend)]


def _lightning(canvas, f, ctrl, state, sky, seed):
    """One stable branched shape per retained DSP pulse; no new events."""
    accent = float(np.clip(ctrl["accent_pulse"][f], 0, 1))
    if accent < .08 or not np.any(sky):
        return canvas
    pulses = np.asarray(ctrl["accent_pulse"][:f+1])
    rises = np.flatnonzero(pulses > np.r_[0., pulses[:-1]] + 1e-8)
    onset = int(rises[-1]) if len(rises) else 0
    rng = np.random.RandomState((seed ^ (onset*2654435761)) & 0xffffffff)
    w, h = canvas.size
    start = rng.uniform(.48, .79)
    yy = np.linspace(-.02, .44, 14)
    xx = start + np.linspace(0, -.16, len(yy)) + rng.normal(0, .018, len(yy))
    paths = [[(float(x*w), float(y*h)) for x, y in zip(xx, yy)]]
    for at, side in ((4, -1), (7, 1), (9, -1)):
        by = yy[at] + np.arange(5)*.020
        bx = xx[at] + side*np.arange(5)*.016 + rng.normal(0, .009, 5)
        bx[0], by[0] = xx[at], yy[at]
        paths.append([(float(x*w), float(y*h)) for x, y in zip(bx, by)])
    # Subpixel filaments remain smooth in both small previews and delivery.
    scale = 2
    core = Image.new("L", (w*scale, h*scale))
    draw = ImageDraw.Draw(core)
    for i, path in enumerate(paths):
        draw.line([(x*scale, y*scale) for x, y in path],
                  fill=255 if i == 0 else 175,
                  width=max(1, round(w/540*scale*(2 if i == 0 else 1))))
    core = core.resize((w, h), Image.Resampling.LANCZOS)
    glow = core.filter(ImageFilter.GaussianBlur(max(.8, w*.009)))
    intensity = accent*(.35+.65*state["storm_light"])
    alpha = np.clip((np.asarray(core, dtype=np.float32)
                    + np.asarray(glow, dtype=np.float32)*3.2)*sky*intensity, 0, 255)
    bolt = Image.new("RGBA", (w, h), (219, 237, 255, 0))
    bolt.putalpha(Image.fromarray(alpha.astype(np.uint8)))
    return Image.alpha_composite(canvas.convert("RGBA"), bolt).convert("RGB")


def _weather(canvas, f, state, asset, ground, light):
    w, h = canvas.size
    t = f / 30.
    density, wind = state["rain_density"], state["rain_wind"]
    draw = ImageDraw.Draw(canvas, "RGBA")
    for px, py, depth, pace, threshold, variation in asset["rain"]:
        strength = float(np.clip((density-threshold)*8+.5, 0, 1))
        if strength <= 0:
            continue
        speed = .31 + .56*depth + .16*pace
        yy = ((py + t*speed) % 1.12) - .06
        # Bounded shear changes direction without the f*wind teleport that
        # would otherwise occur when wind changes late in a performance.
        xx = (px + t*speed*.10 + wind*(yy-.5)*.13) % 1.12 - .06
        length = h*(.007+.025*depth)*(.8+.4*variation)
        alpha = round(strength*(12+38*depth)*(0.78+.30*light))
        draw.line((xx*w, yy*h, xx*w+length*(.10+.30*wind), yy*h+length),
                  fill=(187, 205, 219, alpha), width=max(1, round(w/540*depth*1.6)))
    # Ground-contact rings occupy only visible water, never sky or piano.
    for px, py, phase, pace, threshold in asset["ripples"]:
        xx, yy = min(w-1, int(px*w)), min(h-1, int(py*h))
        mask = float(ground[yy, xx])
        if mask < .5 or threshold > density:
            continue
        age = (phase+t*(.55+.50*pace)) % 1
        radius = w*(.002+.015*age)
        alpha = round(34*mask*(1-age)**2*state["reflection_strength"])
        draw.ellipse((xx-radius, yy-radius*.24, xx+radius, yy+radius*.24),
                     outline=(223, 217, 193, alpha), width=1)
    return canvas


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R

    asset = lay["storm_recital"]
    state, shots = _state(f, ctrl, scene_params)
    pal, params = R.visual_style_for_frame(f, ctrl, scene_palettes, scene_params)
    blend = lambda key: sum(asset[key][index]*weight for index, weight in shots)
    img, sky, ground, flash = (blend(key) for key in ("images", "sky", "ground", "flashes"))
    x, y = asset["x"], asset["y"]
    energy = float(np.mean(ctrl["light"][max(0, f-23):f+1]))
    light = float(np.clip(energy + params.get("light_bias", 0), 0, 1))
    accent = float(np.clip(ctrl["accent_pulse"][f], 0, 1))
    delta = lambda key: np.asarray(pal[key], dtype=np.float32) - BASE_PALETTE[key]
    grade = (sky[..., None]*(delta("sky_top")*.55+delta("sky_bot")*.45)
             + (1-sky-ground)[..., None]*delta("far")
             + ground[..., None]*delta("near"))
    img = img*(.78+.30*state["storm_light"]+.08*light) + grade*.30
    # Slowly advected spray is atmospheric movement, not a claimed beat.
    mist = (np.sin(x*11+y*5-f*.0031)+np.sin(x*19-y*7-f*.0023))*.5
    img += (sky*(mist+1)*state["rain_density"]*2.5)[..., None]
    shimmer = (np.sin(x*39+y*113+f*.071)+np.sin(x*53-y*83-f*.053))*.5
    reflection = ground*(2.0+shimmer*1.6)*state["reflection_strength"]
    img += reflection[..., None]*np.asarray(pal["glow"], dtype=np.float32)/50
    # Lightning lights the clouds and wet stage at sparse original accents.
    # A bounded exposure lift reaches the player without whitening the frame.
    flash_color = np.array([190., 210., 235.]) + delta("moon")*.4
    img += flash[..., None]*flash_color*accent*.52
    img += flash_color*accent*state["storm_light"]*.075
    # Filmic highlight shoulder preserves warm piano/water detail through
    # strong reflected flashes instead of clipping colour channels flat.
    highlights = np.maximum(img-180., 0.)
    img = np.minimum(img, 180.) + 75.*(-np.expm1(-highlights/75.))
    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    canvas = _lightning(canvas, f, ctrl, state, sky, seed)
    canvas = _weather(canvas, f, state, asset, ground, light+accent*1.5)
    left, top, cw, ch = R.camera_crop_box(w, h, *R.camera_for_frame(f, ctrl, scene_params))
    if cw < w-1e-9 or ch < h-1e-9:
        # Bilinear sampling avoids ringing in the moving high-contrast wet
        # stone/strings. Crop bounds and DSP-driven trajectories are shared
        # with the other providers; their sampling remains unchanged.
        canvas = canvas.resize((w, h), Image.Resampling.BILINEAR,
                               box=(left, top, left+cw, top+ch))
    # Delivery uses 4:2:0 chroma. Subpixel luminance AA and a wider colour
    # filter stabilize fine wet-stone, string and rain detail in delivery.
    # The original registered paintings are never modified.
    y_channel, cb, cr = canvas.convert("YCbCr").split()
    y_channel = y_channel.filter(ImageFilter.GaussianBlur(.8))
    chroma_filter = ImageFilter.GaussianBlur(1.5)
    return np.asarray(Image.merge("YCbCr", (y_channel, cb.filter(chroma_filter),
                                           cr.filter(chroma_filter))).convert("RGB"))

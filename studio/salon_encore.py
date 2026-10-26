"""A small, sunlit salon: registered art with deterministic gentle motion.

The illustrated pianist is fictional. Local posture motion suggests playing;
it does not transcribe notes or claim to reproduce the owner's fingering.
Only existing native-audio DSP controls determine accents and scene timing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

from .config import SALON_ENCORE_RENDERER_ID

RENDERER_ID = SALON_ENCORE_RENDERER_ID
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual") / RENDERER_ID
FILES = {"background.png", "performance.png"}
BASE_PALETTE = {"sky_top": (224, 203, 167), "sky_bot": (246, 224, 182),
                "far": (174, 134, 108), "near": (123, 89, 61),
                "moon": (252, 238, 196), "glow": (240, 192, 116)}
SHOTS = ((1., 0., 0.), (1.25, -.32, .8), (1.50, -.48, .65))
SHOT_DISSOLVE_HALF_FRAMES = 4


def _shots(f, ctrl, scene_params):
    from .rendering import scene_transition, smoothstep01

    def at(frame):
        sid = int(ctrl["scene_id"][frame])
        params = (scene_params[sid % len(scene_params)] or {}) if scene_params else {}
        return int(params.get("salon_shot", 0))

    transition = scene_transition(f, ctrl["scene_id"])
    if transition is None:
        return [(at(f), 1.)]
    left, right, _ = transition
    boundary = (left+right)//2
    radius = min(SHOT_DISSOLVE_HALF_FRAMES, (right-left)//2)
    blend = smoothstep01((f-boundary+radius)/(2*radius))
    return [(at(left), 1-blend), (at(right), blend)]


def make_layout(seed, w, h):
    directory = ROOT / DIRECTORY
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("bundle_id") != RENDERER_ID or set(manifest["files"]) != FILES:
        raise ValueError("unexpected salon visual asset files")
    images = {}
    for name in sorted(FILES):
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != manifest["files"][name]:
            raise ValueError(f"visual asset digest mismatch: {name}")
        with Image.open(path) as image:
            images[name] = image.copy()
    sprite = images["performance.png"].convert("RGBA")
    box = sprite.getchannel("A").point(lambda a: 255 if a > 2 else 0).getbbox()
    if box is None:
        raise ValueError("salon performer has no visible pixels")
    x = (np.arange(w, dtype=np.float32)[None, :] + .5) / w
    y = (np.arange(h, dtype=np.float32)[:, None] + .5) / h
    return {"background": np.asarray(images["background.png"].convert("RGB").resize(
                (w, h), Image.Resampling.LANCZOS), dtype=np.float32),
            "sprite": sprite.crop(box), "x": x, "y": y,
            "phase": np.random.RandomState(seed).uniform(0, 2*np.pi),
            "provenance": {"bundle_id": RENDERER_ID,
                "manifest_path": (DIRECTORY / "manifest.json").as_posix(),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "files": {(DIRECTORY / name).as_posix(): manifest["files"][name]
                          for name in sorted(FILES)}}}


def _posture(sprite, f, energy, phase):
    """Subpixel upper-body sway; piano, bench and feet stay anchored.

    A spatially bounded mesh moves only the painted head, torso and wrists.
    This is a slow gesture, not a fabricated beat grid or note transcription.
    Random access returns the same pose without accumulated transforms.
    """
    w, h = sprite.size
    sway = w*.0035*np.sin(f*.014 + phase)*(.35+.65*energy)
    wrist = h*.0018*np.sin(f*.027 + phase*.7)*(.3+.7*energy)

    def point(x, y):
        u, v = x/w, y/h
        torso = np.exp(-((u-.23)/.115)**4-((v-.36)/.30)**4)
        hands = np.exp(-((u-.405)/.065)**4-((v-.48)/.09)**4)
        return x-sway*torso, y-wrist*hands

    xx, yy = np.linspace(0, w, 13).astype(int), np.linspace(0, h, 17).astype(int)
    mesh = []
    for top, bottom in zip(yy, yy[1:]):
        for left, right in zip(xx, xx[1:]):
            if right <= left or bottom <= top:
                continue
            quad = sum((point(x, y) for x, y in ((left, top), (left, bottom),
                        (right, bottom), (right, top))), ())
            mesh.append(((int(left), int(top), int(right), int(bottom)), quad))
    moved = sprite.transform(sprite.size, Image.Transform.MESH, mesh,
                             resample=Image.Resampling.BICUBIC)
    # Pillow's RGBA resampling also rounds premultiplied colours in areas
    # whose transform is identity. Composite only the bounded performer
    # region so the piano, lower legs and transparent edges stay exact.
    u = np.arange(w, dtype=np.float32)[None, :]/w
    v = np.arange(h, dtype=np.float32)[:, None]/h
    mask = np.clip((u-.06)/.06, 0, 1)*np.clip((.51-u)/.05, 0, 1)
    mask = mask*np.clip((v-.08)/.06, 0, 1)*np.clip((.72-v)/.08, 0, 1)
    return Image.composite(moved, sprite, Image.fromarray((mask*255).astype(np.uint8)))


def _shot_frame(frame, index):
    # These are trusted fixed compositions, not the user camera controls.
    # apply_camera intentionally caps those controls at zoom 1.18.
    zoom, pan_x, pan_y = SHOTS[index]
    if zoom == 1:
        return frame
    h, w = frame.shape[:2]
    cw, ch = w/zoom, h/zoom
    left, top = (w-cw)*.5*(1+pan_x), (h-ch)*.5*(1+pan_y)
    return np.asarray(Image.fromarray(frame).resize(
        (w, h), Image.Resampling.BICUBIC, box=(left, top, left+cw, top+ch)))


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R

    asset = lay["salon_encore"]
    pal, params = R.visual_style_for_frame(f, ctrl, scene_palettes, scene_params)
    energy = float(np.mean(ctrl["light"][max(0, f-23):f+1]))
    light = float(np.clip(energy+params.get("light_bias", 0), 0, 1))
    accent = float(np.clip(ctrl["accent_pulse"][f], 0, 1))
    x, y = asset["x"], asset["y"]
    floor = np.clip((y-.56)/.18, 0, 1)
    wall = 1-floor
    delta = lambda key: np.asarray(pal[key], dtype=np.float32)-BASE_PALETTE[key]
    grade = wall[..., None]*(delta("sky_top")*.5+delta("far")*.5)
    grade = grade + floor[..., None]*(delta("sky_bot")*.65+delta("near")*.35)
    img = asset["background"]*(.96+.08*light) + grade*.22
    # Slow leaves passing outside the window create restrained, bounded
    # daylight movement. No geometry is displaced on the piano or guests.
    sun = np.exp(-((x-.52)/.38)**2-((y-.64)/.26)**2)
    dapple = np.sin(x*17+y*9+f*.0031+asset["phase"])
    img += (sun*(1.4+1.0*dapple))[..., None]*np.array([1., .85, .50])
    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).convert("RGBA")

    source = asset["sprite"]
    sw = max(1, round(w*.91*params.get("figure_scale", 1)))
    sh = max(1, round(sw*source.height/source.width))
    sprite = source.resize((sw, sh), Image.Resampling.LANCZOS)
    sprite = _posture(sprite, f, light, asset["phase"])
    left, top = round(w*.50-sw*.50), round(h*.785-sh)
    shadow = Image.new("RGBA", (w, h))
    ImageDraw.Draw(shadow).ellipse((left+sw*.05, h*.775, left+sw*.96, h*.800),
                                   fill=(61, 39, 18, 62))
    canvas = Image.alpha_composite(canvas, shadow.filter(ImageFilter.GaussianBlur(max(1, w*.013))))
    canvas.alpha_composite(sprite, (left, top))
    img = np.asarray(canvas.convert("RGB"), dtype=np.float32)
    # Retained native-DSP accents gently warm the piano's key area and
    # nearby stage, instead of adding a cut or per-note particle burst.
    key_x, key_y = (left+sw*.43)/w, (top+sh*.49)/h
    warmth = np.exp(-((x-key_x)/.15)**2-((y-key_y)/.085)**2)
    img += warmth[..., None]*np.asarray(pal["glow"])*(.24*accent)*params.get("glow_scale", 1)
    frame = np.clip(img, 0, 255).astype(np.uint8)
    # Wide room, medium recital, and a closer player/keyboard composition
    # are crops of one coherent layered scene. Eight-frame dissolves stay
    # centered on native DSP boundaries; no extra cut times are introduced.
    views = [(_shot_frame(frame, index), weight)
             for index, weight in _shots(f, ctrl, scene_params) if weight > 0]
    framed = np.clip(sum(view.astype(np.float32)*weight for view, weight in views), 0, 255).astype(np.uint8)
    framed = R.apply_camera(framed, *R.camera_for_frame(f, ctrl, scene_params))
    # Delivery is 4:2:0. Suppress subpixel paper-grain and colour ringing
    # before encoding, keeping the original registered artwork unchanged.
    luminance, cb, cr = Image.fromarray(framed).convert("YCbCr").split()
    luminance = luminance.filter(ImageFilter.GaussianBlur(.45))
    colour_filter = ImageFilter.GaussianBlur(1.2)
    return np.asarray(Image.merge("YCbCr", (luminance, cb.filter(colour_filter),
                                           cr.filter(colour_filter))).convert("RGB"))

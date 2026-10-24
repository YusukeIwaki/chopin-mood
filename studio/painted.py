"""Versioned painted assets; deterministic animation, no runtime model calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

BUNDLE_ID = "moonlit-recollection-v1"
ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual") / BUNDLE_ID
BASE_PALETTE = {"sky_top": (14, 22, 38), "sky_bot": (82, 98, 116),
                "far": (58, 70, 88), "near": (16, 20, 28),
                "moon": (226, 228, 210), "glow": (148, 162, 176)}


def load_bundle(bundle_id, w, h):
    if bundle_id != BUNDLE_ID:
        raise ValueError("unknown visual asset bundle")
    directory = ROOT / DIRECTORY
    raw_manifest = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    if set(manifest["files"]) != {"background.png", "traveler.png"}:
        raise ValueError("unexpected visual asset files")
    images = {}
    for name, digest in manifest["files"].items():
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"visual asset digest mismatch: {name}")
        with Image.open(path) as im:
            images[name] = im.copy()
    figure = images["traveler.png"].convert("RGBA")
    # Ignore only alpha=1 specks when finding the sprite bounds. Preserve
    # the original alpha within the crop, including antialiased edges.
    bbox = figure.getchannel("A").point(lambda a: 255 if a > 1 else 0).getbbox()
    if bbox is None:
        raise ValueError("traveler asset has no visible pixels")
    return {"background": np.asarray(images["background.png"].convert("RGB").resize(
                (w, h), Image.Resampling.LANCZOS), dtype=np.float64),
            "figure": figure.crop(bbox),
            "provenance": {"bundle_id": bundle_id,
                "manifest_path": (DIRECTORY / "manifest.json").as_posix(),
                "manifest_sha256": hashlib.sha256(raw_manifest).hexdigest(),
                "files": {(DIRECTORY / k).as_posix(): v
                          for k, v in manifest["files"].items()}}}


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R

    asset = lay["painted_assets"]
    pal, params = R.visual_style_for_frame(f, ctrl, scene_palettes, scene_params)
    x = np.arange(w)[None, :] / w
    y = np.arange(h)[:, None] / h
    # The same painting persists through every scene. Spatial colour
    # grading preserves its detail; only DSP-derived transitions set time.
    sky = 1 - np.clip((y-.28)/.27, 0, 1)
    ground = np.clip((y-.66)/.24, 0, 1)
    middle = 1-sky-ground
    delta = lambda key: np.asarray(pal[key], float)-BASE_PALETTE[key]
    grade = (sky[:, :, None] * (.5*delta("sky_top")+.5*delta("sky_bot"))
             + middle[:, :, None]*delta("far")
             + ground[:, :, None]*delta("near"))
    light = float(np.clip(ctrl["light"][f]+params.get("light_bias", 0), 0, 1))
    img = asset["background"] * (.90+.14*light) + grade*.65
    glow = float(params.get("glow_scale", 1))
    moon = np.exp(-((x-.718)**2+((y-.17)*h/w)**2)/(.10**2*2))
    img += moon[:, :, None]*(delta("moon")*.45+(glow-.75)*7)

    # The fixed anchor belongs to the painted path. Camera transforms apply
    # to landscape, contact shadow and traveler together, so feet cannot slide.
    fx, fy = .51*w, .825*h
    figure_h = max(1, int(round(h*.155*params.get("figure_scale", 1)/.9)))
    source = asset["figure"]
    figure_w = max(1, int(round(figure_h*source.width/source.height)))
    sprite = source.resize((figure_w, figure_h), Image.Resampling.LANCZOS)
    acc = float(np.clip(ctrl["accent_pulse"][f], 0, 1))
    hx, hy = fx + figure_w*.20, fy - figure_h*.55
    hand = np.exp(-((x-hx/w)**2+((y-hy/h)*h/w)**2)/(.10**2*2))
    img += hand[:, :, None]*np.asarray(pal["glow"])*(.18*acc)
    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).convert("RGBA")
    shadow = Image.new("RGBA", (w, h))
    ImageDraw.Draw(shadow).ellipse((fx-figure_w*.50, fy-figure_h*.025,
                                   fx+figure_w*.42, fy+figure_h*.012),
                                  fill=(0, 0, 0, 105))
    canvas = Image.alpha_composite(canvas, shadow.filter(
        ImageFilter.GaussianBlur(max(.6, w*.002))))
    canvas.alpha_composite(sprite, (round(fx-figure_w*.5), round(fy-figure_h)))
    canvas = canvas.convert("RGB")
    d = ImageDraw.Draw(canvas, "RGBA")
    mult = float(params.get("particle_multiplier", 1))
    count = int(round(np.clip(ctrl["particle"][f]*mult, 0, 1)*R.N_PARTICLES))
    for (px, py), speed, radius in zip(lay["parts"][:count],
                                     lay["part_speed"][:count], lay["part_r"][:count]):
        px = (px*w+.10*f*speed*w/180) % w
        py = (py*h-.22*f*w/180) % h
        r = radius*w/180*.30
        d.ellipse((px-r, py-r, px+r, py+r), fill=(230, 225, 206, 24))
    camera = R.camera_for_frame(f, ctrl, scene_params)
    return R.apply_camera(np.asarray(canvas.convert("RGB")), *camera)

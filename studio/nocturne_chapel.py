"""Registered night-church art and a candle whose flame stays on its wick.

No runtime generation. Native DSP controls supply light and accents; scene
parameters express a creative arc, never a claim to detect a coda or emotion.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual/nocturne-chapel-v1")
RENDERERS = ("night-church-v1", "last-candle-v1")
BASE_PALETTE = {"sky_top": (12, 17, 29), "sky_bot": (35, 44, 66),
                "far": (30, 36, 48), "near": (15, 18, 25),
                "moon": (130, 156, 204), "glow": (236, 169, 79)}


def make_layout(seed, w, h, renderer_id):
    if renderer_id not in RENDERERS:
        raise ValueError("unknown chapel renderer")
    directory = ROOT / DIRECTORY
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest.get("bundle_id") != "nocturne-chapel-v1" or set(manifest["files"]) != {"church.png", "candle.png"}:
        raise ValueError("unexpected chapel asset files")
    for name, digest in manifest["files"].items():
        path = directory / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"visual asset digest mismatch: {name}")
    name = "candle.png" if renderer_id == "last-candle-v1" else "church.png"
    with Image.open(directory / name) as image:
        background = np.asarray(image.convert("RGB").resize((w, h), Image.Resampling.LANCZOS), dtype=np.float32)
    x = (np.arange(w, dtype=np.float32)[None, :]+.5)/w
    y = (np.arange(h, dtype=np.float32)[:, None]+.5)/h
    return {"background": background, "x": x, "y": y,
            "candle": renderer_id == "last-candle-v1", "wick": manifest["wick_anchor"],
            "phase": np.random.RandomState(seed).uniform(0, 2*np.pi),
            "provenance": {"bundle_id": "nocturne-chapel-v1",
                "manifest_path": (DIRECTORY / "manifest.json").as_posix(),
                "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                "files": {(DIRECTORY / k).as_posix(): v for k, v in manifest["files"].items()}}}


def _flame(canvas, x, y, height, lean, w):
    """Antialiased, asymmetric Bezier flame; base is always the wick tip."""
    from .rendering import _bezier_chain
    ss = 3
    # Supersample only the flame patch, not a multi-megapixel whole frame.
    radius = int(np.ceil(height+abs(lean)+4))
    left, top = int(x)-radius, int(y)-2*radius
    x, y = x-left, y-top
    layer = Image.new("RGBA", (2*radius*ss, (2*radius+4)*ss))
    d = ImageDraw.Draw(layer)
    def shape(scale, colour):
        hh, radius = height*scale, height*.20*scale
        tip = (x+lean*scale, y-hh)
        points = _bezier_chain((x, y+1), [
            ((x-radius*1.3,y-hh*.12),(x-radius,y-hh*.48),tip),
            ((x+radius*.7+lean,y-hh*.55),(x+radius*1.35,y-hh*.15),(x,y+1))], steps=16)
        d.polygon([(px*ss,py*ss) for px,py in points], fill=colour)
    shape(1, (255, 143, 40, 220))
    shape(.77, (255, 212, 111, 248))
    shape(.46, (255, 244, 197, 255))
    layer = layer.resize((2*radius,2*radius+4), Image.Resampling.LANCZOS)
    out = canvas.convert("RGBA")
    out.alpha_composite(layer,(left,top))
    return out.convert("RGB")


def _state(f, ctrl, scene_params):
    from .rendering import scene_transition
    defaults = {"chapel_warmth": 0., "candle_strength": .25}
    def at(frame):
        p = scene_params[int(ctrl["scene_id"][frame]) % len(scene_params)] if scene_params else {}
        return {k:float(p.get(k,v)) for k,v in defaults.items()}
    transition = scene_transition(f,ctrl["scene_id"])
    if transition is None:
        return at(f)
    left,right,blend = transition
    a,b = at(left),at(right)
    return {k:a[k]+(b[k]-a[k])*blend for k in defaults}


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R
    asset = lay["nocturne_chapel"]
    pal, params = R.visual_style_for_frame(f, ctrl, scene_palettes, scene_params)
    params.update(_state(f,ctrl,scene_params))
    energy = float(np.mean(ctrl["light"][max(0,f-29):f+1]))
    light = float(np.clip(energy+params.get("light_bias",0),0,1))
    accent = float(np.clip(ctrl["accent_pulse"][f],0,1))
    x,y,phase = asset["x"],asset["y"],asset["phase"]
    warmth = float(params.get("chapel_warmth",0))
    delta = np.asarray(pal["far"],dtype=np.float32)-BASE_PALETTE["far"]
    img = asset["background"]*(.88+.14*light) + delta*.18
    glow = float(params.get("glow_scale",1))
    if asset["candle"]:
        strength = float(params.get("candle_strength",.25))
        # Intrinsic continuous flame flicker is decorrelated from the notes.
        # DSP accents illuminate the surrounding stone, not per-note cuts.
        flutter = .80+.12*np.sin(f*.19+phase)+.08*np.sin(f*.317+phase*.7)
        height = h*(.012+.035*strength)*flutter
        wx,wy = asset["wick"]
        lean = w*.006*(1-strength*.65)*np.sin(f*.113+phase)
        halo = np.exp(-((x-wx)/(.055+.055*strength))**2-((y-wy+.012)/(.045+.04*strength))**2)
        spread = np.exp(-((x-wx)/.42)**2-((y-.62)/.42)**2)
        img += halo[...,None]*np.array([61,31,8])*(.5+strength)*glow
        img += spread[...,None]*np.array([49,29,7])*warmth
        img += spread[...,None]*np.asarray(pal["glow"])*(.12*accent)*glow
        canvas = Image.fromarray(np.clip(img,0,255).astype(np.uint8))
        canvas = _flame(canvas,wx*w,wy*h,height,lean,w)
    else:
        # Broad oblique light shafts and a floor reflection move within one
        # continuous nave. No expanding sound-wave rings or geometry warp.
        beam = np.exp(-((x-(.58+(y-.35)*.32+.012*np.sin(f*.004+phase)))/.15)**2)
        beam = beam*np.clip((y-.29)/.25,0,1)*np.clip((.97-y)/.22,0,1)
        floor = np.exp(-((x-.56)/.24)**2)*np.clip((y-.64)/.25,0,1)
        img += beam[...,None]*np.asarray(pal["moon"])*(.018+.035*light+.07*accent)*glow
        img += floor[...,None]*np.asarray(pal["moon"])*(.02+.09*accent)*glow
        img += beam[...,None]*np.array([32,19,5])*warmth
        canvas = Image.fromarray(np.clip(img,0,255).astype(np.uint8))
    framed = R.apply_camera(np.asarray(canvas),*R.camera_for_frame(f,ctrl,scene_params))
    # Match the existing delivery treatment for finely textured paintings.
    yy,cb,cr = Image.fromarray(framed).convert("YCbCr").split()
    return np.asarray(Image.merge("YCbCr",(yy.filter(ImageFilter.GaussianBlur(.45)),
        cb.filter(ImageFilter.GaussianBlur(1.2)),cr.filter(ImageFilter.GaussianBlur(1.2)))).convert("RGB"))

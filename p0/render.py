"""Deterministic 2D placeholder renderer + FFmpeg mux.

Imagery (all procedural, original): night window view over layered hills,
moon + glow, stars, drifting particles, a small traveler silhouette.
NOT a bar chart, NOT an evenly-spaced slideshow: camera/light/particles move
continuously from control arrays; palettes change only at DP scene cuts
(structured) or stay fixed (naive).

Determinism: draw(f) depends only on (f, controls, seed). Star/particle
layouts come from RandomState(seed). Per-frame grain uses
RandomState((seed, f)) reseeding, so frame order / chunking cannot change
pixels. No wall-clock, no dict-order dependence.

Diagnostic marker: an 8x8 square at bottom-right encodes
  R = (scene_id * 67) % 256, G = 220 if accent_pulse > 0.25 else 30,
  B = frame_index % 256.
It is tiny (not a headline feature) and exists so QA can verify that planned
events actually reached encoded frames, independently of plan.json.

Audio: the selected source samples [start,end) are cropped losslessly and
encoded to AAC with NO normalization, NO fade, NO time-stretch. The crop is
a derived artifact (output/ only); the original file is never modified.
"""
from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image, ImageDraw, ImageFont

PALETTES = [
    {"sky_top": (8, 12, 38), "sky_bot": (38, 52, 96), "far": (30, 44, 78),
     "near": (16, 26, 52), "moon": (235, 238, 245), "glow": (255, 214, 150)},
    {"sky_top": (16, 10, 34), "sky_bot": (64, 40, 88), "far": (48, 34, 74),
     "near": (26, 18, 46), "moon": (240, 230, 240), "glow": (255, 190, 170)},
    {"sky_top": (6, 20, 30), "sky_bot": (24, 68, 84), "far": (20, 56, 70),
     "near": (10, 32, 44), "moon": (232, 244, 244), "glow": (190, 255, 230)},
]

N_STARS = 140
N_PARTICLES = 120


def _layouts(seed: int, w: int, h: int) -> dict:
    rng = np.random.RandomState(seed)
    return {
        "stars": rng.rand(N_STARS, 2) * np.array([w, h * 0.62]),
        "star_r": 0.6 + rng.rand(N_STARS) * 1.4,
        "parts": rng.rand(N_PARTICLES, 2),
    }


def _coerce_palette(raw: dict) -> dict:
    out = {}
    for k in ("sky_top", "sky_bot", "far", "near", "moon", "glow"):
        v = raw[k]
        out[k] = tuple(int(max(0, min(255, int(c)))) for c in v)
    return out


def draw_frame(f: int, ctrl: dict, mode: str, w: int, h: int,
               seed: int, lay: dict, scene_palettes=None,
               scene_params=None) -> np.ndarray:
    """Render one RGB frame. Pure function of (f, ctrl, seed).

    ``scene_palettes`` (optional list of palette dicts with RGB triples) and
    ``scene_params`` (optional per-scene dicts: ``figure_scale``,
    ``particle_multiplier``, ``light_bias``, ``glow_scale``) let a validated
    declarative storyboard steer the procedural render. When omitted the
    legacy P0 behaviour is unchanged.
    """
    scene_id = int(ctrl["scene_id"][f])
    if scene_palettes:
        pal = _coerce_palette(scene_palettes[scene_id % len(scene_palettes)])
    else:
        pal = PALETTES[scene_id % len(PALETTES)] if mode == "structured" else PALETTES[0]
    spar = {}
    if scene_params:
        spar = scene_params[scene_id % len(scene_params)] or {}
    light = float(np.clip(float(ctrl["light"][f]) + float(spar.get("light_bias", 0.0)), 0.0, 1.0))
    zoom = float(ctrl["zoom"][f])
    camx = float(ctrl["camx"][f])
    camy = float(ctrl["camy"][f])
    dens = float(np.clip(float(ctrl["particle"][f]) * float(spar.get("particle_multiplier", 1.0)), 0.0, 1.0))
    acc = float(ctrl["accent_pulse"][f])
    glow_scale = float(spar.get("glow_scale", 1.0))
    figure_scale = float(spar.get("figure_scale", 1.0))

    top = np.array(pal["sky_top"], np.float64)
    bot = np.array(pal["sky_bot"], np.float64)
    yy = (np.linspace(0, 1, h) ** 1.15)[:, None, None]
    img = np.broadcast_to(
        (top[None, None, :] * (1 - yy) + bot[None, None, :] * yy), (h, w, 3)).copy()

    # window vignette (side mullions): subtle dark frame edges
    m = int(w * 0.045)
    img[:, :m] *= 0.55
    img[:, -m:] *= 0.55
    img[: int(h * 0.03)] *= 0.7

    canvas = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8), "RGB")
    d = ImageDraw.Draw(canvas, "RGBA")

    # stars (twinkle is a pure function of f)
    sx = (lay["stars"][:, 0] + camx * 8) % w
    sy = lay["stars"][:, 1]
    for x, y, r, k in zip(sx, sy, lay["star_r"], range(N_STARS)):
        tw = 0.35 + 0.65 * abs(np.sin(0.05 * f + k))
        a = int(90 + 120 * tw * (0.4 + 0.6 * light))
        d.ellipse([x - r, y - r, x + r, y + r], fill=(230, 235, 255, a))

    # moon + glow modulated by slow light
    mx, my = w * 0.74, h * 0.16
    gr = (60 + 50 * light) * glow_scale
    for rr, aa in ((gr, 26), (gr * 0.6, 40), (gr * 0.35, 60)):
        d.ellipse([mx - rr, my - rr, mx + rr, my + rr], fill=pal["glow"] + (aa,))
    d.ellipse([mx - 26, my - 26, mx + 26, my + 26], fill=pal["moon"] + (255,))

    # hill ridges with parallax from camx; zoom scales ridge amplitude
    xs = np.arange(w)
    for k, (key, base, amp, pha) in enumerate(
        (("far", 0.62, 26, 1.7), ("near", 0.76, 34, 0.4))
    ):
        c = pal[key]
        ridge = (base + (camy - 0.5) * 0.05) * h + amp * zoom * np.sin(
            xs / w * 6.28 * (1 + 0.3 * k) + pha + camx * (2 + k * 2))
        d.polygon([(0, h)] + [(float(x), float(y)) for x, y in zip(xs[::6], ridge[::6])] + [(w, h)],
                  fill=c + (255,))

    # small traveler silhouette on the near hill (drifts slowly with scene)
    tx = w * (0.30 + 0.08 * np.sin(0.002 * f + scene_id))
    ty = h * 0.76 + 34 * zoom * np.sin(tx / w * 6.28 * 1.3 + 0.4 + camx * 4) - 46
    fs = figure_scale
    d.ellipse([tx - 7 * fs, ty - 26 * fs, tx + 7 * fs, ty - 12 * fs], fill=(8, 8, 14, 255))
    d.polygon([(tx - 6 * fs, ty - 12 * fs), (tx + 6 * fs, ty - 12 * fs),
               (tx + 9 * fs, ty + 14 * fs), (tx - 9 * fs, ty + 14 * fs)],
              fill=(8, 8, 14, 255))

    # particles: count follows density; drift is f(seed, f)
    n = int(dens * N_PARTICLES)
    if n > 0:
        px = (lay["parts"][:n, 0] * w + 0.15 * f * (1 + lay["parts"][:n, 1])) % w
        py = (lay["parts"][:n, 1] * h - 0.35 * f) % h
        for x, y in zip(px, py):
            d.ellipse([x - 1.5, y - 1.5, x + 1.5, y + 1.5], fill=(255, 240, 210, 110))

    # restrained accent ripple (expanding ring, only while pulse is alive)
    if acc > 0.03:
        rr = 20 + (1 - acc) * 130
        d.ellipse([mx - rr, my - rr, mx + rr, my + rr],
                  outline=(255, 240, 210, int(150 * acc)), width=3)

    # warm light wash (hysteresis-limited light, never a strobe)
    wash = Image.new("RGBA", (w, h), pal["glow"] + (int(46 * light),))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), wash).convert("RGB")
    d = ImageDraw.Draw(canvas)

    # Unobtrusive validation caption: short ASCII only. The default PIL
    # bitmap font has no Japanese glyphs, so Japanese text renders as tofu
    # boxes (see quality review). No title / performer / AI-anime claims:
    # this is a procedural placeholder, not a finished animated work.
    n_scenes = int(np.max(ctrl["scene_id"]) + 1) if len(ctrl["scene_id"]) else 1
    d.text((10, 10), f"P0 {mode} placeholder {scene_id + 1}/{n_scenes}",
           fill=(235, 235, 240))
    d.text((10, h - 28), "validation audio only; procedural animation",
           fill=(220, 220, 228))

    # diagnostic marker 8x8 bottom-right
    R = (scene_id * 67) % 256
    G = 220 if acc > 0.25 else 30
    B = f % 256
    d.rectangle([w - 12, h - 12, w - 4, h - 4], fill=(R, G, B))

    # deterministic per-frame dither (order-independent)
    grng = np.random.RandomState((seed ^ (0x9E3779B9 + f * 0x85EBCA6B)) & 0xFFFFFFFF)
    grain = grng.randint(-2, 3, size=(h, w, 1)).astype(np.int16)
    arr = np.clip(np.asarray(canvas).astype(np.int16) + grain, 0, 255).astype(np.uint8)
    return arr


def render_video(frames_iter, audio_wav: str, out_mp4: str, w: int, h: int,
                 fps_num: int, fps_den: int, n_frames: int) -> dict:
    """Pipe raw RGB frames to ffmpeg, then mux the clip WAV (AAC).

    Two steps on purpose: a single pipe+mux pass lets ffmpeg truncate
    the tail of the audio stream when the video pipe hits EOF (observed
    58.24 s of audio for a 60 s clip on ffmpeg 5.1). Encoding video-only
    first and muxing afterwards keeps both streams at full duration.
    """
    import os

    fps_str = f"{fps_num}/{fps_den}" if fps_den != 1 else f"{fps_num}"
    video_only = out_mp4 + ".video-only.mp4"
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
        "-framerate", fps_str, "-i", "-",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-frames:v", str(n_frames), video_only,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    count = 0
    for arr in frames_iter:
        proc.stdin.write(np.ascontiguousarray(arr, dtype=np.uint8).tobytes())
        count += 1
    _, serr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg video encode failed: {serr.decode()[-2000:]}")
    try:
        mux = ["ffmpeg", "-y", "-v", "error", "-i", video_only, "-i", audio_wav,
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "256k", "-movflags", "+faststart", out_mp4]
        r = subprocess.run(mux, capture_output=True)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg mux failed: {r.stderr.decode()[-2000:]}")
    finally:
        if os.path.exists(video_only):
            os.unlink(video_only)
    return {"frames_written": count, "ffmpeg_tail": serr.decode()[-500:]}

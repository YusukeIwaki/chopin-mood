#!/usr/bin/env python3
"""Rebuild the registered native sky alpha from the unchanged source painting.

This is an offline asset-build recipe, never invoked by the video renderer.
Coordinates and method parameters are in mask-guide.json. Concave foreground
spires are resolved as 2D connected paint, rather than a downward-filled y(x).
"""
from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter
from scipy.ndimage import binary_fill_holes, binary_propagation, gaussian_filter, median_filter

BUNDLE = Path(__file__).resolve().parent
ROOT = BUNDLE.parents[2]


def build() -> Image.Image:
    guide = json.loads((BUNDLE / "mask-guide.json").read_text())
    source = ROOT / guide["source"]["path"]
    if hashlib.sha256(source.read_bytes()).hexdigest() != guide["source"]["sha256"]:
        raise ValueError("native source painting hash mismatch")
    image = Image.open(source).convert("RGB")
    if list(image.size) != guide["native_size"]:
        raise ValueError("native painting dimensions mismatch")
    pixels = np.asarray(image, float)
    height, width = pixels.shape[:2]
    red = gaussian_filter(pixels, (guide["edge_sigma"], guide["edge_sigma"], 0))[:, :, 0]
    edge = -np.gradient(red, axis=0)
    points = np.asarray(guide["guide_points"])
    approximate = np.interp(np.arange(width), points[:, 0], points[:, 1])
    ys = np.arange(*guide["search_y"])
    parents = np.zeros((width, len(ys)), np.int16)
    previous = None
    for x in range(width):
        score = -edge[ys, x] + guide["guide_cost"] * np.abs(ys - approximate[x])
        score[np.abs(ys - approximate[x]) > guide["search_radius"]] = 1e6
        if previous is not None:
            values = previous.copy()
            arg = np.arange(len(ys))
            for i in range(1, len(ys)):
                if values[i - 1] + guide["path_slope_cost"] < values[i]:
                    values[i] = values[i - 1] + guide["path_slope_cost"]
                    arg[i] = arg[i - 1]
            for i in range(len(ys) - 2, -1, -1):
                if values[i + 1] + guide["path_slope_cost"] < values[i]:
                    values[i] = values[i + 1] + guide["path_slope_cost"]
                    arg[i] = arg[i + 1]
            parents[x] = arg
            score += values
        previous = score
    ridge = np.zeros(width)
    index = int(np.argmin(previous))
    for x in range(width - 1, -1, -1):
        ridge[x] = ys[index]
        index = parents[x, index]
    ridge = median_filter(ridge, guide["horizontal_median_width"])
    sky = np.arange(height)[:, None] < ridge[None, :]
    luminance = pixels @ np.array([.2126, .7152, .0722])
    neutral = ((luminance < guide["neutral_luminance_max"]) &
               (pixels[:, :, 2] - pixels[:, :, 0] < guide["neutral_blue_minus_red_max"]))
    for x0, x1, y0, y1 in guide["concave_regions"]:
        allowed = neutral[y0:y1, x0:x1]
        seed = np.zeros_like(allowed)
        seed[-1, :] = allowed[-1, :]
        seed[:, -1] = allowed[:, -1]
        rock = binary_fill_holes(binary_propagation(seed, mask=allowed))
        sky[y0:y1, x0:x1] = ~rock
    return Image.fromarray((sky * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(guide["feather_sigma"]))


def verify_registered(rebuilt=None) -> None:
    """Protect registered bytes, then compare native pixels across codecs."""
    path = BUNDLE / "sky-mask.png"
    data = path.read_bytes()
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    if hashlib.sha256(data).hexdigest() != manifest["files"]["sky-mask.png"]:
        raise ValueError("registered sky mask hash mismatch")
    rebuilt = build() if rebuilt is None else rebuilt
    with Image.open(io.BytesIO(data)) as registered:
        if (registered.mode != "L" or registered.size != rebuilt.size or
                not np.array_equal(np.asarray(registered), np.asarray(rebuilt))):
            raise ValueError("registered sky mask pixels differ from deterministic rebuild")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        try:
            verify_registered()
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
    else:
        data = io.BytesIO()
        build().save(data, format="PNG")
        path = BUNDLE / "sky-mask.png"
        path.write_bytes(data.getvalue())

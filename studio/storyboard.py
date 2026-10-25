"""Declarative storyboard validation (Codex-authored data, never code).

Scenes must align exactly to deterministic DSP scene IDs. Palettes and
bounded render parameters are the only creative levers; paths, commands
and out-of-range values are rejected.
"""
from __future__ import annotations

import math

PALETTE_KEYS = ("sky_top", "sky_bot", "far", "near", "moon", "glow")
PARAM_RANGES = {
    "figure_scale": (0.5, 2.0),
    "particle_multiplier": (0.0, 2.0),
    "light_bias": (-0.3, 0.3),
    "glow_scale": (0.5, 2.0),
    # Optional executable camera (defaults: zoom 1.0, pan 0.0). The
    # camera transforms the whole frame by a deterministic crop/resample
    # eased with smoothstep over the scene's inclusive frame range; pan
    # is a fraction of the available crop margin (no-op at zoom 1.0).
    "camera_zoom_start": (1.0, 1.18),
    "camera_zoom_end": (1.0, 1.18),
    "camera_pan_x_start": (-1.0, 1.0),
    "camera_pan_x_end": (-1.0, 1.0),
    "camera_pan_y_start": (-1.0, 1.0),
    "camera_pan_y_end": (-1.0, 1.0),
    # Optional storm-recital controls; its provider joins the shot and
    # numeric controls without changing DSP or legacy visual defaults.
    "storm_shot": (0, 2),
    "rain_density": (0.2, 1.0),
    "rain_wind": (-1.0, 1.0),
    "storm_light": (0.0, 1.0),
    "reflection_strength": (0.0, 1.0),
}
_FORBIDDEN_KEYS = {"path", "file", "filepath", "command", "cmd", "script",
                   "code", "exec", "url", "href", "src"}
MAX_TEXT = 500


def _is_rgb(v) -> bool:
    return (isinstance(v, list) and len(v) == 3
            and all(isinstance(c, int) and not isinstance(c, bool)
                    and 0 <= c <= 255 for c in v))


def validate_storyboard(sb: dict, deterministic_scenes: list[dict]) -> dict:
    failures: list[str] = []
    if not isinstance(sb, dict):
        return {"ok": False, "failures": ["storyboard must be an object"]}
    if sb.get("schema_version") != "1.0":
        failures.append("schema_version must be 1.0")
    if "asset_bundle" in sb:
        # Project intake runs before scientific/image dependencies install.
        from .config import PAINTED_ASSET_BUNDLE_ID
        if sb["asset_bundle"] != PAINTED_ASSET_BUNDLE_ID:
            failures.append("unknown asset_bundle")
    if "renderer" in sb:
        from .config import STORM_RECITAL_RENDERER_ID
        if sb["renderer"] != STORM_RECITAL_RENDERER_ID:
            failures.append("unknown renderer")
        if "asset_bundle" in sb:
            failures.append("renderer and asset_bundle are mutually exclusive")
    for key in sb:
        if key.lower() in _FORBIDDEN_KEYS or "/" in str(key) or "\\" in str(key):
            failures.append(f"forbidden top-level key: {key}")
    scenes = sb.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return {"ok": False, "failures": failures + ["scenes must be non-empty"]}
    want_ids = [s["id"] for s in deterministic_scenes]
    got_ids = [s.get("id") for s in scenes if isinstance(s, dict)]
    if got_ids != want_ids:
        failures.append(f"scene ids must align exactly to {want_ids}, got {got_ids}")
    for sc in scenes:
        if not isinstance(sc, dict):
            failures.append("scene must be an object")
            continue
        sid = sc.get("id")
        for key in sc:
            if key.lower() in _FORBIDDEN_KEYS or "/" in str(key):
                failures.append(f"{sid}: forbidden key {key}")
        pal = sc.get("palette")
        if not isinstance(pal, dict) or set(pal) != set(PALETTE_KEYS):
            failures.append(f"{sid}: palette must have exactly {list(PALETTE_KEYS)}")
        else:
            for k, v in pal.items():
                if not _is_rgb(v):
                    failures.append(f"{sid}: palette.{k} must be RGB ints 0-255")
        params = sc.get("params", {})
        if not isinstance(params, dict):
            failures.append(f"{sid}: params must be an object")
        else:
            for k, v in params.items():
                if k not in PARAM_RANGES:
                    failures.append(f"{sid}: unknown param {k}")
                elif not isinstance(v, (int, float)) or isinstance(v, bool):
                    failures.append(f"{sid}: param {k} must be a number")
                elif not math.isfinite(float(v)):
                    failures.append(f"{sid}: param {k} must be finite")
                elif k == "storm_shot" and not float(v).is_integer():
                    failures.append(f"{sid}: param storm_shot must be an integer")
                else:
                    lo, hi = PARAM_RANGES[k]
                    if not (lo <= float(v) <= hi):
                        failures.append(f"{sid}: param {k}={v} out of [{lo}, {hi}]")
        for field in ("motif", "camera"):
            v = sc.get(field, "")
            if not isinstance(v, str) or len(v) > MAX_TEXT:
                failures.append(f"{sid}: {field} must be text <= {MAX_TEXT} chars")
            if isinstance(v, str) and ("://" in v or v.startswith("/")):
                failures.append(f"{sid}: {field} must not contain paths/URLs")
    return {"ok": not failures, "failures": failures}


def default_storyboard(deterministic_scenes: list[dict],
                       palettes: list[dict] | None = None) -> dict:
    """Deterministic fallback storyboard (offline smoke / tests)."""
    base = [
        {"sky_top": [8, 12, 38], "sky_bot": [38, 52, 96], "far": [30, 44, 78],
         "near": [16, 26, 52], "moon": [235, 238, 245], "glow": [255, 214, 150]},
        {"sky_top": [40, 16, 10], "sky_bot": [110, 60, 30], "far": [80, 44, 30],
         "near": [44, 24, 18], "moon": [250, 235, 220], "glow": [255, 180, 120]},
        {"sky_top": [6, 30, 34], "sky_bot": [30, 90, 96], "far": [24, 70, 76],
         "near": [12, 40, 48], "moon": [232, 244, 244], "glow": [180, 255, 220]},
    ]
    scenes = []
    for i, sc in enumerate(deterministic_scenes):
        pal = (palettes[i] if palettes and i < len(palettes)
               else base[i % len(base)])
        scenes.append({
            "id": sc["id"], "palette": pal,
            "motif": "procedural prototype motif (no finished anime claimed)",
            "camera": "slow parallax drift",
            "params": {"figure_scale": 1.0, "particle_multiplier": 1.0,
                       "light_bias": 0.0, "glow_scale": 1.0,
                       "camera_zoom_start": 1.0, "camera_zoom_end": 1.0,
                       "camera_pan_x_start": 0.0, "camera_pan_x_end": 0.0,
                       "camera_pan_y_start": 0.0, "camera_pan_y_end": 0.0},
        })
    return {"schema_version": "1.0", "style_id": "studio-procedural-v1",
            "prototype_notice": "procedural prototype, not finished anime",
            "scenes": scenes}

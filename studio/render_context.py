"""Shared rendering preparation for Studio.

Extracted from ``scripts/studio_render.py`` so the trusted CLI and
later visual-preflight code share one deterministic preparation
path. All DSP timing, plan/control construction and layout seeding
happen here exactly once; the renderer itself (``studio.rendering``)
remains the sole pixel source.

Invariants preserved from the original script:
  * features/events are computed at the native sample rate (no
    resampling, original WAV untouched, read-only);
  * ``clip``/``det_scenes``/``plan``/``controls`` are built with the
    same ``p0`` calls, arguments and order as before, so plans,
    controls and pixels are byte-identical;
  * ``plan["_asset_abspath"]`` stays internal (needed by file QA);
    :func:`public_plan` / ``RenderContext.public_plan`` strips every
    ``_``-prefixed key for serialization, so serialized objects never
    carry absolute source paths;
  * storyboard validation is the existing
    ``studio.storyboard.validate_storyboard`` against the
    deterministic scenes.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from p0 import clip as clipmod
from p0 import events as eventsmod
from p0 import features as featmod
from p0 import plan as planmod
from p0.timebase import Timebase
from studio import rendering as cleanmod
from studio import storyboard as sbmod

__all__ = ["RenderContext", "prepare_context", "public_plan"]


def _sha256_file(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def public_plan(plan: dict) -> dict:
    """Copy of ``plan`` without private (``_``-prefixed) keys."""
    return {k: v for k, v in plan.items() if not k.startswith("_")}


@dataclass
class RenderContext:
    """Deterministic preparation result shared by render/preflight."""

    candidate: dict
    brief: dict
    storyboard: dict
    sample_rate: int
    tb: Timebase
    clip: dict
    det_scenes: list[dict]
    plan: dict
    controls: dict
    layouts: dict
    palettes: list
    scene_params: list
    width: int
    height: int
    seed: int
    source_sha256: str

    @property
    def public_plan(self) -> dict:
        """Serialized plan: private keys (e.g. ``_asset_abspath``) removed."""
        return public_plan(self.plan)

    def frame(self, frame_index: int) -> np.ndarray:
        """One clean frame; exact options of the original script."""
        return cleanmod.draw_clean_frame(
            int(frame_index), self.controls, self.width, self.height,
            self.seed, self.layouts, scene_palettes=self.palettes,
            scene_params=self.scene_params)


def prepare_context(wav, candidate_path, storyboard_path, brief_path, *,
                    width: int = 540, height: int = 960,
                    seed: int = 20261019) -> RenderContext:
    """Load inputs, validate bindings, and run the DSP preparation.

    ``wav`` is the native decoded source WAV (read-only; never
    modified). ``candidate_path`` may hold the raw candidate or the
    brief-wrapped ``{"candidate": ...}`` form, as in the original
    script. Raises ``ValueError`` when the candidate does not equal
    ``brief.candidate``, when the native source hash disagrees with
    ``brief.source_hashes.decoded_wav_sha256`` (when present), or
    when storyboard validation fails.
    """
    wav_path = Path(wav)
    cand_raw = json.loads(Path(candidate_path).read_text())
    if isinstance(cand_raw, dict) and "candidate" in cand_raw:
        cand = cand_raw["candidate"]
    else:
        cand = cand_raw
    storyboard = json.loads(Path(storyboard_path).read_text())
    brief = json.loads(Path(brief_path).read_text())

    brief_candidate = brief.get("candidate") if isinstance(brief, dict) else None
    if brief_candidate != cand:
        raise ValueError("candidate does not match brief.candidate")

    source_sha256 = _sha256_file(wav_path)
    hashes = brief.get("source_hashes") if isinstance(brief, dict) else None
    want = hashes.get("decoded_wav_sha256") if isinstance(hashes, dict) else None
    if want is not None and want != source_sha256:
        raise ValueError(
            "source WAV sha256 does not match "
            "brief.source_hashes.decoded_wav_sha256")

    y, sr = sf.read(str(wav_path), dtype="float64", always_2d=True)
    sample_rate = int(sr)
    y_mono = y.mean(axis=1) if y.shape[1] > 1 else y.ravel()
    tb = Timebase(sample_rate, 30, 1)
    feat = featmod.compute_features(y_mono, sample_rate)
    events = eventsmod.detect_all(feat)
    clip = {"start_sample": int(cand["start_sample"]),
            "end_sample": int(cand["end_sample"]),
            "n_frames": int(cand["n_frames"]),
            "selection_reason": "DSP candidate selected by ID; no LLM timing",
            "edge_100ms_rms_dbfs": None, "edge_energy_note": "studio render"}
    det_scenes = cand.get("scenes") or clipmod.plan_scenes(
        clip, events, feat, tb)["scenes"]
    v = sbmod.validate_storyboard(storyboard, det_scenes)
    if not v["ok"]:
        exc = ValueError(
            "storyboard validation failed: " + "; ".join(v["failures"]))
        exc.failures = list(v["failures"])
        raise exc
    accents = planmod.pick_accents(events, clip, sample_rate)
    params = {"seed": seed, "width": width, "height": height,
              "analysis_hop": int(feat["meta"]["hop"]),
              "analysis_nfft": int(feat["meta"]["n_fft"]),
              "light_floor": planmod.LIGHT_FLOOR}
    plan = planmod.build_plan_json(clip, {"scenes": det_scenes,
                                          "reason": cand.get("scene_reason", "deterministic"),
                                          "candidate_cuts": []},
                                   accents, tb, seed, params, [])
    plan["_asset_abspath"] = str(wav_path.resolve())
    plan["storyboard_style"] = storyboard.get("style_id")
    palettes = [s["palette"] for s in storyboard["scenes"]]
    scene_params = [s.get("params", {}) for s in storyboard["scenes"]]
    controls = planmod.build_controls("structured", clip["n_frames"], tb, clip,
                                      det_scenes, accents, feat, seed)
    layouts = cleanmod.make_layouts(seed, width, height)
    if storyboard.get("renderer") is not None:
        from . import storm_recital
        layouts["storm_recital"] = storm_recital.make_layout(seed, width, height)
        # Public-plan binding invalidates old visual approval when the
        # trusted provider code changes, including uncommitted changes.
        plan["visual_assets"] = layouts["storm_recital"]["provenance"]
        plan["visual_renderer"] = {
            "renderer_id": storyboard["renderer"],
            "source_path": "studio/storm_recital.py",
            "sha256": _sha256_file(Path(storm_recital.__file__)),
        }
    if storyboard.get("asset_bundle") is not None:
        from .painted import load_bundle
        layouts["painted_assets"] = load_bundle(storyboard["asset_bundle"], width, height)
        # Included in public_plan_sha256, so preflight approval also binds
        # the exact asset files and manifest, even in local working trees.
        plan["visual_assets"] = layouts["painted_assets"]["provenance"]
    return RenderContext(
        candidate=cand, brief=brief, storyboard=storyboard,
        sample_rate=sample_rate, tb=tb, clip=clip, det_scenes=det_scenes,
        plan=plan, controls=controls, layouts=layouts, palettes=palettes,
        scene_params=scene_params, width=int(width), height=int(height),
        seed=int(seed), source_sha256=source_sha256)

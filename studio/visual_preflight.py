"""Deterministic visual evidence and approval core for Studio.

This module implements the still/motion *preflight* evidence layer
that sits between :func:`studio.render_context.prepare_context` and
the full renderer in ``scripts/studio_render.py``:

* :func:`generate_evidence` renders only bounded evidence (native
  still frames for every scene, or short silent motion windows
  centred on each scene midpoint) -- never a whole video;
* :func:`validate_evidence` strictly re-validates an evidence
  directory (schema, identity, binding, samples, exact media hashes,
  decoded-pixel correspondence) and fails closed on any tampering;
* :func:`validate_review` strictly validates a visual review bound
  to one evidence digest;
* :func:`approved_binding` / :func:`validate_approval_file` combine
  a passing still review and a passing motion review into one
  serialisable attestation that the full renderer can embed in its
  existing ``provenance.json``.

All pixels come from ``RenderContext.frame`` (the deterministic
clean renderer); all timing comes from the deterministic plan.
Decoded motion samples are compared with the reference renderer
using :func:`p0.qa._frame_error_metrics` and the original rendered
spotcheck thresholds (global MAE 8, patch MAE 16) -- never widened.

Evidence never carries raw source paths, absolute paths, logs or
credentials: the public plan only, canonical hashes of the inputs,
and media hashes of the generated files.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import subprocess
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image

from p0 import qa as _qa

__all__ = [
    "LISTENING_NOTE",
    "generate_evidence",
    "validate_evidence",
    "validate_review",
    "approved_binding",
    "validate_approval_file",
]

#: Exact listening note required in every visual review.
LISTENING_NOTE = "audio listening unavailable; not a full musical judgement"

EVIDENCE_VERSION = "1.0"
REVIEW_SCHEMA_VERSION = "1.0"
APPROVAL_SCHEMA_VERSION = "1.0"

#: Original rendered-spotcheck thresholds (p0.qa); do not widen.
GLOBAL_MAE_MAX = float(_qa.RENDERED_GLOBAL_MAE_MAX)
PATCH_MAE_MAX = float(_qa.RENDERED_PATCH_MAE_MAX)

MAX_MOTION_WINDOW_FRAMES = 60
MOTION_FPS = 30

# Bounds (fail closed above these; legitimate preflight evidence is
# far smaller: at most 60 preview frames per scene).
MAX_EVIDENCE_BYTES = 10 * 1024 * 1024
MAX_REVIEW_BYTES = 256 * 1024
MAX_MEDIA_FILE_BYTES = 64 * 1024 * 1024
MAX_MEDIA_TOTAL_BYTES = 256 * 1024 * 1024
MAX_SCENES = 64
MAX_SAMPLES = 3 * MAX_SCENES
MAX_PREVIEW_FRAMES = MAX_MOTION_WINDOW_FRAMES * MAX_SCENES

_EVIDENCE_KEYS = {
    "version", "stage", "identity", "binding", "plan", "samples",
    "motion_qa", "media_sha256",
}
_IDENTITY_KEYS = {
    "engine_sha", "run_id", "run_attempt", "iteration",
    "parent_issue", "child_issue", "candidate_id",
}
_BINDING_KEYS = {
    "source_sha256", "brief_sha256", "candidate_sha256",
    "storyboard_sha256", "public_plan_sha256", "controls_sha256",
    "width", "height", "seed",
}
_STILL_SAMPLE_KEYS = {"scene_id", "role", "source_frame", "file"}
_MOTION_SAMPLE_KEYS = _STILL_SAMPLE_KEYS | {"preview_frame"}
_REVIEW_KEYS = {
    "schema_version", "stage", "evidence_sha256", "verdict", "checks",
    "blockers", "summary", "repair", "listening_note",
}
_REVIEW_CHECK_KEYS = {
    "evidence_reviewed", "brief_adherence", "visual_quality",
    "motion_quality",
}
_REPAIR_KEYS = {"route", "instructions"}
_MOTION_QA_KEYS = {
    "pass", "thresholds", "frames", "windows", "disjoint_windows",
    "has_audio",
}
_APPROVAL_FILE_KEYS = {
    "schema_version", "still_dir", "motion_dir", "engine_sha",
    "run_id", "run_attempt",
}
_FRAME_NAME_RE = re.compile(r"^frame-\d{6,}\.png$")
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Repository root against which approval-file references resolve.
#: ``validate_approval_file`` resolves ``still_dir``/``motion_dir``
#: strictly inside this root (no approval-file-relative fallback).
#: Tests may monkeypatch this constant to a synthetic root.
REPO_ROOT = Path(__file__).resolve().parents[1]


def _is_int(v) -> bool:
    """True only for real ints (bool is never an int here)."""
    return isinstance(v, int) and not isinstance(v, bool)


def _is_number(v) -> bool:
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(float(v)))


def _canonical_bytes(obj) -> bytes:
    """Canonical JSON: sorted keys, tight separators, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _canonical_hash(obj) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _controls_hash(controls) -> str:
    """Deterministic hash over controls incl. dtype+shape+bytes."""
    h = hashlib.sha256()
    for key in sorted(controls):
        arr = np.ascontiguousarray(np.asarray(controls[key]))
        h.update(str(key).encode("utf-8") + b"\x00")
        h.update(str(arr.dtype).encode("utf-8") + b"\x00")
        h.update(json.dumps(list(arr.shape)).encode("utf-8") + b"\x00")
        h.update(arr.tobytes())
        h.update(b"\x00")
    return h.hexdigest()


def _strict_json_loads(data: bytes):
    """Parse JSON rejecting duplicate keys and NaN/Infinity tokens."""
    def _no_const(x):
        raise ValueError(f"non-finite JSON value: {x}")

    def _pairs(pairs):
        obj = {}
        for k, v in pairs:
            if k in obj:
                raise ValueError(f"duplicate JSON key: {k}")
            obj[k] = v
        return obj

    return json.loads(data.decode("utf-8"), parse_constant=_no_const,
                      object_pairs_hook=_pairs)


def _check_finite(obj, path: str, failures: list[str]) -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        failures.append(f"{path} must be finite")
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _check_finite(v, f"{path}.{k}", failures)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _check_finite(v, f"{path}[{i}]", failures)


def _identity_for_context(context, *, engine_sha, run_id, run_attempt,
                          iteration) -> dict:
    brief = context.brief if isinstance(context.brief, dict) else {}
    cand = context.candidate if isinstance(context.candidate, dict) else {}
    candidate_id = cand.get("id") or brief.get("candidate_id")
    return {
        "engine_sha": engine_sha,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "iteration": iteration,
        "parent_issue": brief.get("parent_issue"),
        "child_issue": brief.get("child_issue"),
        "candidate_id": candidate_id,
    }


def _binding_for_context(context) -> dict:
    return {
        "source_sha256": context.source_sha256,
        "brief_sha256": _canonical_hash(context.brief),
        "candidate_sha256": _canonical_hash(context.candidate),
        "storyboard_sha256": _canonical_hash(context.storyboard),
        "public_plan_sha256": _canonical_hash(context.public_plan),
        "controls_sha256": _controls_hash(context.controls),
        "width": int(context.width),
        "height": int(context.height),
        "seed": int(context.seed),
    }


def _validate_work_identity(*, engine_sha, run_id, run_attempt,
                            iteration) -> None:
    if not (isinstance(engine_sha, str) and _SHA40_RE.match(engine_sha)):
        raise ValueError("engine_sha must be a 40-hex sha")
    if not _is_int(run_id) or run_id < 1:
        raise ValueError("run_id must be a positive integer")
    if not _is_int(run_attempt) or run_attempt < 1:
        raise ValueError("run_attempt must be a positive integer")
    if not _is_int(iteration) or iteration < 0:
        raise ValueError("iteration must be a non-negative integer")


def _scenes_of(raw_scenes) -> list[dict]:
    scenes = []
    for sc in raw_scenes:
        scenes.append({
            "id": sc["id"],
            "start_frame": int(sc["start_frame"]),
            "end_frame": int(sc["end_frame"]),
        })
    return scenes


def _scene_triples(scenes: list[dict]):
    """(scene, [(role, source_frame)]) start/mid/end per scene."""
    out = []
    for sc in scenes:
        s0 = sc["start_frame"]
        e_incl = sc["end_frame"] - 1
        triples = [("start", s0), ("mid", (s0 + e_incl) // 2),
                   ("end", e_incl)]
        out.append((sc, triples))
    return out


def _motion_windows(scenes: list[dict]) -> list[dict]:
    """Up-to-60-frame windows centred on each scene midpoint."""
    windows: list[dict] = []
    preview = 0
    for sc in scenes:
        s0, e0 = sc["start_frame"], sc["end_frame"]
        count = min(MAX_MOTION_WINDOW_FRAMES, e0 - s0)
        mid_incl = (s0 + (e0 - 1)) // 2
        ws = mid_incl - count // 2
        ws = max(s0, min(ws, e0 - count))
        we = ws + count
        windows.append({
            "scene_id": sc["id"],
            "source_start_frame": ws,
            "source_end_frame": we,
            "preview_start_frame": preview,
            "preview_end_frame": preview + count,
            "frame_count": count,
        })
        preview += count
    return windows


def _thumb_size(width: int, height: int) -> tuple[int, int]:
    return (max(1, int(width) // 2), max(1, int(height) // 2))


def _contact_sheet(rows: list[list[np.ndarray]], width: int,
                   height: int) -> Image.Image:
    """3-column contact sheet: one row per scene, start/mid/end."""
    tw, th = _thumb_size(width, height)
    sheet = Image.new("RGB", (tw * 3, th * max(1, len(rows))))
    for r, row in enumerate(rows):
        for c, arr in enumerate(row[:3]):
            thumb = Image.fromarray(
                np.ascontiguousarray(arr, dtype=np.uint8)).resize(
                    (tw, th), Image.BICUBIC)
            sheet.paste(thumb, (c * tw, r * th))
    return sheet


def _save_contact_sheet(rows, width, height, path: Path) -> None:
    _contact_sheet(rows, width, height).save(
        str(path), format="JPEG", quality=88)


def _frame_png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(arr, dtype=np.uint8)).save(
        buf, format="PNG")
    return buf.getvalue()


def _probe_preview(path: Path) -> tuple[dict, list[str]]:
    """ffprobe facts for a motion preview (dims/fps/count/audio)."""
    failures: list[str] = []
    try:
        info = _qa.ffprobe(str(path))
    except Exception as e:  # noqa: BLE001 - probe failure is a failure
        return {}, [f"preview ffprobe failed: {e}"]
    streams = info.get("streams") or []
    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if len(videos) != 1:
        failures.append("preview must have exactly one video stream")
    if audios:
        failures.append("preview must have no audio stream")
    v = videos[0] if videos else {}
    try:
        fps = Fraction(str(v.get("avg_frame_rate", "0/1")))
    except Exception:  # noqa: BLE001
        fps = Fraction(0, 1)
    try:
        count = int(v.get("nb_read_frames", -1))
    except (TypeError, ValueError):
        count = -1
    facts = {
        "width": v.get("width"), "height": v.get("height"),
        "fps": fps, "frame_count": count,
        "has_audio": bool(audios), "codec": v.get("codec_name"),
    }
    return facts, failures


def _encode_preview(context, windows: list[dict], out_path: Path) -> int:
    """Encode concatenated silent windows (libx264/crf18/yuv420p)."""
    width, height = int(context.width), int(context.height)
    cmd = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}",
        "-framerate", str(MOTION_FPS), "-i", "-",
        "-an", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-r", str(MOTION_FPS), "-threads", "1", str(out_path),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    total = 0
    try:
        for win in windows:
            for src in range(win["source_start_frame"],
                             win["source_end_frame"]):
                arr = np.ascontiguousarray(context.frame(src),
                                           dtype=np.uint8)
                proc.stdin.write(arr.tobytes())
                total += 1
    except BrokenPipeError:
        pass
    _, serr = proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg preview encode failed: {serr.decode()[-1000:]}")
    return total


def generate_evidence(context, out_dir, *, stage, engine_sha, run_id,
                      run_attempt, iteration) -> dict:
    """Generate deterministic still or motion evidence for one context.

    ``stage`` is ``"still"`` or ``"motion"``. ``out_dir`` must be a
    fresh directory (missing or empty); an existing non-empty
    directory is never overwritten and raises ``ValueError``.

    * still: native-resolution PNGs (``frame-NNNNNN.png``, indexed by
      the original source frame local to the clip) for the start,
      middle and end of *every* deterministic scene, plus a
      3-column ``contact-sheet.jpg`` (one row per scene).
    * motion: up to 60 continuous frames centred on each scene
      midpoint (bounded within the scene), concatenated as disjoint
      windows into a silent ``preview.mp4`` (ffmpeg libx264, CRF 18,
      yuv420p, 30 fps, no audio). Decoded start/mid/end frames of
      each window are saved as native PNGs plus a contact sheet.
      Decoded samples are compared against ``context.frame`` with
      the original global-8/patch-16 MAE thresholds; any exceedance
      raises ``ValueError`` (fail closed, no evidence emitted).

    Writes ``evidence.json`` as exact canonical JSON bytes (sorted
    keys, tight separators, ``allow_nan=False``); its SHA-256 over
    the exact file bytes is the evidence digest. No whole-video
    render is performed. Returns the evidence dict.
    """
    if stage not in ("still", "motion"):
        raise ValueError("stage must be 'still' or 'motion'")
    _validate_work_identity(engine_sha=engine_sha, run_id=run_id,
                            run_attempt=run_attempt, iteration=iteration)
    out = Path(out_dir)
    if out.is_symlink():
        raise ValueError("out_dir must not be a symlink")
    if out.exists():
        if not out.is_dir():
            raise ValueError("out_dir exists and is not a directory")
        if any(out.iterdir()):
            raise ValueError("out_dir must be fresh (non-empty directory "
                             "is never overwritten)")
    else:
        out.mkdir(parents=True)

    scenes = _scenes_of(context.det_scenes)
    if not scenes:
        raise ValueError("context has no scenes")
    if len(scenes) > MAX_SCENES:
        raise ValueError("too many scenes for preflight evidence")
    n_frames = int(context.clip["n_frames"])
    for sc in scenes:
        if not (0 <= sc["start_frame"] < sc["end_frame"] <= n_frames):
            raise ValueError(f"scene out of bounds: {sc['id']}")

    identity = _identity_for_context(
        context, engine_sha=engine_sha, run_id=run_id,
        run_attempt=run_attempt, iteration=iteration)
    if not isinstance(identity["candidate_id"], str) \
            or not identity["candidate_id"]:
        raise ValueError("candidate_id must be a non-empty string")
    binding = _binding_for_context(context)
    # Public plan normalised through canonical JSON (tuples -> lists).
    plan = json.loads(_canonical_bytes(context.public_plan))

    samples: list[dict] = []
    motion_qa = None
    written: set[str] = set()

    def _write_frame(name: str, arr: np.ndarray) -> None:
        if name not in written:
            (out / name).write_bytes(_frame_png_bytes(arr))
            written.add(name)

    if stage == "still":
        rows: list[list[np.ndarray]] = []
        for sc, triples in _scene_triples(scenes):
            row: list[np.ndarray] = []
            for role, src in triples:
                arr = context.frame(src)
                name = f"frame-{src:06d}.png"
                _write_frame(name, arr)
                samples.append({"scene_id": sc["id"], "role": role,
                                "source_frame": int(src), "file": name})
                row.append(arr)
            rows.append(row)
        _save_contact_sheet(rows, context.width, context.height,
                            out / "contact-sheet.jpg")
    else:
        windows = _motion_windows(scenes)
        preview_path = out / "preview.mp4"
        total = _encode_preview(context, windows, preview_path)
        facts, probe_failures = _probe_preview(preview_path)
        if probe_failures:
            raise ValueError("; ".join(probe_failures))
        if facts["width"] != int(context.width) \
                or facts["height"] != int(context.height):
            raise ValueError("preview dimensions do not match context")
        if facts["fps"] != Fraction(MOTION_FPS, 1):
            raise ValueError("preview fps must be 30")
        if facts["frame_count"] != total:
            raise ValueError(
                f"preview frame count {facts['frame_count']} != {total}")
        if facts["has_audio"]:
            raise ValueError("preview must have no audio")
        qa_frames: list[dict] = []
        all_ok = True
        rows = []
        for win in windows:
            ws, we = win["source_start_frame"], win["source_end_frame"]
            ps = win["preview_start_frame"]
            row = []
            for role, src in (("start", ws),
                              ("mid", (ws + we - 1) // 2),
                              ("end", we - 1)):
                pframe = ps + (src - ws)
                decoded = _qa.decode_frame_rgb(
                    str(preview_path), pframe, int(context.width),
                    int(context.height))
                name = f"frame-{src:06d}.png"
                _write_frame(name, decoded)
                samples.append({
                    "scene_id": win["scene_id"], "role": role,
                    "source_frame": int(src), "preview_frame": int(pframe),
                    "file": name})
                metrics = _qa._frame_error_metrics(
                    decoded, np.asarray(context.frame(src)))
                ok = (metrics["global_mae"] <= GLOBAL_MAE_MAX
                      and metrics["patch_mae_max"] <= PATCH_MAE_MAX)
                all_ok = all_ok and ok
                qa_frames.append({
                    "scene_id": win["scene_id"], "role": role,
                    "source_frame": int(src),
                    "preview_frame": int(pframe), "file": name,
                    **metrics, "pass": bool(ok)})
                row.append(decoded)
            rows.append(row)
        motion_qa = {
            "pass": bool(all_ok),
            "thresholds": {"global_mae_max": GLOBAL_MAE_MAX,
                           "patch_mae_max": PATCH_MAE_MAX},
            "frames": qa_frames,
            "windows": windows,
            "disjoint_windows": True,
            "has_audio": False,
        }
        if not all_ok:
            raise ValueError(
                "motion preview decoded samples exceed QA thresholds")
        _save_contact_sheet(rows, context.width, context.height,
                            out / "contact-sheet.jpg")

    media_names = sorted(
        p.name for p in out.iterdir()
        if p.is_file() and p.name != "evidence.json")
    media_sha256 = {name: _sha256_file(out / name)
                    for name in media_names}
    evidence = {
        "version": EVIDENCE_VERSION,
        "stage": stage,
        "identity": identity,
        "binding": binding,
        "plan": plan,
        "samples": samples,
        "motion_qa": motion_qa,
        "media_sha256": media_sha256,
    }
    (out / "evidence.json").write_bytes(_canonical_bytes(evidence))
    return evidence


def _expected_samples(scenes: list[dict], stage: str):
    """Expected (samples, windows) derived deterministically."""
    if stage == "still":
        samples = []
        for sc, triples in _scene_triples(scenes):
            for role, src in triples:
                samples.append({"scene_id": sc["id"], "role": role,
                                "source_frame": int(src),
                                "file": f"frame-{src:06d}.png"})
        return samples, None
    windows = _motion_windows(scenes)
    samples = []
    for win in windows:
        ws, we, ps = (win["source_start_frame"], win["source_end_frame"],
                      win["preview_start_frame"])
        for role, src in (("start", ws), ("mid", (ws + we - 1) // 2),
                          ("end", we - 1)):
            samples.append({
                "scene_id": win["scene_id"], "role": role,
                "source_frame": int(src),
                "preview_frame": int(ps + (src - ws)),
                "file": f"frame-{src:06d}.png"})
    return samples, windows


def validate_evidence(out_dir, context=None, expected_engine_sha=None,
                      expected_run_id=None, expected_run_attempt=None) -> dict:
    """Strictly validate an evidence directory. Never raises on bad input.

    Checks: fresh whitelist of files (``evidence.json``,
    ``contact-sheet.jpg``, native ``frame-NNNNNN.png`` files, optional
    ``preview.mp4`` for motion, optional ``review.json``); no extra
    files/directories/symlinks/traversal; bounded file/media sizes and
    frame counts; exact canonical ``evidence.json`` bytes; strict
    schema/types (bool is never an int, non-finite rejected);
    identity vs any expected engine/run values; samples exactly the
    deterministic start/mid/end (still) or window mapping (motion);
    ``media_sha256`` exactly covering and matching every media file;
    native PNG dimensions; contact-sheet bytes rebuilt from the
    native frames; ffprobe dimensions/fps/count/no-audio for motion.

    When ``context`` is given, the input binding and public plan are
    compared against the live context and representative pixels are
    re-checked: still PNG arrays must equal ``context.frame``
    exactly; motion decoded samples must meet the same
    global-8/patch-16 thresholds against ``context.frame``.

    Returns ``{"ok", "failures", "evidence", "evidence_sha256"}``
    where ``evidence_sha256`` is the SHA-256 of the exact
    ``evidence.json`` bytes (or ``None`` when the file is absent).
    """
    failures: list[str] = []
    evidence = None
    evidence_sha256 = None
    base = Path(out_dir)

    def _done():
        return {"ok": not failures, "failures": failures,
                "evidence": evidence, "evidence_sha256": evidence_sha256}

    if base.is_symlink() or not base.is_dir():
        return {"ok": False, "failures": ["evidence directory missing"],
                "evidence": None, "evidence_sha256": None}

    # --- file inventory: whitelist, symlinks, dirs, traversal, size ---
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError as e:
        return {"ok": False, "failures": [f"cannot list evidence dir: {e}"],
                "evidence": None, "evidence_sha256": None}
    names: list[str] = []
    total_media = 0
    for entry in entries:
        name = entry.name
        names.append(name)
        if entry.is_symlink():
            failures.append(f"symlink in evidence directory: {name}")
            continue
        if entry.is_dir():
            failures.append(f"extra directory in evidence: {name}")
            continue
        if not entry.is_file():
            failures.append(f"non-file in evidence: {name}")
            continue
        if name in ("..", ".") or "/" in name or "\\" in name:
            failures.append(f"path traversal in evidence: {name}")
        allowed = name in ("evidence.json", "contact-sheet.jpg",
                           "preview.mp4", "review.json") \
            or bool(_FRAME_NAME_RE.match(name))
        if not allowed:
            failures.append(f"extra file in evidence: {name}")
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            size = 0
        if name == "evidence.json" and size > MAX_EVIDENCE_BYTES:
            failures.append("evidence.json exceeds size bound")
        if name == "review.json" and size > MAX_REVIEW_BYTES:
            failures.append("review.json exceeds size bound")
        if name not in ("evidence.json",):
            total_media += size
            if size > MAX_MEDIA_FILE_BYTES:
                failures.append(f"media file exceeds size bound: {name}")
    if total_media > MAX_MEDIA_TOTAL_BYTES:
        failures.append("evidence media exceeds total size bound")
    name_set = set(names)
    if "evidence.json" not in name_set:
        failures.append("missing evidence.json")
        return _done()
    if "contact-sheet.jpg" not in name_set:
        failures.append("missing contact-sheet.jpg")
    if failures:
        # Inventory is a trust boundary: never read through a rejected
        # symlink or decode a file from an invalid directory.
        return _done()

    # --- evidence.json: exact bytes, canonical form, strict schema ---
    try:
        raw = (base / "evidence.json").read_bytes()
    except OSError as e:
        failures.append(f"cannot read evidence.json: {e}")
        return _done()
    evidence_sha256 = _sha256_bytes(raw)
    if len(raw) > MAX_EVIDENCE_BYTES:
        failures.append("evidence.json exceeds size bound")
    try:
        parsed = _strict_json_loads(raw)
    except Exception as e:  # noqa: BLE001 - malformed JSON is a failure
        failures.append(f"evidence.json is not valid strict JSON: {e}")
        return _done()
    if not isinstance(parsed, dict):
        failures.append("evidence must be an object")
        return _done()
    evidence = parsed
    try:
        if _canonical_bytes(parsed) != raw:
            failures.append("evidence.json is not canonical JSON")
    except Exception as e:  # noqa: BLE001
        failures.append(f"evidence is not canonical-serialisable: {e}")
    _check_finite(parsed, "evidence", failures)
    if set(parsed) != _EVIDENCE_KEYS:
        failures.append(
            f"evidence keys {sorted(parsed)} != {sorted(_EVIDENCE_KEYS)}")
    if parsed.get("version") != EVIDENCE_VERSION:
        failures.append("evidence version must be '1.0'")
    stage = parsed.get("stage")
    if stage not in ("still", "motion"):
        failures.append("evidence stage must be still/motion")

    identity = parsed.get("identity")
    if not isinstance(identity, dict) or set(identity or {}) != _IDENTITY_KEYS:
        failures.append("identity must have exactly the required keys")
        identity = identity if isinstance(identity, dict) else {}
    else:
        if not (isinstance(identity.get("engine_sha"), str)
                and _SHA40_RE.match(identity["engine_sha"] or "")):
            failures.append("identity engine_sha must be a 40-hex sha")
        for field in ("run_id", "run_attempt"):
            if not _is_int(identity.get(field)) or identity[field] < 1:
                failures.append(f"identity {field} must be a positive int")
        if not _is_int(identity.get("iteration")) \
                or identity.get("iteration", -1) < 0:
            failures.append("identity iteration must be a non-negative int")
        for field in ("parent_issue", "child_issue"):
            v = identity.get(field)
            if v is not None and (not _is_int(v) or v < 1):
                failures.append(f"identity {field} must be a positive int")
        if not isinstance(identity.get("candidate_id"), str) \
                or not identity.get("candidate_id"):
            failures.append("identity candidate_id must be non-empty text")
    if expected_engine_sha is not None \
            and identity.get("engine_sha") != expected_engine_sha:
        failures.append("identity engine_sha does not match expected")
    if expected_run_id is not None \
            and identity.get("run_id") != expected_run_id:
        failures.append("identity run_id does not match expected")
    if expected_run_attempt is not None \
            and identity.get("run_attempt") != expected_run_attempt:
        failures.append("identity run_attempt does not match expected")

    binding = parsed.get("binding")
    if not isinstance(binding, dict) or set(binding or {}) != _BINDING_KEYS:
        failures.append("binding must have exactly the required keys")
        binding = binding if isinstance(binding, dict) else {}
    else:
        for field in ("source_sha256", "brief_sha256", "candidate_sha256",
                      "storyboard_sha256", "public_plan_sha256",
                      "controls_sha256"):
            if not (isinstance(binding.get(field), str)
                    and _SHA64_RE.match(binding[field] or "")):
                failures.append(f"binding {field} must be a sha256 hex")
        for field in ("width", "height"):
            if not _is_int(binding.get(field)) or binding[field] < 1:
                failures.append(f"binding {field} must be a positive int")
        if not _is_int(binding.get("seed")):
            failures.append("binding seed must be an integer")

    plan = parsed.get("plan")
    if not isinstance(plan, dict):
        failures.append("plan must be an object")
        plan = {}
    else:
        if any(str(k).startswith("_") for k in plan):
            failures.append("plan must not contain private keys")
        clip = plan.get("clip") if isinstance(plan.get("clip"), dict) else {}
        if not _is_int(clip.get("n_frames")) or clip.get("n_frames", 0) < 1:
            failures.append("plan clip n_frames must be a positive int")
        plan_scenes = plan.get("scenes")
        if not isinstance(plan_scenes, list) or not plan_scenes:
            failures.append("plan scenes must be a non-empty list")
            plan_scenes = []
        if isinstance(plan_scenes, list) and len(plan_scenes) > MAX_SCENES:
            failures.append("plan has too many scenes")

    # No absolute paths anywhere in the evidence payload.
    def _scan_paths(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if str(k).startswith("_"):
                    failures.append(f"private key in evidence: {path}.{k}")
                _scan_paths(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _scan_paths(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            if obj.startswith("/") and "/" in obj[1:]:
                failures.append(f"absolute path in evidence: {path}")

    _scan_paths(parsed, "evidence")

    # --- deterministic scenes/samples ---
    scenes: list[dict] = []
    n_frames = None
    if context is not None:
        try:
            scenes = _scenes_of(context.det_scenes)
            n_frames = int(context.clip["n_frames"])
        except Exception as e:  # noqa: BLE001
            failures.append(f"context scenes unavailable: {e}")
    elif isinstance(plan.get("scenes"), list) and plan["scenes"]:
        try:
            scenes = _scenes_of(plan["scenes"])
            n_frames = int(plan["clip"]["n_frames"])
        except Exception:  # noqa: BLE001
            failures.append("plan scenes are malformed")
    expected_samples, windows = ([], None)
    if scenes and stage in ("still", "motion"):
        for sc in scenes:
            if not (0 <= sc["start_frame"] < sc["end_frame"]):
                failures.append(f"scene out of bounds: {sc['id']}")
        if n_frames is not None and scenes \
                and scenes[-1]["end_frame"] > n_frames:
            failures.append("scenes exceed clip frame count")
        expected_samples, windows = _expected_samples(scenes, stage)

    samples = parsed.get("samples")
    if not isinstance(samples, list) or not samples:
        failures.append("samples must be a non-empty list")
        samples = samples if isinstance(samples, list) else []
    if len(samples) > MAX_SAMPLES:
        failures.append("too many samples")
    want_keys = _STILL_SAMPLE_KEYS if stage == "still" else _MOTION_SAMPLE_KEYS
    for i, s in enumerate(samples):
        if not isinstance(s, dict) or set(s) != want_keys:
            failures.append(f"samples[{i}] keys must be {sorted(want_keys)}")
            continue
        if s.get("role") not in ("start", "mid", "end"):
            failures.append(f"samples[{i}] role invalid")
        if not _is_int(s.get("source_frame")) or s.get("source_frame", -1) < 0:
            failures.append(f"samples[{i}] source_frame must be >= 0")
        elif s.get("file") != f"frame-{s['source_frame']:06d}.png":
            failures.append(f"samples[{i}] file does not match source frame")
        if stage == "motion" and not _is_int(s.get("preview_frame")):
            failures.append(f"samples[{i}] preview_frame must be an int")
    if expected_samples and samples != expected_samples:
        failures.append("samples do not match deterministic scene samples")
    if failures:
        # Later code indexes sample paths and reconstructs the sheet.
        # Malformed entries must be reported, not used as paths/keys.
        return _done()

    # --- media hashes / exact file set ---
    media = parsed.get("media_sha256")
    if not isinstance(media, dict):
        failures.append("media_sha256 must be an object")
        media = {}
    else:
        for k, v in media.items():
            if not (isinstance(v, str) and _SHA64_RE.match(v or "")):
                failures.append(f"media_sha256[{k}] must be a sha256 hex")
    sample_files = sorted({s["file"] for s in samples
                           if isinstance(s, dict) and "file" in s})
    expected_media = set(sample_files) | {"contact-sheet.jpg"}
    if stage == "motion":
        expected_media.add("preview.mp4")
    if stage == "still" and "preview.mp4" in name_set:
        failures.append("still evidence must not contain preview.mp4")
    if set(media) != expected_media:
        failures.append(
            f"media_sha256 keys {sorted(media)} != {sorted(expected_media)}")
    expected_files = expected_media | {"evidence.json"}
    actual_files = {n for n in names
                    if n in name_set and (base / n).is_file()
                    and not (base / n).is_symlink()}
    # review.json is the only permitted non-media extra file.
    if actual_files - {"review.json"} != expected_files:
        failures.append(
            f"evidence file set {sorted(actual_files)} "
            f"!= {sorted(expected_files | {'review.json'})} "
            f"(review.json optional)")
    for name in sorted(expected_media):
        p = base / name
        if not p.is_file():
            failures.append(f"missing media file: {name}")
            continue
        if media.get(name) != _sha256_file(p):
            failures.append(f"media hash mismatch: {name}")

    # --- motion_qa structure ---
    motion_qa = parsed.get("motion_qa")
    if stage == "still" and motion_qa is not None:
        failures.append("still motion_qa must be null")
    if stage == "motion":
        if not isinstance(motion_qa, dict) \
                or set(motion_qa or {}) != _MOTION_QA_KEYS:
            failures.append("motion_qa must have exactly the required keys")
            motion_qa = motion_qa if isinstance(motion_qa, dict) else {}
        else:
            if motion_qa.get("disjoint_windows") is not True:
                failures.append("motion_qa must declare disjoint windows")
            if motion_qa.get("has_audio") is not False:
                failures.append("motion_qa must declare no audio")
            th = motion_qa.get("thresholds")
            if not isinstance(th, dict):
                failures.append("motion_qa thresholds must be an object")
                th = {}
            if th.get("global_mae_max") != GLOBAL_MAE_MAX \
                    or th.get("patch_mae_max") != PATCH_MAE_MAX:
                failures.append("motion_qa thresholds must be 8/16")
            if windows is not None and motion_qa.get("windows") != windows:
                failures.append("motion_qa windows do not match scenes")
            qframes = motion_qa.get("frames")
            if not isinstance(qframes, list) \
                    or len(qframes) != len(samples):
                failures.append("motion_qa frames must cover every sample")
            else:
                for i, qf in enumerate(qframes):
                    if not isinstance(qf, dict):
                        failures.append(f"motion_qa frames[{i}] malformed")
                        continue
                    for field in ("global_mae", "patch_mae_max",
                                  "patch_mae_mean"):
                        if not _is_number(qf.get(field)):
                            failures.append(
                                f"motion_qa frames[{i}] {field} invalid")
                    want_pass = (
                        _is_number(qf.get("global_mae"))
                        and _is_number(qf.get("patch_mae_max"))
                        and qf["global_mae"] <= GLOBAL_MAE_MAX
                        and qf["patch_mae_max"] <= PATCH_MAE_MAX)
                    if qf.get("pass") is not want_pass:
                        failures.append(
                            f"motion_qa frames[{i}] pass flag inconsistent")
            if motion_qa.get("pass") is not True:
                failures.append("motion_qa did not pass")

    # --- live context binding/plan ---
    if context is not None:
        try:
            if binding != _binding_for_context(context):
                failures.append("binding does not match context inputs")
        except Exception as e:  # noqa: BLE001
            failures.append(f"cannot recompute context binding: {e}")
        try:
            if _canonical_bytes(plan) != _canonical_bytes(
                    context.public_plan):
                failures.append("plan does not match context public plan")
        except Exception as e:  # noqa: BLE001
            failures.append(f"cannot compare context plan: {e}")

    if failures:
        return _done()

    # --- decoded pixels / probe ---
    width = binding.get("width") if _is_int(binding.get("width")) else None
    height = binding.get("height") if _is_int(binding.get("height")) else None
    decoded_by_preview: dict[int, np.ndarray] = {}
    if stage == "motion" and "preview.mp4" in name_set \
            and width and height:
        facts, probe_failures = _probe_preview(base / "preview.mp4")
        failures.extend(probe_failures)
        if facts:
            if facts["width"] != width or facts["height"] != height:
                failures.append("preview dimensions do not match binding")
            if facts["fps"] != Fraction(MOTION_FPS, 1):
                failures.append("preview fps must be 30")
            total = sum(w["frame_count"] for w in windows) if windows else -1
            if facts["frame_count"] != total:
                failures.append("preview frame count does not match windows")
            if total > MAX_PREVIEW_FRAMES:
                failures.append("preview exceeds frame count bound")
            for s in samples:
                pf = s.get("preview_frame") if isinstance(s, dict) else None
                if _is_int(pf) and pf not in decoded_by_preview:
                    try:
                        decoded_by_preview[pf] = _qa.decode_frame_rgb(
                            str(base / "preview.mp4"), pf, width, height)
                    except Exception as e:  # noqa: BLE001
                        failures.append(f"cannot decode preview frame {pf}: {e}")
    # Native PNG dimensions + pixel correspondence.
    png_arrays: dict[str, np.ndarray] = {}
    for s in samples:
        if not isinstance(s, dict):
            continue
        name = s.get("file")
        p = base / str(name)
        if not p.is_file():
            continue
        try:
            with Image.open(p) as im:
                arr = np.asarray(im.convert("RGB"))
        except Exception as e:  # noqa: BLE001
            failures.append(f"cannot decode PNG {name}: {e}")
            continue
        png_arrays[str(name)] = arr
        if width and height and arr.shape != (height, width, 3):
            failures.append(f"PNG {name} is not native {width}x{height}")
            continue
        src = s.get("source_frame")
        if stage == "still" and context is not None and _is_int(src):
            try:
                if not np.array_equal(arr, np.asarray(context.frame(src))):
                    failures.append(
                        f"still PNG {name} does not equal reference frame")
            except Exception as e:  # noqa: BLE001
                failures.append(f"cannot render reference frame {src}: {e}")
        if stage == "motion" and _is_int(s.get("preview_frame")):
            dec = decoded_by_preview.get(s["preview_frame"])
            if dec is not None and not np.array_equal(arr, dec):
                failures.append(
                    f"motion PNG {name} does not equal decoded preview frame")
            if context is not None and dec is not None and _is_int(src):
                try:
                    m = _qa._frame_error_metrics(
                        dec, np.asarray(context.frame(src)))
                    if m["global_mae"] > GLOBAL_MAE_MAX \
                            or m["patch_mae_max"] > PATCH_MAE_MAX:
                        failures.append(
                            f"motion sample {name} exceeds QA thresholds")
                except Exception as e:  # noqa: BLE001
                    failures.append(f"cannot check motion sample {name}: {e}")
    # Contact sheet must be the deterministic rebuild from native PNGs.
    if width and height and samples and scenes \
            and "contact-sheet.jpg" in name_set:
        rows: list[list[np.ndarray]] = []
        complete = True
        ordered = samples
        idx = 0
        # Samples are stored scene-by-scene, 3 roles per scene.
        for _sc in scenes:
            row = []
            for _ in range(3):
                if idx >= len(ordered):
                    complete = False
                    break
                arr = png_arrays.get(ordered[idx].get("file", ""))
                if arr is None:
                    complete = False
                    break
                row.append(arr)
                idx += 1
            rows.append(row)
        if complete and len(rows) == len(scenes):
            try:
                buf = io.BytesIO()
                _contact_sheet(rows, width, height).save(
                    buf, format="JPEG", quality=88)
                if buf.getvalue() != (base / "contact-sheet.jpg").read_bytes():
                    failures.append(
                        "contact-sheet.jpg does not match native frames")
            except Exception as e:  # noqa: BLE001
                failures.append(f"cannot rebuild contact sheet: {e}")
    return _done()


def validate_review(review, evidence_dir, *, require_pass=False) -> dict:
    """Strictly validate a visual review bound to one evidence directory.

    ``review`` is the review dict (or a path to a review JSON file).
    The separate visual-review schema is enforced at runtime with
    exact fields: ``schema_version='1.0'``, ``stage``,
    ``evidence_sha256``, ``verdict``, ``checks`` (exactly the boolean
    ``evidence_reviewed``/``brief_adherence``/``visual_quality``/
    ``motion_quality``), ``blockers``, ``summary``, ``repair``
    (``route`` + ``instructions``) and the exact ``listening_note``.

    A ``pass`` requires all checks true, no blockers, and repair
    ``none`` with empty instructions. A ``fail`` requires blockers
    and a ``storyboard``/``engine`` route with instructions.
    ``summary`` is at most 4000 chars; blockers/instructions at most
    20 strings of 1..1000 chars. Stage and evidence digest must bind
    to the actual ``evidence.json`` in ``evidence_dir`` (which must
    itself validate). For still evidence, ``motion_quality: true``
    explicitly means not-applicable; it is not a motion judgement.
    With ``require_pass=True`` a structurally valid ``fail`` also
    fails here. This validates a visual preflight review only: it
    never binds a PR head and is not the full mechanical QA review.

    Returns ``{"ok", "failures", "review", "evidence_sha256"}``.
    """
    failures: list[str] = []
    ev = validate_evidence(evidence_dir)
    if not ev["ok"]:
        failures.extend(f"evidence: {f}" for f in ev["failures"])
    evidence_sha256 = ev["evidence_sha256"]
    evidence = ev["evidence"] if isinstance(ev.get("evidence"), dict) else {}
    ev_stage = evidence.get("stage")

    obj = review
    if isinstance(review, (str, Path)):
        try:
            raw = Path(review).read_bytes()
            if len(raw) > MAX_REVIEW_BYTES:
                failures.append("review file exceeds size bound")
            obj = _strict_json_loads(raw)
        except Exception as e:  # noqa: BLE001
            failures.append(f"review file is not valid strict JSON: {e}")
            obj = None
    if not isinstance(obj, dict):
        failures.append("review must be an object")
        return {"ok": False, "failures": failures, "review": None,
                "evidence_sha256": evidence_sha256}
    review = obj
    _check_finite(review, "review", failures)
    if set(review) != _REVIEW_KEYS:
        failures.append(
            f"review keys {sorted(review)} != {sorted(_REVIEW_KEYS)}")
    if review.get("schema_version") != REVIEW_SCHEMA_VERSION:
        failures.append("review schema_version must be '1.0'")
    if review.get("stage") not in ("still", "motion"):
        failures.append("review stage must be still/motion")
    elif ev_stage is not None and review.get("stage") != ev_stage:
        failures.append("review stage does not match evidence stage")
    if not (isinstance(review.get("evidence_sha256"), str)
            and _SHA64_RE.match(review["evidence_sha256"] or "")):
        failures.append("review evidence_sha256 must be a sha256 hex")
    elif evidence_sha256 is not None \
            and review.get("evidence_sha256") != evidence_sha256:
        failures.append("review evidence_sha256 does not match evidence")
    verdict = review.get("verdict")
    if verdict not in ("pass", "fail"):
        failures.append("review verdict must be pass/fail")
    checks = review.get("checks")
    if not isinstance(checks, dict) or set(checks or {}) != _REVIEW_CHECK_KEYS:
        failures.append("review checks must have exactly the required keys")
        checks = checks if isinstance(checks, dict) else {}
    else:
        for key in sorted(_REVIEW_CHECK_KEYS):
            if not isinstance(checks.get(key), bool):
                failures.append(f"review check must be a boolean: {key}")
    blockers = review.get("blockers")
    if not isinstance(blockers, list) \
            or any(not isinstance(b, str) or not b.strip()
                   or len(b) > 1000
                   for b in blockers or []):
        failures.append(
            "review blockers must be non-empty strings of <=1000 chars")
        blockers = blockers if isinstance(blockers, list) else []
    elif len(blockers) > 20:
        failures.append("review blockers exceeds 20 items")
    summary = review.get("summary")
    if not isinstance(summary, str) or not summary.strip() \
            or len(summary) > 4000:
        failures.append("review summary must be non-empty text <= 4000 chars")
    if review.get("listening_note") != LISTENING_NOTE:
        failures.append("review listening_note missing or incorrect")
    repair = review.get("repair")
    if not isinstance(repair, dict) or set(repair or {}) != _REPAIR_KEYS:
        failures.append("review repair must have exactly route/instructions")
        repair = repair if isinstance(repair, dict) else {}
        route, instructions = repair.get("route"), repair.get("instructions")
    else:
        route, instructions = repair.get("route"), repair.get("instructions")
        if route not in ("none", "storyboard", "engine"):
            failures.append("repair route must be none/storyboard/engine")
        if not isinstance(instructions, list) \
                or any(not isinstance(s, str) or not s.strip()
                       or len(s) > 1000
                       for s in instructions or []):
            failures.append(
                "repair instructions must be non-empty strings of <=1000 chars")
            instructions = instructions if isinstance(instructions, list) else []
        elif len(instructions) > 20:
            failures.append("repair instructions exceeds 20 items")
    if verdict == "pass":
        if isinstance(checks, dict) and set(checks) == _REVIEW_CHECK_KEYS:
            for key in sorted(_REVIEW_CHECK_KEYS):
                if checks.get(key) is not True:
                    failures.append(f"pass requires check true: {key}")
        if blockers:
            failures.append("pass requires no blockers")
        if route != "none" or instructions:
            failures.append("pass requires repair none with no instructions")
    elif verdict == "fail":
        if not blockers:
            failures.append("fail requires blockers")
        if route not in ("storyboard", "engine"):
            failures.append("fail requires storyboard/engine route")
        if not instructions:
            failures.append("fail requires repair instructions")
    if require_pass and verdict != "pass":
        failures.append("review verdict is not pass")
    return {"ok": not failures, "failures": failures, "review": review,
            "evidence_sha256": evidence_sha256}


def _load_review_file(evidence_dir: Path) -> tuple[dict, str]:
    """Load ``review.json`` from an evidence dir; raise on any problem."""
    path = Path(evidence_dir) / "review.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"missing review.json in {Path(evidence_dir).name}")
    raw = path.read_bytes()
    if len(raw) > MAX_REVIEW_BYTES:
        raise ValueError("review.json exceeds size bound")
    try:
        obj = _strict_json_loads(raw)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"review.json is not valid strict JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("review.json must be an object")
    return obj, _sha256_bytes(raw)


def approved_binding(still_dir, motion_dir, context, *,
                     expected_engine_sha, expected_run_id,
                     expected_run_attempt) -> dict:
    """Combine passing still+motion preflight reviews into an attestation.

    Both evidence directories are fully re-validated against
    ``context`` and the expected engine/run identity (no review flag
    is trusted without rechecking the actual files); each must carry
    a ``review.json`` that passes :func:`validate_review` with
    ``require_pass=True``. Stages must be still/motion respectively
    and both evidences must share the same iteration and render
    binding (identity except stage-independent fields, binding,
    plan). Raises ``ValueError`` on any failure.

    Returns a JSON-serialisable attestation containing both
    evidence/review hashes plus the shared identity and binding.
    """
    still = validate_evidence(
        still_dir, context=context, expected_engine_sha=expected_engine_sha,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt)
    if not still["ok"]:
        raise ValueError("still evidence invalid: "
                         + "; ".join(still["failures"]))
    motion = validate_evidence(
        motion_dir, context=context, expected_engine_sha=expected_engine_sha,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt)
    if not motion["ok"]:
        raise ValueError("motion evidence invalid: "
                         + "; ".join(motion["failures"]))
    sev, mev = still["evidence"], motion["evidence"]
    if sev.get("stage") != "still":
        raise ValueError("still_dir does not hold still evidence")
    if mev.get("stage") != "motion":
        raise ValueError("motion_dir does not hold motion evidence")
    still_review, still_review_sha = _load_review_file(Path(still_dir))
    motion_review, motion_review_sha = _load_review_file(Path(motion_dir))
    sr = validate_review(still_review, still_dir, require_pass=True)
    if not sr["ok"]:
        raise ValueError("still review invalid: " + "; ".join(sr["failures"]))
    mr = validate_review(motion_review, motion_dir, require_pass=True)
    if not mr["ok"]:
        raise ValueError("motion review invalid: " + "; ".join(mr["failures"]))
    if sev.get("identity") != mev.get("identity"):
        raise ValueError("still/motion identity mismatch (iteration/run)")
    if sev.get("binding") != mev.get("binding"):
        raise ValueError("still/motion render binding mismatch")
    if sev.get("plan") != mev.get("plan"):
        raise ValueError("still/motion plan mismatch")
    return {
        "schema_version": APPROVAL_SCHEMA_VERSION,
        "identity": sev["identity"],
        "binding": sev["binding"],
        "still_evidence_sha256": still["evidence_sha256"],
        "motion_evidence_sha256": motion["evidence_sha256"],
        "still_review_sha256": still_review_sha,
        "motion_review_sha256": motion_review_sha,
        "still": {"stage": "still",
                  "evidence_sha256": still["evidence_sha256"],
                  "review_sha256": still_review_sha},
        "motion": {"stage": "motion",
                   "evidence_sha256": motion["evidence_sha256"],
                   "review_sha256": motion_review_sha},
    }


def validate_approval_file(path, context, *, expected_engine_sha) -> dict:
    """Validate an approval file and delegate to :func:`approved_binding`.

    The file must be a strict JSON object with exactly
    ``{schema_version: '1.0', still_dir, motion_dir, engine_sha,
    run_id, run_attempt}``. ``still_dir``/``motion_dir`` are
    repository-relative references: absolute paths and ``..``
    traversal are rejected, and references resolve strictly against
    :data:`REPO_ROOT` (the repository root, parent of the ``studio``
    package; tests may monkeypatch it). There is no approval-file-
    relative fallback. A symlink in any ancestor path component of a
    resolved reference (not only the leaf) is rejected, and the
    resolved path must stay inside ``REPO_ROOT``. The file's
    ``engine_sha`` must equal ``expected_engine_sha``. Raises
    ``ValueError`` on failure; returns the
    :func:`approved_binding` attestation.
    """
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise ValueError("approval file missing")
    try:
        obj = _strict_json_loads(p.read_bytes())
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"approval file is not valid strict JSON: {e}") from e
    if not isinstance(obj, dict) or set(obj) != _APPROVAL_FILE_KEYS:
        raise ValueError(
            f"approval file keys must be {sorted(_APPROVAL_FILE_KEYS)}")
    if obj.get("schema_version") != APPROVAL_SCHEMA_VERSION:
        raise ValueError("approval schema_version must be '1.0'")
    if not (isinstance(obj.get("engine_sha"), str)
            and _SHA40_RE.match(obj["engine_sha"] or "")):
        raise ValueError("approval engine_sha must be a 40-hex sha")
    if obj["engine_sha"] != expected_engine_sha:
        raise ValueError("approval engine_sha does not match expected")
    for field in ("run_id", "run_attempt"):
        if not _is_int(obj.get(field)) or obj[field] < 1:
            raise ValueError(f"approval {field} must be a positive int")

    repo_root = Path(REPO_ROOT)

    def _resolve(ref, field: str) -> Path:
        if not isinstance(ref, str) or not ref:
            raise ValueError(f"approval {field} must be a relative path")
        if "\\" in ref or Path(ref).is_absolute() or ref.startswith("/"):
            raise ValueError(f"approval {field} must not be absolute")
        parts = Path(ref).parts
        if any(part == ".." for part in parts):
            raise ValueError(f"approval {field} must not contain '..'")
        if any(part in ("", ".") for part in parts):
            raise ValueError(f"approval {field} must be a clean relative path")
        candidate = repo_root.joinpath(*parts)
        # Reject a symlink in ANY ancestor component, not only the leaf.
        probe = repo_root
        for part in parts:
            probe = probe / part
            if probe.is_symlink():
                raise ValueError(
                    f"approval {field} must not traverse a symlink")
        try:
            resolved = candidate.resolve()
            root_resolved = repo_root.resolve()
        except OSError as e:
            raise ValueError(f"approval {field} cannot be resolved") from e
        if resolved != root_resolved \
                and root_resolved not in resolved.parents:
            raise ValueError(
                f"approval {field} must resolve inside the repository root")
        return candidate

    still_dir = _resolve(obj["still_dir"], "still_dir")
    motion_dir = _resolve(obj["motion_dir"], "motion_dir")
    return approved_binding(
        still_dir, motion_dir, context,
        expected_engine_sha=obj["engine_sha"],
        expected_run_id=obj["run_id"],
        expected_run_attempt=obj["run_attempt"])

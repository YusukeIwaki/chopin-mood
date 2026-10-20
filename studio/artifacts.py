"""Artifact ZIP validation + hash manifests (release cannot be forged
by editing PR text: hashes bind run, artifact, head and file contents)."""
from __future__ import annotations

import hashlib
import io
import stat
import zipfile

from . import config


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_hash_manifest(files: dict[str, bytes]) -> dict[str, str]:
    return {name: sha256_bytes(data) for name, data in sorted(files.items())}


def verify_hash_manifest(manifest: dict, files: dict[str, bytes]) -> dict:
    failures: list[str] = []
    if set(manifest) != set(files):
        failures.append(
            f"manifest/file set mismatch: manifest={sorted(manifest)} "
            f"files={sorted(files)}")
    for name, data in files.items():
        want = manifest.get(name)
        if want is not None and want != sha256_bytes(data):
            failures.append(f"hash mismatch: {name}")
    return {"ok": not failures, "failures": failures}


def validate_zip_entries(entries: list[dict], *,
                         expected: set[str] | None = None) -> dict:
    """entries: {name, size, external_attr, is_dir}. Rejects traversal,
    absolute paths, symlinks, oversize and unexpected files."""
    failures: list[str] = []
    total = 0
    names: list[str] = []
    for e in entries:
        name = e["name"]
        names.append(name)
        if e.get("is_dir"):
            continue
        if name.startswith("/") or name.startswith("\\") or "://" in name:
            failures.append(f"absolute path in zip: {name}")
        parts = name.replace("\\", "/").split("/")
        if any(p == ".." for p in parts):
            failures.append(f"traversal in zip: {name}")
        if "\\" in name:
            failures.append(f"backslash in zip: {name}")
        mode = (int(e.get("external_attr", 0)) >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            failures.append(f"symlink in zip: {name}")
        size = int(e.get("size", 0))
        total += size
        if size > config.MAX_ARTIFACT_FILE_BYTES:
            failures.append(f"oversize file in zip: {name} ({size})")
    if total > config.MAX_ARTIFACT_TOTAL_BYTES:
        failures.append(f"artifact total oversize: {total}")
    if expected is not None:
        got = {n for n in names if not n.endswith("/")}
        if got != set(expected):
            failures.append(f"unexpected file set: got {sorted(got)} "
                            f"want {sorted(expected)}")
    if len(names) != len(set(names)):
        failures.append("duplicate entries in zip")
    return {"ok": not failures, "failures": failures}


RECEIPT_FIELDS = ("pr", "child", "parent", "head_sha", "run_id",
                  "run_attempt", "engine_sha", "source_hashes",
                  "planning_fingerprint", "production_files")

MEDIA_SHA256_FILES = ("video.mp4", "contact-sheet.jpg", "timeline.html")
SOURCE_HASH_KEYS = ("attachment_sha256", "decoded_wav_sha256")


def _is_sha256_hex(v) -> bool:
    return (isinstance(v, str) and len(v) == 64
            and all(c in "0123456789abcdef" for c in v))


def _is_sha1_hex(v) -> bool:
    return (isinstance(v, str) and len(v) == 40
            and all(c in "0123456789abcdef" for c in v))


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def verify_media_provenance(provenance, files: dict, manifest: dict) -> dict:
    """Committed provenance.json ``media_sha256`` must name EXACTLY the
    rendered media files, and both the actual artifact bytes and the
    hash-manifest entry for each must equal the committed value. This
    is what makes an honestly-rehashed substituted video fail: the
    committed provenance (bound to the PR head) does not move."""
    failures: list[str] = []
    if not isinstance(provenance, dict):
        return {"ok": False, "failures": ["committed provenance.json is not an object"]}
    media = provenance.get("media_sha256")
    if not isinstance(media, dict):
        return {"ok": False, "failures": ["committed provenance media_sha256 missing"]}
    if set(media) != set(MEDIA_SHA256_FILES):
        failures.append(
            f"provenance media_sha256 set {sorted(media)} != {sorted(MEDIA_SHA256_FILES)}")
    for name in MEDIA_SHA256_FILES:
        want = media.get(name)
        if not _is_sha256_hex(want):
            failures.append(f"provenance media_sha256.{name} must be sha256 hex")
            continue
        data = (files or {}).get(name)
        if data is None:
            failures.append(f"artifact missing media file: {name}")
            continue
        if sha256_bytes(data) != want:
            failures.append(f"media bytes do not match committed provenance: {name}")
        if (manifest or {}).get(name) != want:
            failures.append(f"hash manifest does not match committed provenance: {name}")
    return {"ok": not failures, "failures": failures}


def verify_source_hashes(receipt, brief, provenance) -> dict:
    """Receipt attachment/decoded hashes must equal BOTH the brief and
    the (committed) provenance source_hashes, with the exact key set."""
    failures: list[str] = []
    rsh = receipt.get("source_hashes") if isinstance(receipt, dict) else None
    bsh = brief.get("source_hashes") if isinstance(brief, dict) else None
    psh = provenance.get("source_hashes") if isinstance(provenance, dict) else None
    for label, sh in (("receipt", rsh), ("brief", bsh), ("provenance", psh)):
        if not isinstance(sh, dict) or set(sh) != set(SOURCE_HASH_KEYS):
            failures.append(f"{label} source_hashes must have exactly {sorted(SOURCE_HASH_KEYS)}")
        elif any(not _is_sha256_hex(sh.get(k)) for k in SOURCE_HASH_KEYS):
            failures.append(f"{label} source_hashes values must be sha256 hex")
    if isinstance(rsh, dict) and isinstance(bsh, dict) and rsh != bsh:
        failures.append("receipt source_hashes do not match brief source_hashes")
    if isinstance(rsh, dict) and isinstance(psh, dict) and rsh != psh:
        failures.append("receipt source_hashes do not match provenance source_hashes")
    return {"ok": not failures, "failures": failures}


def build_receipt(*, pr: int, child: int, parent: int, head_sha: str,
                  run_id: int, run_attempt: int, engine_sha: str,
                  source_hashes: dict, planning_fingerprint: str,
                  production_files: dict[str, str]) -> dict:
    """Artifact-only binding (never committed): links the PR head, the
    trusted run and EVERY committed production path to its sha256."""
    return {
        "schema_version": "1.0", "pr": int(pr), "child": int(child),
        "parent": int(parent), "head_sha": head_sha, "run_id": int(run_id),
        "run_attempt": int(run_attempt), "engine_sha": engine_sha,
        "source_hashes": dict(source_hashes),
        "planning_fingerprint": planning_fingerprint,
        "production_files": dict(sorted(production_files.items())),
    }


def validate_receipt(receipt, *, pr: int, child: int, parent: int,
                     head_sha: str, production_files: dict[str, str],
                     expected_run_id: int | None = None,
                     expected_run_attempt: int | None = None,
                     expected_engine_sha: str | None = None,
                     expected_source_hashes: dict | None = None,
                     expected_planning_fingerprint: str | None = None) -> dict:
    """Strict receipt validation. No absent-field pass: every field in
    RECEIPT_FIELDS must be present with the exact type, and when the
    caller supplies the trusted run / brief expectations (the real
    controller always does) run_attempt, engine_sha, run_id, source
    hashes and fingerprint must equal them."""
    failures: list[str] = []
    if not isinstance(receipt, dict):
        return {"ok": False, "failures": ["missing receipt.json"]}
    for field in RECEIPT_FIELDS:
        if field not in receipt:
            failures.append(f"receipt missing field: {field}")
    for field, want in (("pr", pr), ("child", child), ("parent", parent)):
        if field in receipt:
            if not _is_int(receipt.get(field)):
                failures.append(f"receipt {field} must be an integer")
            elif receipt.get(field) != want:
                failures.append(f"receipt {field} mismatch")
    if "head_sha" in receipt and receipt.get("head_sha") != head_sha:
        failures.append("receipt head_sha mismatch")
    if not _is_sha1_hex(receipt.get("head_sha")):
        failures.append("receipt head_sha must be a 40-hex sha")
    for field in ("run_id", "run_attempt"):
        if field in receipt and not _is_int(receipt.get(field)):
            failures.append(f"receipt {field} must be an integer")
    if _is_int(receipt.get("run_attempt")) and receipt.get("run_attempt", 0) < 1:
        failures.append("receipt run_attempt must be >= 1")
    if expected_run_id is not None and receipt.get("run_id") != expected_run_id:
        failures.append("receipt run_id does not match workflow run")
    if expected_run_attempt is not None and receipt.get("run_attempt") != expected_run_attempt:
        failures.append("receipt run_attempt does not match run run_attempt")
    if not _is_sha1_hex(receipt.get("engine_sha")):
        failures.append("receipt engine_sha must be a 40-hex sha")
    if expected_engine_sha is not None and receipt.get("engine_sha") != expected_engine_sha:
        failures.append("receipt engine_sha does not match run head_sha")
    pf = receipt.get("production_files")
    if not isinstance(pf, dict):
        failures.append("receipt production_files must be an object")
    else:
        if pf != dict(sorted((production_files or {}).items())):
            failures.append("receipt production_files digest map mismatch")
        for k, v in pf.items():
            if not _is_sha256_hex(v):
                failures.append(f"receipt production_files.{k} must be sha256 hex")
    sh = receipt.get("source_hashes")
    if not isinstance(sh, dict) or set(sh if isinstance(sh, dict) else {}) != set(SOURCE_HASH_KEYS):
        failures.append(f"receipt source_hashes must have exactly {sorted(SOURCE_HASH_KEYS)}")
    else:
        for key in SOURCE_HASH_KEYS:
            if not _is_sha256_hex(sh.get(key)):
                failures.append(f"receipt source_hashes.{key} must be sha256 hex")
    if expected_source_hashes is not None and sh != expected_source_hashes:
        failures.append("receipt source_hashes do not match expected (brief/provenance)")
    fp = receipt.get("planning_fingerprint")
    if not _is_sha256_hex(fp):
        failures.append("receipt planning_fingerprint must be sha256 hex")
    if expected_planning_fingerprint is not None and fp != expected_planning_fingerprint:
        failures.append("receipt planning_fingerprint does not match brief fingerprint")
    return {"ok": not failures, "failures": failures}


def build_timeline_html(timeline: dict, *, title: str = "Studio timeline") -> str:
    """Self-contained timeline visualization (inline CSS/SVG only; no
    external files, no absolute paths, no data-file references)."""
    import html as _html
    import json as _json
    scenes = timeline.get("scenes", [])
    clip = timeline.get("clip", {})
    total = max(1, int(clip.get("n_frames", 1)))
    bars = []
    for sc in scenes:
        left = 100.0 * int(sc.get("start_frame", 0)) / total
        width = max(0.5, 100.0 * (int(sc.get("end_frame", 0))
                                  - int(sc.get("start_frame", 0))) / total)
        bars.append(
            f'<div class="sc" style="left:{left:.2f}%;width:{width:.2f}%">'
            f'{_html.escape(str(sc.get("id", "")))}'
            f'<span>{_html.escape(str(sc.get("duration_sec", "")))}s</span></div>')
    accents = timeline.get("accents", [])
    ticks = "".join(
        f'<i style="left:{100.0 * int(a.get("frame", 0)) / total:.2f}%"></i>'
        for a in accents if isinstance(a, dict))
    payload = _html.escape(_json.dumps(
        {"candidate_id": timeline.get("candidate_id"),
         "n_frames": total, "n_scenes": len(scenes),
         "n_accents": len(accents)}, ensure_ascii=False))
    return ("<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            f"<title>{_html.escape(title)}</title><style>"
            "body{font-family:system-ui,sans-serif;margin:2rem;color:#1c2430}"
            ".track{position:relative;height:64px;background:#e8edf5;border-radius:8px}"
            ".sc{position:absolute;top:8px;height:48px;background:#3b5bdb;color:#fff;"
            "border-radius:6px;font-size:12px;padding:4px;overflow:hidden}"
            ".sc span{display:block;opacity:.85}.acc{position:relative;height:18px}"
            ".acc i{position:absolute;width:2px;height:14px;background:#e8590c}"
            "pre{background:#f1f3f5;padding:1rem;border-radius:8px}</style></head><body>"
            f"<h1>{_html.escape(title)}</h1>"
            "<p>Deterministic DSP timings (native frames @30fps). "
            "No manual timeline edits.</p>"
            f"<div class=\"track\">{''.join(bars)}</div>"
            f"<div class=\"acc\">{ticks}</div>"
            f"<pre>{payload}</pre></body></html>")


def read_and_validate_zip(data: bytes, *,
                          expected: set[str] | None = None) -> dict:
    """Open ZIP bytes, validate entries, return {ok, failures, files}."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        return {"ok": False, "failures": [f"bad zip: {e}"], "files": {}}
    entries = [{"name": i.filename, "size": i.file_size,
                "external_attr": i.external_attr,
                "is_dir": i.is_dir()} for i in zf.infolist()]
    v = validate_zip_entries(entries, expected=expected)
    if not v["ok"]:
        return {"ok": False, "failures": v["failures"], "files": {}}
    files: dict[str, bytes] = {}
    for info in zf.infolist():
        if info.is_dir():
            continue
        # ZipFile already rejects absolute/traversal on extraction; we only
        # read in memory after validation above.
        files[info.filename] = zf.read(info.filename)
    return {"ok": True, "failures": [], "files": files}

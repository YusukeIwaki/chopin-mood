"""Synthetic tests for studio.visual_preflight (deterministic evidence).

Only synthetic audio and the real clean renderer are used. Contexts
come from prepare_context on a tiny 90-frame clip; motion evidence
goes through the real ffmpeg/ffprobe path at small size.
"""
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from p0 import qa as Q
from studio import storyboard as sbmod
from studio import visual_preflight as V
from studio.render_context import prepare_context

ROOT = Path(__file__).resolve().parents[1]
SR, W, H, SEED = 22050, 120, 200, 20261019
N_FRAMES = 90
CLIP_END = N_FRAMES * (SR // 30)
ENGINE_SHA = "a" * 40
RUN_ID, RUN_ATTEMPT, ITERATION = 123, 1, 0


def _candidate():
    mid_sample, mid_frame = CLIP_END // 2, N_FRAMES // 2
    return {
        "id": "cand-001", "start_sample": 0, "end_sample": CLIP_END,
        "n_frames": N_FRAMES, "duration_sec": round(CLIP_END / SR, 4),
        "score": 1.0, "scene_reason": "deterministic-test",
        "scenes": [
            {"id": "scene01", "start_sample": 0, "end_sample": mid_sample,
             "start_frame": 0, "end_frame": mid_frame,
             "duration_sec": round(mid_sample / SR, 4)},
            {"id": "scene02", "start_sample": mid_sample,
             "end_sample": CLIP_END, "start_frame": mid_frame,
             "end_frame": N_FRAMES,
             "duration_sec": round((CLIP_END - mid_sample) / SR, 4)},
        ],
    }


def _write_wav(path: Path, freq: float = 220.0) -> str:
    n = SR * 4
    t = np.arange(n) / SR
    y = 0.08 * np.sin(2 * np.pi * freq * t)
    for k, start in enumerate((0.5, 1.5, 2.5)):
        s = int(start * SR)
        L = min(int(0.8 * SR), n - s)
        te = np.arange(L) / SR
        y[s:s + L] += 0.30 * np.exp(-te * 3.0) * np.sin(
            2 * np.pi * (freq * (1.25 ** k)) * te)
    sf.write(str(path), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_bundle(base: Path, *, freq: float = 220.0, sb_obj=None,
                 wav_name: str = "synthetic.wav"):
    base.mkdir(parents=True, exist_ok=True)
    wav = base / wav_name
    source_sha = _write_wav(wav, freq=freq)
    cand = _candidate()
    cand_path = base / "candidate.json"
    cand_path.write_text(json.dumps(cand, indent=2))
    sb = sb_obj if sb_obj is not None else sbmod.default_storyboard(
        cand["scenes"])
    sb_path = base / "storyboard.json"
    sb_path.write_text(json.dumps(sb, indent=2))
    brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
             "candidate_id": cand["id"], "fingerprint": "0" * 64,
             "candidate": cand,
             "source_hashes": {"decoded_wav_sha256": source_sha,
                               "attachment_sha256": "0" * 64}}
    brief_path = base / "brief.json"
    brief_path.write_text(json.dumps(brief, indent=2))
    return {"wav": wav, "candidate": cand_path, "storyboard": sb_path,
            "brief": brief_path, "cand_obj": cand, "sb_obj": sb,
            "brief_obj": brief, "source_sha": source_sha}


def _prepare(bundle, **kw):
    kw.setdefault("width", W)
    kw.setdefault("height", H)
    kw.setdefault("seed", SEED)
    return prepare_context(bundle["wav"], bundle["candidate"],
                           bundle["storyboard"], bundle["brief"], **kw)


def _gen(ctx, out, stage):
    return V.generate_evidence(ctx, out, stage=stage,
                               engine_sha=ENGINE_SHA, run_id=RUN_ID,
                               run_attempt=RUN_ATTEMPT,
                               iteration=ITERATION)


def _digest(directory) -> str:
    return hashlib.sha256(
        (Path(directory) / "evidence.json").read_bytes()).hexdigest()


def _review(directory, *, verdict="pass", route=None, blockers=None,
            instructions=None, digest=None, stage=None, checks=None):
    ev = json.loads((Path(directory) / "evidence.json").read_text())
    return {
        "schema_version": "1.0",
        "stage": stage or ev["stage"],
        "evidence_sha256": digest or _digest(directory),
        "verdict": verdict,
        "checks": checks or {"evidence_reviewed": True,
                             "brief_adherence": True,
                             "visual_quality": True,
                             "motion_quality": True},
        "blockers": list(blockers or []),
        "summary": "Synthetic review of the attached evidence.",
        "repair": {"route": route if route is not None
                   else ("none" if verdict == "pass" else "storyboard"),
                   "instructions": list(instructions or [])},
        "listening_note": V.LISTENING_NOTE,
    }


def _write_review(directory, review) -> None:
    (Path(directory) / "review.json").write_text(json.dumps(review, indent=2))


def _copy_evidence(src, dst):
    shutil.copytree(str(src), str(dst))
    return Path(dst)


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _make_bundle(tmp_path_factory.mktemp("vp-bundle"))


@pytest.fixture(scope="module")
def ctx(bundle):
    return _prepare(bundle)


@pytest.fixture(scope="module")
def still_dir(tmp_path_factory, ctx):
    out = tmp_path_factory.mktemp("vp-still") / "still"
    _gen(ctx, out, "still")
    return out


@pytest.fixture(scope="module")
def motion_dir(tmp_path_factory, ctx):
    out = tmp_path_factory.mktemp("vp-motion") / "motion"
    _gen(ctx, out, "motion")
    return out


# --- generation: samples, bounds, determinism -----------------------

def test_still_samples_cover_all_scenes_at_native_size(ctx, still_dir):
    ev = json.loads((still_dir / "evidence.json").read_text())
    assert set(ev) == {"version", "stage", "identity", "binding", "plan",
                       "samples", "motion_qa", "media_sha256"}
    assert ev["stage"] == "still" and ev["motion_qa"] is None
    assert ev["identity"] == {"engine_sha": ENGINE_SHA, "run_id": RUN_ID,
                              "run_attempt": RUN_ATTEMPT,
                              "iteration": ITERATION, "parent_issue": 1,
                              "child_issue": 2, "candidate_id": "cand-001"}
    expected = []
    for sc in ctx.det_scenes:
        s0, e1 = sc["start_frame"], sc["end_frame"] - 1
        for role, src in (("start", s0), ("mid", (s0 + e1) // 2),
                          ("end", e1)):
            expected.append({"scene_id": sc["id"], "role": role,
                             "source_frame": src,
                             "file": f"frame-{src:06d}.png"})
    assert ev["samples"] == expected
    from PIL import Image
    for s in expected:
        with Image.open(still_dir / s["file"]) as im:
            assert im.size == (W, H)
    with Image.open(still_dir / "contact-sheet.jpg") as sheet:
        assert sheet.size == (3 * (W // 2), len(ctx.det_scenes) * (H // 2))
    result = V.validate_evidence(still_dir, ctx,
                                 expected_engine_sha=ENGINE_SHA,
                                 expected_run_id=RUN_ID,
                                 expected_run_attempt=RUN_ATTEMPT)
    assert result["ok"], result["failures"]
    assert result["evidence_sha256"] == _digest(still_dir)


def test_still_evidence_is_deterministic(ctx, tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    _gen(ctx, a, "still")
    _gen(ctx, b, "still")
    assert (a / "evidence.json").read_bytes() == (b / "evidence.json").read_bytes()
    for name in ("contact-sheet.jpg", "frame-000000.png"):
        assert (a / name).read_bytes() == (b / name).read_bytes()


def test_fresh_directory_required(ctx, still_dir):
    with pytest.raises(ValueError, match="fresh"):
        _gen(ctx, still_dir, "still")
    with pytest.raises(ValueError, match="stage"):
        V.generate_evidence(ctx, still_dir.parent / "x", stage="bogus",
                            engine_sha=ENGINE_SHA, run_id=RUN_ID,
                            run_attempt=RUN_ATTEMPT, iteration=ITERATION)
    with pytest.raises(ValueError, match="engine_sha"):
        V.generate_evidence(ctx, still_dir.parent / "y", stage="still",
                            engine_sha="zz", run_id=RUN_ID,
                            run_attempt=RUN_ATTEMPT, iteration=ITERATION)


def test_motion_preview_real_ffmpeg_noaudio_exact_map(ctx, motion_dir):
    ev = json.loads((motion_dir / "evidence.json").read_text())
    assert ev["stage"] == "motion"
    assert ev["motion_qa"]["has_audio"] is False
    assert ev["motion_qa"]["disjoint_windows"] is True
    windows = ev["motion_qa"]["windows"]
    # Windows: <=60 frames, centred on and bounded by each scene.
    preview = 0
    for win, sc in zip(windows, ctx.det_scenes):
        assert win["frame_count"] <= 60
        assert sc["start_frame"] <= win["source_start_frame"]
        assert win["source_end_frame"] <= sc["end_frame"]
        mid = (sc["start_frame"] + sc["end_frame"] - 1) // 2
        wmid = (win["source_start_frame"] + win["source_end_frame"] - 1) // 2
        assert abs(wmid - mid) <= 1
        assert win["preview_start_frame"] == preview
        preview = win["preview_end_frame"]
    for a, b in zip(windows, windows[1:]):
        assert a["source_end_frame"] <= b["source_start_frame"]
    # Exact sample map: start/mid/end of each window.
    expected = []
    for win in windows:
        ws, we, ps = (win["source_start_frame"], win["source_end_frame"],
                      win["preview_start_frame"])
        for role, src in (("start", ws), ("mid", (ws + we - 1) // 2),
                          ("end", we - 1)):
            expected.append({"scene_id": win["scene_id"], "role": role,
                             "source_frame": src,
                             "preview_frame": ps + (src - ws),
                             "file": f"frame-{src:06d}.png"})
    assert ev["samples"] == expected
    # Real ffprobe: one silent 30fps video stream, exact frame count.
    probe = Q.ffprobe(str(motion_dir / "preview.mp4"))
    kinds = [s["codec_type"] for s in probe["streams"]]
    assert kinds == ["video"]
    v = probe["streams"][0]
    assert (v["width"], v["height"]) == (W, H)
    assert v["avg_frame_rate"] == "30/1"
    assert int(v["nb_read_frames"]) == preview
    result = V.validate_evidence(motion_dir, ctx,
                                 expected_engine_sha=ENGINE_SHA)
    assert result["ok"], result["failures"]


# --- tampering / stale bindings -------------------------------------

def test_media_tamper_fails(ctx, still_dir, motion_dir, tmp_path):
    bad = _copy_evidence(still_dir, tmp_path / "bad-png")
    png = bad / "frame-000000.png"
    data = bytearray(png.read_bytes())
    data[-20] ^= 0xFF
    png.write_bytes(bytes(data))
    assert not V.validate_evidence(bad, ctx)["ok"]

    bad2 = _copy_evidence(motion_dir, tmp_path / "bad-mp4")
    with open(bad2 / "preview.mp4", "ab") as fh:
        fh.write(b"tamper")
    result = V.validate_evidence(bad2, ctx)
    assert not result["ok"]
    assert any("preview.mp4" in f for f in result["failures"])


def test_controls_tamper_fails(ctx, still_dir):
    controls = {k: np.array(v, copy=True) for k, v in ctx.controls.items()}
    controls["light"] = controls["light"] + 0.25
    tampered = replace(ctx, controls=controls)
    result = V.validate_evidence(still_dir, tampered)
    assert not result["ok"]
    assert any("binding" in f for f in result["failures"])


def test_stale_engine_and_run_fail(still_dir):
    assert not V.validate_evidence(
        still_dir, expected_engine_sha="b" * 40)["ok"]
    assert not V.validate_evidence(still_dir, expected_run_id=999)["ok"]
    assert not V.validate_evidence(still_dir, expected_run_attempt=9)["ok"]


def test_new_storyboard_source_seed_invalidate(bundle, ctx, still_dir,
                                               tmp_path):
    # New storyboard (palette change): binding + pixels move.
    sb2 = copy.deepcopy(bundle["sb_obj"])
    sb2["scenes"][0]["palette"]["sky_top"] = [200, 30, 30]
    b2 = _make_bundle(tmp_path / "b2", sb_obj=sb2)
    assert not V.validate_evidence(still_dir, _prepare(b2))["ok"]
    # New source audio: source hash (and DSP inputs) move.
    b3 = _make_bundle(tmp_path / "b3", freq=330.0)
    assert not V.validate_evidence(still_dir, _prepare(b3))["ok"]
    # New seed: seed/controls/layout binding moves.
    assert not V.validate_evidence(
        still_dir, _prepare(bundle, seed=SEED + 1))["ok"]


def test_shape_strictness_and_bool_as_int(still_dir, motion_dir, tmp_path):
    bad = _copy_evidence(still_dir, tmp_path / "bad-shape")
    ev = json.loads((bad / "evidence.json").read_text())
    ev["identity"]["run_id"] = True  # bool is not an int
    (bad / "evidence.json").write_bytes(V._canonical_bytes(ev))
    assert not V.validate_evidence(bad)["ok"]

    bad2 = _copy_evidence(still_dir, tmp_path / "bad-extra")
    ev2 = json.loads((bad2 / "evidence.json").read_text())
    ev2["unexpected"] = 1
    (bad2 / "evidence.json").write_bytes(V._canonical_bytes(ev2))
    assert not V.validate_evidence(bad2)["ok"]

    bad3 = _copy_evidence(still_dir, tmp_path / "bad-sample")
    ev3 = json.loads((bad3 / "evidence.json").read_text())
    ev3["samples"][0]["source_frame"] += 1
    (bad3 / "evidence.json").write_bytes(V._canonical_bytes(ev3))
    assert not V.validate_evidence(bad3)["ok"]

    # Review shape: extra key, missing check, non-bool check.
    review = _review(still_dir)
    review["extra"] = 1
    assert not V.validate_review(review, still_dir)["ok"]
    review = _review(still_dir)
    del review["checks"]["motion_quality"]
    assert not V.validate_review(review, still_dir)["ok"]
    review = _review(still_dir)
    review["checks"]["visual_quality"] = 1
    assert not V.validate_review(review, still_dir)["ok"]
    # Schema file declares the same exact fields.
    schema = json.loads((ROOT / ".github/studio/schemas"
                         / "visual-review.schema.json").read_text())
    assert set(schema["required"]) == {
        "schema_version", "stage", "evidence_sha256", "verdict", "checks",
        "blockers", "summary", "repair", "listening_note"}
    assert schema["properties"]["listening_note"]["enum"] == [
        V.LISTENING_NOTE]


def test_symlink_extra_and_missing_files(still_dir, tmp_path):
    extra = _copy_evidence(still_dir, tmp_path / "extra")
    (extra / "notes.txt").write_text("nope")
    assert not V.validate_evidence(extra)["ok"]

    link = _copy_evidence(still_dir, tmp_path / "link")
    (link / "frame-999999.png").symlink_to(link / "frame-000000.png")
    assert not V.validate_evidence(link)["ok"]

    missing = _copy_evidence(still_dir, tmp_path / "missing")
    (missing / "frame-000000.png").unlink()
    assert not V.validate_evidence(missing)["ok"]

    subdir = _copy_evidence(still_dir, tmp_path / "subdir")
    (subdir / "nested").mkdir()
    assert not V.validate_evidence(subdir)["ok"]


def test_review_digest_stale_and_pass_rules(still_dir, motion_dir):
    review = _review(still_dir)
    assert V.validate_review(review, still_dir)["ok"]
    stale = dict(review, evidence_sha256="0" * 64)
    assert not V.validate_review(stale, still_dir)["ok"]
    wrong_stage = _review(motion_dir, stage="still")
    assert not V.validate_review(wrong_stage, motion_dir)["ok"]
    # Pass with a false check / blocker / repair is invalid.
    for mutate in (
            lambda r: r["checks"].__setitem__("visual_quality", False),
            lambda r: r.__setitem__("blockers", ["visible defect"]),
            lambda r: r["repair"].__setitem__("route", "storyboard")):
        r = _review(still_dir)
        mutate(r)
        assert not V.validate_review(r, still_dir)["ok"], r
    # A structurally valid fail routes with full blocker detail.
    fail = _review(still_dir, verdict="fail", blockers=["figure unreadable"],
                   instructions=["scene01: raise figure_scale and verify"])
    assert V.validate_review(fail, still_dir)["ok"]
    assert not V.validate_review(fail, still_dir, require_pass=True)["ok"]
    no_route = _review(still_dir, verdict="fail", route="none",
                       blockers=["x"], instructions=[])
    assert not V.validate_review(no_route, still_dir)["ok"]


# --- approval --------------------------------------------------------

def _approved_dirs(still_dir, motion_dir, tmp_path, *, motion_fail=False):
    s = _copy_evidence(still_dir, tmp_path / "still")
    m = _copy_evidence(motion_dir, tmp_path / "motion")
    _write_review(s, _review(s))
    if motion_fail:
        _write_review(m, _review(
            m, verdict="fail", blockers=["sampled motion jumps"],
            instructions=["scene01: fix numeric camera and re-render"]))
    else:
        _write_review(m, _review(m))
    return s, m


def test_still_pass_motion_fail_blocks_approval(ctx, still_dir, motion_dir,
                                                tmp_path):
    s, m = _approved_dirs(still_dir, motion_dir, tmp_path, motion_fail=True)
    with pytest.raises(ValueError, match="motion review"):
        V.approved_binding(s, m, ctx, expected_engine_sha=ENGINE_SHA,
                           expected_run_id=RUN_ID,
                           expected_run_attempt=RUN_ATTEMPT)


def test_valid_approval_binding_and_file(ctx, bundle, still_dir, motion_dir,
                                         tmp_path, monkeypatch):
    # Strict REPO_ROOT rule: refs resolve only inside the patched root.
    monkeypatch.setattr(V, "REPO_ROOT", tmp_path)
    s, m = _approved_dirs(still_dir, motion_dir, tmp_path)
    att = V.approved_binding(s, m, ctx, expected_engine_sha=ENGINE_SHA,
                             expected_run_id=RUN_ID,
                             expected_run_attempt=RUN_ATTEMPT)
    assert att["still_evidence_sha256"] == _digest(s)
    assert att["motion_evidence_sha256"] == _digest(m)
    assert att["identity"]["candidate_id"] == "cand-001"
    assert att["binding"]["source_sha256"] == ctx.source_sha256
    json.dumps(att)  # serialisable attestation

    approval = {"schema_version": "1.0", "still_dir": "still",
                "motion_dir": "motion", "engine_sha": ENGINE_SHA,
                "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT}
    apath = tmp_path / "approval.json"
    apath.write_text(json.dumps(approval))
    att2 = V.validate_approval_file(apath, ctx,
                                    expected_engine_sha=ENGINE_SHA)
    assert att2 == att
    # Traversal / absolute refs and stale engine are rejected.
    for ref in ("../still", "/tmp/still"):
        bad = dict(approval, still_dir=ref)
        apath.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            V.validate_approval_file(apath, ctx,
                                     expected_engine_sha=ENGINE_SHA)
    apath.write_text(json.dumps(approval))
    with pytest.raises(ValueError, match="engine_sha"):
        V.validate_approval_file(apath, ctx, expected_engine_sha="b" * 40)
    (tmp_path / "link").symlink_to(tmp_path, target_is_directory=True)
    apath.write_text(json.dumps(dict(approval, still_dir="link/still")))
    with pytest.raises(ValueError, match="symlink"):
        V.validate_approval_file(apath, ctx, expected_engine_sha=ENGINE_SHA)


def test_approval_flag_requires_engine_sha(bundle, tmp_path):
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/studio_render.py"),
         "--wav", str(bundle["wav"]), "--candidate", str(bundle["candidate"]),
         "--storyboard", str(bundle["storyboard"]),
         "--brief", str(bundle["brief"]), "--out", str(tmp_path / "out"),
         "--width", str(W), "--height", str(H),
         "--visual-approval", str(tmp_path / "approval.json")],
        capture_output=True, text=True, cwd=str(ROOT), timeout=600)
    assert r.returncode == 2, (r.stdout[-1000:], r.stderr[-1000:])
    assert "requires --engine-sha" in r.stdout


def test_valid_approval_provenance(bundle, ctx, still_dir, motion_dir,
                                   tmp_path, request):
    # The real CLI subprocess uses the real repository root. A patched
    # module global in this pytest process cannot change its resolver.
    (ROOT / "output").mkdir(exist_ok=True)
    layout = Path(tempfile.mkdtemp(prefix="visual-test-", dir=ROOT / "output"))
    request.addfinalizer(lambda: shutil.rmtree(layout))
    s2, m2 = _approved_dirs(still_dir, motion_dir, layout)
    approval = {"schema_version": "1.0", "still_dir": str(s2.relative_to(ROOT)),
                "motion_dir": str(m2.relative_to(ROOT)), "engine_sha": ENGINE_SHA,
                "run_id": RUN_ID, "run_attempt": RUN_ATTEMPT}
    apath = layout / "approval.json"
    apath.write_text(json.dumps(approval))
    out = tmp_path / "render-out"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/studio_render.py"),
         "--wav", str(bundle["wav"]), "--candidate", str(bundle["candidate"]),
         "--storyboard", str(bundle["storyboard"]),
         "--brief", str(bundle["brief"]), "--out", str(out),
         "--width", str(W), "--height", str(H),
         "--visual-approval", str(apath), "--engine-sha", ENGINE_SHA],
        capture_output=True, text=True, cwd=str(ROOT), timeout=1200)
    # The tiny 3 s fixture clip cannot satisfy the full renderer's
    # 6 s minimum-scene QA invariant (pre-existing check_file rule),
    # so the CLI exits 2 on QA -- but only after the approval was
    # validated and the encode/provenance completed. That is the
    # behaviour bound here; QA itself is covered by existing tests.
    assert (out / "video.mp4").is_file()
    qa = json.loads((out / "qa.json").read_text())
    assert [c["name"] for c in qa["checks"] if not c["pass"]] == [
        "studio:scene_invariants"], qa
    prov = json.loads((out / "provenance.json").read_text())
    assert prov["visual_approval"]["still_evidence_sha256"] == _digest(s2)
    assert prov["visual_approval"]["motion_evidence_sha256"] == _digest(m2)
    assert prov["visual_approval"]["identity"]["engine_sha"] == ENGINE_SHA
    # studio_render's own output file set is unchanged by the flag.
    names = {p.name for p in out.iterdir() if p.is_file()}
    assert names == {"video.mp4", "plan.json", "qa.json", "storyboard.json",
                     "brief.json", "provenance.json", "timeline.json",
                     "timeline.html", "contact-sheet.jpg", "hashes.json"}


@pytest.mark.parametrize("mutation", [
    lambda e: e.__setitem__("samples", [None]),
    lambda e: e["samples"][0].__setitem__("file", {"bad": 1}),
    lambda e: e["samples"][0].__setitem__("file", "../../outside.png"),
])
def test_malformed_samples_fail_before_media_read(still_dir, tmp_path, monkeypatch, mutation):
    bad = _copy_evidence(still_dir, tmp_path / "bad")
    ev = json.loads((bad / "evidence.json").read_text())
    mutation(ev)
    (bad / "evidence.json").write_bytes(V._canonical_bytes(ev))
    def unexpected(*args, **kwargs):
        pytest.fail("invalid sample inventory must not reach image decoding")
    monkeypatch.setattr(V.Image, "open", unexpected)
    assert not V.validate_evidence(bad)["ok"]


def test_invalid_inventory_never_follows_symlink(still_dir, tmp_path, monkeypatch):
    bad = _copy_evidence(still_dir, tmp_path / "bad")
    (bad / "evidence.json").unlink()
    (bad / "evidence.json").symlink_to(still_dir / "evidence.json")
    original = Path.read_bytes
    def no_link_read(path):
        assert path != bad / "evidence.json", "read through rejected symlink"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", no_link_read)
    assert not V.validate_evidence(bad)["ok"]


@pytest.mark.parametrize("field", ["summary", "blockers", "instructions"])
def test_review_rejects_whitespace_only(still_dir, field):
    r = _review(still_dir, verdict="fail", blockers=["visible defect"],
                instructions=["change numeric palette"])
    if field == "instructions":
        r["repair"][field] = [" \n "]
    else:
        r[field] = " \n " if field == "summary" else [" \n "]
    assert not V.validate_review(r, still_dir)["ok"]

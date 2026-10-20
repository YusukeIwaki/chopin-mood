"""Studio controller tests: gates, validation, idempotency, lifecycle.

Offline only: FakeGitHub fixtures, no network, synthetic signals only.
"""
import io
import json
import stat
import zipfile

import numpy as np
import pytest

from studio import (artifacts, candidates, config, fingerprint, gating,
                    lifecycle, storyboard, validate)
from studio.github_client import FakeGitHub

HEAD = "a" * 40
GOOD_URL = "https://github.com/user-attachments/files/123/rec.bin"


# --- intake / attachment -------------------------------------------------
def test_intake_owner_only():
    ok = validate.validate_intake(author="YusukeIwaki", sender="YusukeIwaki",
                                  issue_state="open")
    assert ok["ok"]
    assert not validate.validate_intake(
        author="someone", sender="YusukeIwaki", issue_state="open")["ok"]
    assert not validate.validate_intake(
        author="YusukeIwaki", sender="someone", issue_state="open")["ok"]
    assert not validate.validate_intake(
        author="YusukeIwaki", sender="YusukeIwaki", issue_state="closed")["ok"]
    # Dispatch never waives the source-author check.
    assert validate.validate_intake(
        author="YusukeIwaki", sender=None, issue_state="open", via_dispatch=True,
        dispatch_actor="YusukeIwaki")["ok"]
    assert not validate.validate_intake(
        author="someone-else", sender=None, issue_state="open", via_dispatch=True,
        dispatch_actor="YusukeIwaki")["ok"]
    assert not validate.validate_intake(
        author="YusukeIwaki", sender=None, issue_state="open", via_dispatch=True,
        dispatch_actor="bot")["ok"]


def test_attachment_url_rules():
    assert validate.validate_attachment_url(GOOD_URL)["ok"]
    for bad in ("http://github.com/user-attachments/files/1/x",
                "https://example.com/user-attachments/files/1/x",
                "https://github.com/other/path",
                "file:///etc/passwd",
                "https://user:pass@github.com/user-attachments/files/1/x",
                "https://localhost/user-attachments/files/1/x"):
        assert not validate.validate_attachment_url(bad)["ok"], bad
    chain = validate.validate_redirect_chain(
        [GOOD_URL, "https://github.com/user-attachments/files/123/r"])
    assert chain["ok"]
    assert not validate.validate_redirect_chain(
        [GOOD_URL, "http://evil.example/x"])["ok"]


def test_media_limits():
    assert validate.validate_media_limits(
        size_bytes=100, duration_sec=60, sample_rate=44100, channels=2)["ok"]
    assert not validate.validate_media_limits(
        size_bytes=config.MAX_SOURCE_BYTES + 1, duration_sec=60,
        sample_rate=44100, channels=2)["ok"]
    assert not validate.validate_media_limits(
        size_bytes=100, duration_sec=0, sample_rate=44100, channels=2)["ok"]
    assert not validate.validate_media_limits(
        size_bytes=100, duration_sec=60, sample_rate=192000, channels=2)["ok"]


# --- candidates -----------------------------------------------------------
def _cand(cid, s0, dur_samples, sr=22050):
    nf = int(dur_samples / sr * 30)
    return {"id": cid, "start_sample": s0,
            "end_sample": candidates.frame_boundary_sample(s0, nf, sr, 30, 1),
            "n_frames": nf, "sample_rate": sr, "fps": [30, 1],
            "duration_sec": dur_samples / sr}


def test_candidate_bounds_and_diversity():
    c = _cand("cand-01", 0, 22050 * 90)
    assert candidates.validate_candidate(c) == []
    short = _cand("cand-02", 0, 22050 * 30)
    assert any("below min" in e for e in candidates.validate_candidate(short))
    bad = dict(c, end_sample=c["end_sample"] + 1)
    assert any("frame boundary" in e for e in candidates.validate_candidate(bad))
    ranked = [_cand("cand-01", 0, 22050 * 90),
              _cand("cand-02", 22050 * 5, 22050 * 90),
              _cand("cand-03", 22050 * 200, 22050 * 70)]
    picked = candidates.diversity_filter(ranked, max_n=3)
    assert [p["id"] for p in picked] == ["cand-01", "cand-03"]  # overlap dropped


def test_planner_unknown_id():
    cands = [_cand("cand-01", 0, 22050 * 90)]
    assert candidates.validate_planner_selection(["cand-01"], cands)["ok"]
    r = candidates.validate_planner_selection(["cand-99"], cands)
    assert not r["ok"] and any("unknown" in f for f in r["failures"])
    assert not candidates.validate_planner_selection(
        ["cand-01", "cand-01"], cands)["ok"]


# --- storyboard schema ------------------------------------------------------
SCENES = [{"id": "scene01"}, {"id": "scene02"}]


def _sb(**over):
    sb = storyboard.default_storyboard(SCENES)
    sb.update(over)
    return sb


def test_storyboard_valid_and_rejects():
    assert storyboard.validate_storyboard(_sb(), SCENES)["ok"]
    bad = _sb()
    bad["scenes"][0]["params"]["figure_scale"] = 99
    assert not storyboard.validate_storyboard(bad, SCENES)["ok"]
    bad2 = _sb()
    bad2["scenes"][0]["path"] = "/etc/passwd"
    assert not storyboard.validate_storyboard(bad2, SCENES)["ok"]
    bad3 = _sb()
    bad3["scenes"] = list(reversed(bad3["scenes"]))
    assert not storyboard.validate_storyboard(bad3, SCENES)["ok"]
    bad4 = _sb()
    bad4["scenes"][0]["palette"]["sky_top"] = [300, 0, 0]
    assert not storyboard.validate_storyboard(bad4, SCENES)["ok"]


def test_production_path_allowlist():
    assert validate.validate_production_file("production/12/brief.json", 10)["ok"]
    assert not validate.validate_production_file("production/12/video.mp4", 10)["ok"]
    assert not validate.validate_production_file("../brief.json", 10)["ok"]
    assert not validate.validate_production_file("/abs/brief.json", 10)["ok"]


# --- fingerprint / idempotency ---------------------------------------------
def test_fingerprint_and_intake_idempotency():
    fp = fingerprint.canonical_fingerprint(
        source_url=GOOD_URL, source_sha256="B" * 64, period="Late")
    assert fp == fingerprint.canonical_fingerprint(
        source_url=GOOD_URL, source_sha256="b" * 64, period="late")
    assert fingerprint.decide_intake(
        existing_fingerprint=fp, new_fingerprint=fp, has_children=True
    )["action"] == "reuse"
    assert fingerprint.decide_intake(
        existing_fingerprint=fp, new_fingerprint="0" * 64, has_children=True
    )["action"] == "fail_new_issue"
    assert fingerprint.parse_fingerprint(fingerprint.fingerprint_marker(fp)) == fp


def test_review_notify_idempotent():
    key = lifecycle.artifact_key(7, HEAD, 42)
    assert lifecycle.should_notify(["nothing"], key)
    assert not lifecycle.should_notify([f"Key: {key}"], key)


# --- release gates -----------------------------------------------------------
def _release_ctx(**over):
    ctx = {
        "pr": {"number": 7, "merged": True, "merged_by": "YusukeIwaki",
               "base": "main", "head_sha": HEAD},
        "reviews": [{"user": "YusukeIwaki", "state": "COMMENTED", "commit_id": HEAD},
                    {"user": "YusukeIwaki", "state": "APPROVED", "commit_id": HEAD}],
        "review": {"verdict": "pass", "reviewed_head_sha": HEAD,
                   "checks": {"mechanical_qa_reviewed": True,
                              "brief_adherence": True, "timing_integrity": True,
                              "aesthetic_approval": True},
                   "blockers": [], "summary": "ok",
                   "listening_note": "audio listening unavailable; not a full musical judgement"},
        "mechanical_pass": True,
        "run": {"id": 11, "status": "completed", "conclusion": "success",
                "head_branch": "main", "event": "issues",
                "path": ".github/workflows/studio-production.yml"},
        "artifact": {"workflow_run_id": 11, "expired": False,
                     "name": f"studio-child-3-pr-7-{HEAD}-run-11-a1"},
        "manifest_ok": True, "bundle_ok": True, "receipt_ok": True,
    }
    ctx.update(over)
    return ctx


def test_release_success_exact_head():
    # COMMENT after APPROVED must not erase the approval.
    assert gating.evaluate_release_gate(_release_ctx())["ok"]


@pytest.mark.parametrize("over,frag", [
    ({"reviews": []}, "approval"),
    ({"reviews": [{"user": "intruder", "state": "APPROVED", "commit_id": HEAD}]}, "owner"),
    ({"reviews": [{"user": "YusukeIwaki", "state": "APPROVED", "commit_id": "b" * 40}]}, "head SHA"),
    ({"reviews": [{"user": "YusukeIwaki", "state": "APPROVED", "commit_id": HEAD},
                  {"user": "YusukeIwaki", "state": "CHANGES_REQUESTED", "commit_id": HEAD}]}, "CHANGES_REQUESTED"),
    ({"reviews": [{"user": "YusukeIwaki", "state": "APPROVED", "commit_id": HEAD},
                  {"user": "YusukeIwaki", "state": "DISMISSED", "commit_id": HEAD}]}, "DISMISSED"),
    ({"review": {"verdict": "fail", "reviewed_head_sha": HEAD, "checks": {},
                  "blockers": ["x"], "summary": "no",
                  "listening_note": "audio listening unavailable; not a full musical judgement"}}, "review"),
    ({"mechanical_pass": False}, "mechanical"),
    ({"run": {"id": 11, "status": "completed", "conclusion": "failure",
              "head_branch": "main", "event": "issues",
              "path": ".github/workflows/studio-production.yml"}}, "completed/success"),
    ({"artifact": {"workflow_run_id": 99, "expired": False,
                   "name": f"studio-child-3-pr-7-{HEAD}-run-11-a1"}}, "belong"),
    ({"artifact": {"workflow_run_id": 11, "expired": True,
                   "name": f"studio-child-3-pr-7-{HEAD}-run-11-a1"}}, "expired"),
    ({"manifest_ok": False}, "manifest"),
    ({"bundle_ok": False}, "bundle"),
    ({"pr": {"number": 7, "merged": False, "merged_by": "YusukeIwaki",
             "base": "main", "head_sha": HEAD}}, "merged"),
])
def test_release_rejections(over, frag):
    r = gating.evaluate_release_gate(_release_ctx(**over))
    assert not r["ok"] and any(frag in f for f in r["failures"]), r


def test_parent_close_requires_all_terminal():
    done = {"released": True, "state": "closed", "state_reason": "completed",
            "closed_by_owner": True}
    dismissed = {"released": False, "state": "closed",
                 "state_reason": "not_planned", "closed_by_owner": True}
    arbitrary = {"released": False, "state": "closed",
                 "state_reason": "completed", "closed_by_owner": False}
    open_child = {"released": False, "state": "open", "state_reason": None,
                  "closed_by_owner": False}
    assert gating.should_close_parent([done, dismissed])
    assert not gating.should_close_parent([done, open_child])
    assert not gating.should_close_parent([done, arbitrary])
    assert not gating.should_close_parent([])


# --- artifact zip -------------------------------------------------------------
def _zip_bytes(files: dict[str, bytes], extra=None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
        for name, data, attr in (extra or []):
            info = zipfile.ZipInfo(name)
            info.external_attr = attr
            z.writestr(info, data)
    return buf.getvalue()


def test_zip_validation_and_hashes():
    files = {"video.mp4": b"x", "plan.json": b"{}"}
    manifest = artifacts.build_hash_manifest(files)
    assert artifacts.verify_hash_manifest(manifest, files)["ok"]
    tampered = dict(files, **{"plan.json": b"{bad}"})
    assert not artifacts.verify_hash_manifest(manifest, tampered)["ok"]
    edited = dict(manifest, **{"extra.json": "0" * 64})
    assert not artifacts.verify_hash_manifest(edited, files)["ok"]
    ok = artifacts.read_and_validate_zip(_zip_bytes(files), expected=set(files))
    assert ok["ok"]
    trav = artifacts.read_and_validate_zip(
        _zip_bytes({"../evil.txt": b"x"}), expected=None)
    assert not trav["ok"]
    link_attr = (stat.S_IFLNK | 0o777) << 16
    sym = artifacts.read_and_validate_zip(
        _zip_bytes(files, extra=[("link", b"/etc/passwd", link_attr)]),
        expected=None)
    assert not sym["ok"] and any("symlink" in f for f in sym["failures"])
    unexp = artifacts.read_and_validate_zip(
        _zip_bytes({"surprise.exe": b"x"}), expected=set(files))
    assert not unexp["ok"]


# --- fake lifecycle dry run ---------------------------------------------------
def test_fake_lifecycle_idempotent():
    gh = FakeGitHub()
    fp = fingerprint.canonical_fingerprint(source_url=GOOD_URL,
                                           source_sha256="c" * 64)
    cands = [{"id": "cand-01", "start_sample": 0, "end_sample": 1984500,
              "n_frames": 2700, "sample_rate": 22050, "fps": [30, 1],
              "duration_sec": 90.0, "score": 1.0,
              "endpoint_evidence": {}, "scenes": [{"id": "scene01"}]}]
    parent = gh.create_issue(title="source", body="src")
    first = lifecycle.ensure_children(gh, parent=parent["number"],
                                      selected=cands, fp=fp)
    n_issues = len(gh.issues)
    second = lifecycle.ensure_children(gh, parent=parent["number"],
                                       selected=cands, fp=fp)
    assert [c["number"] for c in first] == [c["number"] for c in second]
    assert len(gh.issues) == n_issues  # reused, no duplicate child
    assert first[0]["number"] in gh.issues[parent["number"]]["sub_issues"]
    pr = gh.create_or_reuse_pr(branch="studio/issue-2", child=2,
                               head_sha=HEAD, artifact_key="k1")
    pr2 = gh.create_or_reuse_pr(branch="studio/issue-2", child=2,
                                head_sha=HEAD, artifact_key="k1")
    assert pr["number"] == pr2["number"]
    body = gh.post_review_comment(pr["number"], passed=True)
    assert body == "@YusukeIwaki please check this"
    rel = gh.publish_release(tag="studio-pr-7", pr=7, assets={"video.mp4": "h"})
    assert rel["tag"] == "studio-pr-7"
    with pytest.raises(ValueError):
        gh.publish_release(tag="studio-pr-7", pr=7, assets={"video.mp4": "OTHER"})


def test_palette_steers_render_frame():
    """Two storyboards -> measurably different pixels (renderer consumes them)."""
    from p0 import plan as P
    from p0 import render as R
    from p0.timebase import Timebase
    tb = Timebase(22050, 30, 1)
    n = 30
    clip = {"start_sample": 0, "end_sample": tb.clip_end_sample(0, n),
            "n_frames": n}
    scenes = [{"id": "scene01", "start_sample": 0, "end_sample": clip["end_sample"],
               "start_frame": 0, "end_frame": n, "duration_sec": 1.0}]
    import soundfile as sf  # noqa: F401  (ensures p0 deps present)
    from p0 import features as F
    y = np.zeros(22050)
    feat = F.compute_features(y, 22050)
    ctrl = P.build_controls("structured", n, tb, clip, scenes, [], feat, 7)
    lay = R._layouts(7, 180, 320)
    sb_a = storyboard.default_storyboard(scenes)
    sb_b = storyboard.default_storyboard(scenes, palettes=[{
        "sky_top": [200, 30, 30], "sky_bot": [240, 120, 40],
        "far": [150, 60, 40], "near": [90, 30, 20],
        "moon": [255, 240, 220], "glow": [255, 160, 90]}])
    fa = R.draw_frame(5, ctrl, "structured", 180, 320, 7, lay,
                      scene_palettes=[s["palette"] for s in sb_a["scenes"]],
                      scene_params=[s["params"] for s in sb_a["scenes"]])
    fb = R.draw_frame(5, ctrl, "structured", 180, 320, 7, lay,
                      scene_palettes=[s["palette"] for s in sb_b["scenes"]],
                      scene_params=[s["params"] for s in sb_b["scenes"]])
    assert np.abs(fa.astype(int) - fb.astype(int)).mean() > 20

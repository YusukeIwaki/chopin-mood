"""Real-controller lifecycle tests over FakeTransport (GitHub-shaped JSON).

These run the SAME studio.controller functions the scripts/workflows
call - no disconnected fake workflow. Offline: no network, no models.
"""
import hashlib
import io
import json
import zipfile

import numpy as np
import pytest
from release_support import complete_release
import soundfile as sf

from studio import (artifacts, candidates, config, controller, fingerprint,
                    gating, validate)
from studio.github_client import FakeTransport

HEAD_PLACEHOLDER = "unused"
GOOD_URL = "https://github.com/user-attachments/files/123/rec.bin"
OWNER = config.OWNER
BOT = "github-actions[bot]"


def _cand(cid, s0, sr=22050, dur=90):
    nf = int(dur * 30)
    end = candidates.frame_boundary_sample(s0, nf, sr, 30, 1)
    scenes = []
    third = nf // 3
    for k in range(3):
        scenes.append({"id": f"scene{k+1:02d}",
                       "start_frame": k * third,
                       "end_frame": (k + 1) * third if k < 2 else nf})
    return {"id": cid, "start_sample": s0, "end_sample": end,
            "n_frames": nf, "sample_rate": sr, "fps": [30, 1],
            "duration_sec": dur, "score": 1.0,
            "endpoint_evidence": {"start": {}, "end": {}}, "scenes": scenes}


def _brief(cand):
    return {"world": "night station world", "character": "a small traveler",
            "narrative_arc": "departure to arrival",
            "creative_interpretation": "late period as healing (direction, not fact)",
            "scene_guidance": [
                {"scene_id": s["id"], "direction": f"guidance {s['id']}",
                 "composition": "wide layered hills", "palette_intent": "deep blue"}
                for s in cand["scenes"]],
            "renderer_constraints": "procedural palettes only",
            "evidence_notes": "DSP endpoints only", "uncertainty": "no listening"}


def _planner(cands):
    return {"selected_candidate_ids": [c["id"] for c in cands],
            "rationale": "diverse windows",
            "briefs": {c["id"]: _brief(c) for c in cands}}


CREATIVE = {"piece": "Owner Piece", "period": "late",
            "art_notes": "first line\nsecond line\nthird line"}


@pytest.fixture()
def world():
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    parent = gh.seed_issue(title="source", body="recording")
    cands = [_cand("cand-01", 0), _cand("cand-02", 22050 * 300)]
    fp = fingerprint.canonical_fingerprint(
        source_url=GOOD_URL, source_sha256="c" * 64, piece=CREATIVE["piece"],
        period="late", art_notes=CREATIVE["art_notes"])
    r = controller.ensure_children_controller(
        gh, parent=parent["number"], candidates=cands,
        planner_output=_planner(cands), creative=CREATIVE, fp=fp)
    assert r["ok"], r
    return {"gh": gh, "parent": parent, "cands": cands, "fp": fp, "plan": r}


VIDEO_BYTES = b"fake-mp4-bytes"
JPG_BYTES = b"fake-jpg"
ENGINE_SHA = "e" * 40


def _timeline_html(cand):
    return artifacts.build_timeline_html(
        {"candidate_id": cand["id"], "clip": {"n_frames": 10},
         "scenes": [], "accents": []}).encode()


def _media_bytes(cand):
    return {"video.mp4": VIDEO_BYTES, "contact-sheet.jpg": JPG_BYTES,
            "timeline.html": _timeline_html(cand)}


def _media_sha256(cand):
    return {n: hashlib.sha256(b).hexdigest() for n, b in _media_bytes(cand).items()}


def _production_files(child, parent, cand, fp):
    brief = {"schema_version": "1.0", "parent_issue": parent,
             "child_issue": child, "candidate_id": cand["id"],
             "fingerprint": fp, "candidate": cand, "piece": CREATIVE["piece"],
             "period": "late", "period_direction": "healing",
             "art_notes": CREATIVE["art_notes"], "creative_brief": _brief(cand),
             "source_hashes": {"attachment_sha256": "a" * 64,
                               "decoded_wav_sha256": "b" * 64}}
    prov = {"schema_version": "1.0", "candidate_id": cand["id"],
            "child_issue": child, "parent_issue": parent,
            "source_hashes": {"attachment_sha256": "a" * 64,
                              "decoded_wav_sha256": "b" * 64},
            "planning_fingerprint": fp,
            "media_sha256": _media_sha256(cand)}
    files = {
        "brief.json": json.dumps(brief, indent=2).encode(),
        "storyboard.json": json.dumps({"schema_version": "1.0", "scenes": []}).encode(),
        "plan.json": json.dumps({"clip": {"n_frames": cand["n_frames"]}}).encode(),
        "qa.json": json.dumps({"pass": True, "checks": []}).encode(),
        "provenance.json": json.dumps(prov, indent=2).encode(),
        "timeline.json": json.dumps({"candidate_id": cand["id"], "scenes": []}).encode(),
    }
    return files, brief


def _build_artifact(gh, *, pr, child, parent, cand, fp, head, prod_map,
                    run_attempt=1, engine_sha=ENGINE_SHA, receipt_overrides=None,
                    media_overrides=None):
    receipt = artifacts.build_receipt(
        pr=pr, child=child, parent=parent, head_sha=head, run_id=11,
        run_attempt=run_attempt, engine_sha=engine_sha,
        source_hashes={"attachment_sha256": "a" * 64,
                       "decoded_wav_sha256": "b" * 64},
        planning_fingerprint=fp, production_files=prod_map)
    if receipt_overrides:
        receipt.update(receipt_overrides)
    prod_files, brief_obj = _production_files(child, parent, cand, fp)
    media = _media_bytes(cand)
    if media_overrides:
        media.update(media_overrides)
    files = {
        "video.mp4": media["video.mp4"],
        "plan.json": prod_files["plan.json"],
        "qa.json": prod_files["qa.json"],
        "storyboard.json": prod_files["storyboard.json"],
        "brief.json": prod_files["brief.json"],
        "provenance.json": prod_files["provenance.json"],
        "timeline.json": prod_files["timeline.json"],
        "timeline.html": media["timeline.html"],
        "contact-sheet.jpg": media["contact-sheet.jpg"],
        "review.json": json.dumps({
            "verdict": "pass", "reviewed_head_sha": head,
            "checks": {"mechanical_qa_reviewed": True, "brief_adherence": True,
                       "timing_integrity": True, "aesthetic_approval": True},
            "blockers": [], "summary": "ok",
            "listening_note": gating.LISTENING_NOTE}).encode(),
        "receipt.json": json.dumps(receipt, indent=2).encode(),
    }
    files["hashes.json"] = json.dumps(artifacts.build_hash_manifest(files), indent=2).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in sorted(files):
            z.writestr(name, files[name])
    gh.runs[11] = {"id": 11, "status": "completed", "conclusion": "success",
                   "head_branch": "main", "event": "issues",
                   "path": config.WORKFLOW_PATH, "workflow_id": gh.workflow_id,
                   "head_sha": engine_sha, "run_attempt": run_attempt,
                   "head_repository": {"full_name": gh.repo},
                   "repository": {"full_name": gh.repo}}
    gh.artifacts[42] = {"id": 42, "expired": False,
                        "workflow_run": {"id": 11, "head_sha": engine_sha,
                                         "head_branch": "main"},
                        "name": f"studio-child-{child}-pr-{pr}-{head}-run-11-a{run_attempt}"}
    gh.artifact_bytes[42] = buf.getvalue()
    return files


def _commit_and_artifact(world, idx=0):
    gh, parent = world["gh"], world["parent"]
    item = world["plan"]["matrix"][idx]
    child, cand = item["child_issue"], item["candidate"]
    files, _brief_obj = _production_files(child, parent["number"], cand, world["fp"])
    commit = controller.commit_bundle_controller(
        gh, child=child, files=files, repo_owner="YusukeIwaki")
    assert commit["ok"], commit
    art = _build_artifact(gh, pr=commit["pr_number"], child=child,
                          parent=parent["number"], cand=cand, fp=world["fp"],
                          head=commit["head_sha"],
                          prod_map=commit["blob_sha256"])
    return {"commit": commit, "child": child, "cand": cand, "art_files": art}


def _merge_as_owner(gh, pr, head):
    pull = gh.pulls[pr]
    head_commit = gh.commits[pull["head"]["sha"]]
    merge_sha = hashlib.sha1(f"merge:{head}".encode()).hexdigest()
    gh.commits[merge_sha] = {"sha": merge_sha, "tree_sha": head_commit["tree_sha"],
                             "parents": [pull["head"]["sha"]],
                             "author_login": OWNER}
    pull.update({"state": "closed", "merged": True, "merged_at": "2026-10-20T00:00:00Z",
                 "merged_by": {"login": OWNER}, "merge_commit_sha": merge_sha})
    gh.issues[pr].update({"state": "closed"})
    return merge_sha


REVIEW_PASS = lambda head: {  # noqa: E731
    "verdict": "pass", "reviewed_head_sha": head,
    "checks": {"mechanical_qa_reviewed": True, "brief_adherence": True,
               "timing_integrity": True, "aesthetic_approval": True},
    "blockers": [], "summary": "ok", "listening_note": gating.LISTENING_NOTE}


# --- full lifecycle ---------------------------------------------------------
def test_full_lifecycle_plan_children_commit_notify_merge_release_parent(world):
    gh = world["gh"]
    assert len(world["plan"]["matrix"]) == 2
    built = _commit_and_artifact(world, 0)
    commit, child = built["commit"], built["child"]
    pr = commit["pr_number"]
    # Child issue carries the planner brief verbatim + multi-line notes.
    body = gh.issues[child]["body"]
    assert "night station world" in body and "second line" in body
    # Notify: receipt + exact ping once + assignee; retry adds nothing.
    r1 = controller.publish_review_controller(
        gh, pr_number=pr, child=child, head_sha=commit["head_sha"], run_id=11,
        run_attempt=1, artifact_id=42, artifact_url="https://example.invalid/a",
        review=REVIEW_PASS(commit["head_sha"]), mechanical_pass=True)
    assert r1["ok"], r1
    r2 = controller.publish_review_controller(
        gh, pr_number=pr, child=child, head_sha=commit["head_sha"], run_id=11,
        run_attempt=1, artifact_id=42, artifact_url="https://example.invalid/a",
        review=REVIEW_PASS(commit["head_sha"]), mechanical_pass=True)
    assert r2["ok"] and all(r2["ensured"].values()), r2
    pings = [r for r in gh.reviews[pr] if r["body"] == controller.EXACT_PING]
    assert len(pings) == 1 and pings[0]["commit_id"] == commit["head_sha"]
    assert {"login": OWNER} in gh.issues[pr]["assignees"]
    _merge_as_owner(gh, pr, commit["head_sha"])
    rel = complete_release(controller.release_controller, gh, pr_number=pr)
    assert rel["ok"], rel
    assert rel["parent_closed"] is False  # sibling still open
    assert gh.releases and not list(gh.releases.values())[0]["draft"]
    assert gh.issues[child]["state"] == "closed"
    # Dismiss sibling as owner not_planned, rerun: parent closes, release untouched.
    sib = world["plan"]["matrix"][1]["child_issue"]
    gh.issues[sib].update({"state": "closed", "state_reason": "not_planned",
                           "closed_by": {"login": OWNER}})
    rel2 = complete_release(controller.release_controller, gh, pr_number=pr)
    assert rel2["ok"] and rel2["parent_closed"] is True, rel2
    assert gh.issues[world["parent"]["number"]]["state"] == "closed"


def test_planning_retry_reuses_original_briefs_and_rejects_edit(world):
    gh = world["gh"]
    changed = _planner(world["cands"])
    changed["briefs"]["cand-01"] = dict(changed["briefs"]["cand-01"], world="REPLACED")
    r = controller.ensure_children_controller(
        gh, parent=world["parent"]["number"], candidates=world["cands"],
        planner_output=changed, creative=CREATIVE, fp=world["fp"])
    assert r["ok"] and r["reused_plan"] is True
    assert r["briefs"]["cand-01"]["world"] == "night station world"
    bad = controller.ensure_children_controller(
        gh, parent=world["parent"]["number"], candidates=world["cands"],
        planner_output=_planner(world["cands"]), creative=CREATIVE, fp="0" * 64)
    assert not bad["ok"] and bad.get("action") == "fail_new_issue"


def test_publish_review_partial_failure_recovers_independently(world):
    gh = world["gh"]
    built = _commit_and_artifact(world, 0)
    commit, child, pr = built["commit"], built["child"], built["commit"]["pr_number"]
    # Simulate partial failure: bot receipt comment exists, nothing else.
    # A user-forged receipt must NOT count.
    gh.comments.setdefault(pr, []).append(
        {"id": 1, "body": fingerprint.receipt_marker(run_id=11, artifact_id=42, pr=pr,
         head_sha=commit["head_sha"], child=child) + " Key: " +
         f"pr-{pr}:{commit['head_sha']}:42", "user": {"login": "forger"}})
    r = controller.publish_review_controller(
        gh, pr_number=pr, child=child, head_sha=commit["head_sha"], run_id=11,
        run_attempt=1, artifact_id=42, artifact_url="",
        review=REVIEW_PASS(commit["head_sha"]), mechanical_pass=True)
    assert r["ok"] and r["ensured"]["receipt_comment"] is False, r
    assert [x for x in gh.reviews.get(pr, []) if x["body"] == controller.EXACT_PING]
    assert {"login": OWNER} in gh.issues[pr]["assignees"]
    # Stale head is a hard failure.
    stale = controller.publish_review_controller(
        gh, pr_number=pr, child=child, head_sha="0" * 40, run_id=11,
        run_attempt=1, artifact_id=42, artifact_url="",
        review=REVIEW_PASS("0" * 40), mechanical_pass=True)
    assert not stale["ok"]


def test_commit_unchanged_tree_skips_and_human_guard(world):
    gh = world["gh"]
    item = world["plan"]["matrix"][0]
    files, _ = _production_files(item["child_issue"], world["parent"]["number"],
                                 item["candidate"], world["fp"])
    c1 = controller.commit_bundle_controller(gh, child=item["child_issue"],
                                             files=files, repo_owner="YusukeIwaki")
    c2 = controller.commit_bundle_controller(gh, child=item["child_issue"],
                                             files=files, repo_owner="YusukeIwaki")
    assert c1["ok"] and c2["ok"] and c2["head_sha"] == c1["head_sha"]
    assert c2["tree_unchanged"] is True
    # Human-authored commit on the branch blocks further mutation.
    head_commit = gh.commits[c1["head_sha"]]
    head_commit["author_login"] = "human-editor"
    changed = dict(files, **{"plan.json": b"{}"})
    blocked = controller.commit_bundle_controller(
        gh, child=item["child_issue"], files=changed, repo_owner="YusukeIwaki")
    assert not blocked["ok"]


def test_release_rejects_tampered_artifact_after_owner_merge(world):
    gh = world["gh"]
    built = _commit_and_artifact(world, 0)
    commit, pr = built["commit"], built["commit"]["pr_number"]
    _merge_as_owner(gh, pr, commit["head_sha"])
    # Tamper one artifact byte (plan.json) and rebuild zip.
    files = dict(built["art_files"])
    files["plan.json"] = b'{"tampered": true}'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in sorted(files):
            z.writestr(n, files[n])
    gh.artifact_bytes[42] = buf.getvalue()
    rel = complete_release(controller.release_controller, gh, pr_number=pr)
    assert not rel["ok"]


def test_release_existing_asset_mismatch_never_overwrites(world):
    gh = world["gh"]
    built = _commit_and_artifact(world, 0)
    commit, pr = built["commit"], built["commit"]["pr_number"]
    controller.publish_review_controller(
        gh, pr_number=pr, child=built["child"], head_sha=commit["head_sha"],
        run_id=11, run_attempt=1, artifact_id=42, artifact_url="",
        review=REVIEW_PASS(commit["head_sha"]), mechanical_pass=True)
    _merge_as_owner(gh, pr, commit["head_sha"])
    assert complete_release(controller.release_controller, gh, pr_number=pr)["ok"]
    # Corrupt the stored release asset; a rerun must fail, not overwrite.
    rel = list(gh.releases.values())[0]
    rel["assets"][0]["digest"] = "sha256:" + "0" * 64
    again = complete_release(controller.release_controller, gh, pr_number=pr)
    assert not again["ok"] and "mismatch" in " ".join(again["failures"])


# --- negative gates -----------------------------------------------------------
def test_redirect_allowlist_negatives():
    good = GOOD_URL
    cdn = "https://objects.githubusercontent.com/github-production-release-asset-2e65be/1"
    assert validate.validate_redirect_chain([good, cdn])["ok"]
    for bad in ("https://evil.example/file", "https://169.254.169.254/latest/meta-data/",
                "https://10.0.0.5/x", "https://github.com:8443/x",
                "http://objects.githubusercontent.com/x",
                "https://user:pass@objects.githubusercontent.com/x"):
        assert not validate.validate_redirect_chain([good, bad])["ok"], bad


def test_review_gate_requires_aesthetic_and_head():
    base = REVIEW_PASS("a" * 40)
    assert gating.evaluate_review_gate(base, mechanical_pass=True,
                                       expected_head_sha="a" * 40)["ok"]
    no_aes = dict(base, checks=dict(base["checks"], aesthetic_approval=False))
    assert not gating.evaluate_review_gate(no_aes, mechanical_pass=True,
                                           expected_head_sha="a" * 40)["ok"]
    wrong_head = dict(base, reviewed_head_sha="b" * 40)
    assert not gating.evaluate_review_gate(wrong_head, mechanical_pass=True,
                                           expected_head_sha="a" * 40)["ok"]


def test_planner_brief_validation(world):
    cands = world["cands"]
    out = _planner(cands)
    out["briefs"]["cand-01"]["scene_guidance"] = out["briefs"]["cand-01"]["scene_guidance"][:1]
    assert not candidates.validate_planner_output(out, cands)["ok"]
    out2 = _planner(cands)
    out2["briefs"]["cand-01"]["start_sample"] = 0
    assert not candidates.validate_planner_output(out2, cands)["ok"]
    out3 = _planner(cands)
    del out3["briefs"]["cand-02"]
    assert not candidates.validate_planner_output(out3, cands)["ok"]


def test_decode_preserves_24bit_stereo(tmp_path):
    from studio import decode as dec
    sr = 22050
    y = (np.sin(2 * np.pi * 220 * np.arange(sr) / sr) * 0.4).astype(np.float64)
    stereo = np.column_stack([y, y * 0.5])
    src = tmp_path / "src24.wav"
    sf.write(str(src), stereo, sr, subtype="PCM_24")
    dst = tmp_path / "decoded.wav"
    meta = dec.decode_lossless(src, dst)
    info = sf.info(str(dst))
    assert info.subtype == "PCM_24" and info.channels == 2, meta
    # Native-subtype rewrite may be byte-identical; hashes are recorded separately.
    raw, _ = sf.read(str(src), dtype="float64", always_2d=True)
    out, _ = sf.read(str(dst), dtype="float64", always_2d=True)
    assert np.allclose(raw, out, atol=1e-6)


def test_qa_motion_nonmultiple_width():
    from p0 import qa as Q
    frames = [np.random.RandomState(i).rand(101, 103, 3) * 255 for i in range(3)]
    diffs = Q._motion_frames(frames)
    assert len(diffs) == 2 and all(d >= 0 for d in diffs)


def test_decode_compressed_high_precision(tmp_path):
    import subprocess
    from studio import decode as dec
    sr = 22050
    y = (np.sin(2 * np.pi * 220 * np.arange(sr) / sr) * 0.4).astype(np.float64)
    wav = tmp_path / "tone.wav"
    sf.write(str(wav), y, sr, subtype="PCM_16")
    mp3 = tmp_path / "tone.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(wav),
                    "-c:a", "libmp3lame", str(mp3)], check=True)
    dst = tmp_path / "decoded.wav"
    meta = dec.decode_lossless(mp3, dst)
    info = sf.info(str(dst))
    assert meta["mode"] == "compressed_pcm_f32le" and info.channels == 1
    assert dec.sha256_file(mp3) != dec.sha256_file(dst)

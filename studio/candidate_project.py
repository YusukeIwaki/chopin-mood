"""Data-only candidate contract for the direct-Issue production workflow.

Local review records are auditable declarations, not proof of a model identity.
Actions independently reproduces and reviews the committed inputs. This module
uses only the standard library so intake can run before media dependencies.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from p0.timebase import Timebase
from . import config, creative, fingerprint, storyboard, validate

INPUT_NAMES = ("project.json", "brief.json", "storyboard.json", "editorial.json")
CREATIVE_INPUT_NAMES = INPUT_NAMES[:3]
MEDIA_NAMES = ("video.mp4", "contact-sheet.jpg", "timeline.html")
QA_CHECK_NAMES = frozenset("studio:" + name for name in (
    "stream_kinds", "frame_count", "video_stream", "audio_stream", "audio_sync",
    "crop_pcm_exact", "no_black_frames", "content_changes", "scene_invariants",
    "input_hash_unchanged", "rendered_frame_spotcheck"))
MAX_REPAIR_ROUNDS = 2


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 8000


def _digest(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{" + str(length) + "}", value)


def canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def candidate_dir(issue: int, candidate: str) -> Path:
    _require(_integer(issue, 1), "source issue must be a positive integer")
    _require(isinstance(candidate, str) and re.fullmatch(r"cand-0[123]", candidate),
             "candidate ID must be cand-01, cand-02 or cand-03 (maximum three)")
    return Path("studio/projects") / f"issue-{issue}" / "candidates" / candidate


def _hash_map(value, names, label):
    _require(isinstance(value, dict) and set(value) == set(names)
             and all(_digest(v) for v in value.values()), f"invalid {label} hashes")


def validate_live_issue(gh, issue, source_url, brief=None, *, require_open=True):
    source = gh.get_issue(issue)
    _require(not source.get("pull_request") and source.get("user", {}).get("login") == config.OWNER,
             "source issue must be owner-authored")
    _require(not require_open or source.get("state") == "open", "source issue must be open")
    urls = [u for u in validate.extract_attachment_urls(source.get("body") or "")
            if validate.validate_attachment_url(u)["ok"]]
    _require(urls == [source_url], "live source attachment changed or is not unique")
    fields = creative.parse_issue_body(source.get("body"))
    if brief is not None:
        _require(all((fields.get(k) or "") == (brief.get(k) or "")
                     for k in ("piece", "period", "art_notes")),
                 "live creative direction changed; update the candidate")
    return source


def validate_timing(candidate):
    """Reject invented, inconsistent or non-integer timing before rendering."""
    _require(isinstance(candidate, dict), "candidate must be an object")
    for key in ("start_sample", "end_sample", "sample_rate", "n_frames"):
        _require(_integer(candidate.get(key), 0 if key == "start_sample" else 1),
                 f"candidate {key} must be an integer")
    _require(candidate.get("fps") == [config.FPS_NUM, config.FPS_DEN], "unsupported fps")
    sr, n = candidate["sample_rate"], candidate["n_frames"]
    _require(sr <= config.MAX_SAMPLE_RATE, "sample rate exceeds limit")
    tb = Timebase(sr, *candidate["fps"])
    start = candidate["start_sample"]
    _require(candidate["end_sample"] == tb.clip_end_sample(start, n),
             "candidate sample/frame endpoint mismatch")
    _require(config.CANDIDATE_MIN_SEC <= n / float(tb.fps) <= config.CANDIDATE_MAX_SEC,
             "candidate duration must be 60–120 seconds")
    _require(candidate["end_sample"] <= config.MAX_DURATION_SEC * sr,
             "candidate exceeds source duration limit")
    scenes = candidate.get("scenes")
    _require(isinstance(scenes, list) and 1 <= len(scenes) <= 6, "invalid scene list")
    cursor, ids = 0, set()
    for scene in scenes:
        _require(isinstance(scene, dict) and _text(scene.get("id")), "invalid scene identity")
        _require(scene["id"] not in ids, "duplicate scene identity")
        ids.add(scene["id"])
        a, b = scene.get("start_frame"), scene.get("end_frame")
        _require(_integer(a) and _integer(b, 1) and a == cursor and b <= n
                 and b - a >= config.MIN_SCENE_SEC * float(tb.fps),
                 "scene coverage/order/minimum duration mismatch")
        _require(_integer(scene.get("start_sample")) and _integer(scene.get("end_sample"), 1)
                 and scene["start_sample"] == tb.frame_boundary_sample(start, a)
                 and scene["end_sample"] == tb.frame_boundary_sample(start, b),
                 "scene sample/frame mismatch")
        cursor = b
    _require(cursor == n, "scenes do not cover candidate")
    accents = candidate.get("accents")
    _require(isinstance(accents, list), "candidate accents must be a list")
    previous = -1
    for accent in accents:
        _require(isinstance(accent, dict), "invalid accent")
        sample, frame = accent.get("source_sample"), accent.get("frame")
        _require(_integer(sample) and start <= sample < candidate["end_sample"]
                 and sample >= previous, "accent sample/order invalid")
        _require(_integer(frame) and frame == tb.sample_to_frame(start, sample),
                 "accent sample/frame mismatch")
        previous = sample


def evidence_digest(editorial):
    return sha256_bytes(canonical_bytes({k: editorial[k] for k in
        ("input_sha256", "media_sha256", "qa_sha256")}))


def editorial_template(input_sha256, media_dir, *, creator_id, round_number=0):
    """Snapshot real local evidence; deliberately leaves reviews unfinished."""
    directory = Path(media_dir)
    qa_bytes = (directory / "qa.json").read_bytes()
    record = {"schema_version": "2.0", "round": round_number, "creator_id": creator_id,
              "input_sha256": {k: input_sha256[k] for k in CREATIVE_INPUT_NAMES},
              "media_sha256": {k: sha256_bytes((directory / k).read_bytes()) for k in MEDIA_NAMES},
              "qa_sha256": sha256_bytes(qa_bytes), "qa": json.loads(qa_bytes),
              "reviews": {}, "decision": {"editor_id": "", "verdict": "stop" if round_number == MAX_REPAIR_ROUNDS else "revise",
                                            "rationale": "Independent reviews pending",
                                            "resolutions": []}}
    binding = evidence_digest(record)
    for role in ("music", "visual"):
        record["reviews"][role] = {
            "reviewer_id": "", "evidence_sha256": binding, "verdict": "revise",
            "hard_blockers": ["Review not performed"], "concerns": [], "evidence": [],
            "listening": "unavailable" if role == "music" else "not_applicable",
            "limitations": ["Review not performed"]}
    return record


def validate_dsp_candidate(brief, analysis):
    """Compare with a fresh trusted DSP run; never accept hand-edited timings."""
    _require(isinstance(analysis, dict), "invalid DSP analysis")
    matches = [c for c in analysis.get("candidates", [])
               if isinstance(c, dict) and c.get("id") == brief.get("candidate_id")]
    _require(len(matches) == 1 and matches[0] == brief.get("candidate"),
             "candidate differs from fresh DSP analysis; manual timing edits are not allowed")
    _require(analysis.get("source_sha256") == brief["source_hashes"]["decoded_wav_sha256"],
             "DSP analysis source hash mismatch")


def validate_editorial(editorial, input_sha256, *, require_ready=True, media_dir=None):
    keys = {"schema_version", "round", "creator_id", "input_sha256", "media_sha256",
            "qa_sha256", "qa", "reviews", "decision"}
    _require(isinstance(editorial, dict) and set(editorial) == keys, "invalid editorial fields")
    _require(editorial["schema_version"] == "2.0", "invalid editorial schema")
    _require(_integer(editorial["round"]) and editorial["round"] <= MAX_REPAIR_ROUNDS,
             "maximum initial version plus two repair rounds")
    _require(_text(editorial["creator_id"]), "creator_id is required")
    _hash_map(editorial["input_sha256"], CREATIVE_INPUT_NAMES, "editorial input")
    _require(editorial["input_sha256"] == {k: input_sha256[k] for k in CREATIVE_INPUT_NAMES},
             "stale editorial input evidence")
    _hash_map(editorial["media_sha256"], MEDIA_NAMES, "local media")
    _require(_digest(editorial["qa_sha256"]), "QA hash is required")
    qa = editorial["qa"]
    _require(isinstance(qa, dict) and type(qa.get("pass")) is bool
             and isinstance(qa.get("checks"), list), "invalid local QA")
    checks = qa["checks"]
    names = [c.get("name") for c in checks if isinstance(c, dict)]
    _require(len(checks) == len(QA_CHECK_NAMES) and set(names) == QA_CHECK_NAMES
             and all(type(c.get("pass")) is bool for c in checks), "incomplete local QA checks")
    passed = all(c["pass"] for c in checks)
    _require(qa["pass"] == passed, "QA overall result disagrees with checks")
    if media_dir is not None:
        directory = Path(media_dir)
        for name, expected in {**editorial["media_sha256"], "qa.json": editorial["qa_sha256"]}.items():
            path = directory / name
            _require(path.is_file() and not path.is_symlink()
                     and sha256_bytes(path.read_bytes()) == expected, f"stale local media: {name}")
        _require(json.loads((directory / "qa.json").read_text()) == qa, "local QA record mismatch")
    ready_claimed = require_ready or (isinstance(editorial.get("decision"), dict)
                                     and editorial["decision"].get("verdict") == "ready")
    reviews = editorial["reviews"]
    _require(isinstance(reviews, dict) and set(reviews) == {"music", "visual"},
             "independent music and visual reviews required")
    binding, reviewers, concerns, hard = evidence_digest(editorial), set(), set(), []
    for role, review in reviews.items():
        _require(isinstance(review, dict) and set(review) == {"reviewer_id", "evidence_sha256",
                 "verdict", "hard_blockers", "concerns", "evidence", "listening", "limitations"},
                 f"invalid {role} review fields")
        _require(review["evidence_sha256"] == binding, f"stale {role} review")
        _require(review["verdict"] in ("pass", "revise"), "invalid review verdict")
        for key in ("hard_blockers", "evidence", "limitations"):
            _require(isinstance(review[key], list) and all(_text(v) for v in review[key]),
                     f"invalid {role} {key}")
        _require(review["listening"] in ("performed", "unavailable", "not_applicable"),
                 "invalid listening disclosure")
        if role == "music":
            _require(review["listening"] != "not_applicable", "music review must disclose listening")
            _require(review["listening"] != "unavailable" or bool(review["limitations"]),
                     "unavailable listening requires explicit limitation")
        _require(isinstance(review["concerns"], list), "review concerns must be a list")
        for concern in review["concerns"]:
            _require(isinstance(concern, dict) and set(concern) == {"id", "note"}
                     and _text(concern["id"]) and _text(concern["note"]), "invalid review concern")
            key = (role, concern["id"])
            _require(key not in concerns, "duplicate concern")
            concerns.add(key)
        hard.extend(review["hard_blockers"])
        if ready_claimed:
            reviewer = review["reviewer_id"]
            _require(_text(reviewer) and reviewer != editorial["creator_id"]
                     and reviewer not in reviewers, "independent reviewer identities required")
            reviewers.add(reviewer)
            _require(bool(review["evidence"]), f"{role} review needs observed evidence")
            _require(review["verdict"] != "revise" or review["concerns"] or review["hard_blockers"],
                     "revision verdict needs an actionable finding")
    decision = editorial["decision"]
    _require(isinstance(decision, dict) and set(decision) == {"editor_id", "verdict", "rationale", "resolutions"},
             "invalid editorial decision")
    _require(decision["verdict"] in ("ready", "revise", "stop") and _text(decision["rationale"]),
             "invalid final editorial decision")
    _require(isinstance(decision["resolutions"], list), "invalid editorial resolutions")
    resolved = set()
    for resolution in decision["resolutions"]:
        _require(isinstance(resolution, dict) and set(resolution) == {"role", "concern_id", "action", "reason"},
                 "invalid concern resolution")
        key = (resolution["role"], resolution["concern_id"])
        _require(key in concerns and key not in resolved and resolution["action"] in ("accept", "fixed")
                 and _text(resolution["reason"]), "unbound/duplicate concern resolution")
        resolved.add(key)
    _require(not (editorial["round"] == MAX_REPAIR_ROUNDS and decision["verdict"] == "revise"),
             "repair budget exhausted: choose ready or stop")
    if ready_claimed:
        _require(passed, "mechanical QA failure cannot be waived")
        _require(not hard, "hard blockers cannot be waived")
        _require(concerns <= resolved, "unresolved subjective concerns")
        _require(decision["verdict"] == "ready" and _text(decision["editor_id"]),
                 "editorial decision is not ready")
    return editorial


def validate_candidate_documents(documents, *, input_sha256=None, issue=None,
                                 candidate_id=None, gh=None, require_ready=True,
                                 media_dir=None, require_open=True):
    _require(isinstance(documents, dict) and set(documents) == set(INPUT_NAMES),
             "candidate requires exactly four JSON inputs")
    # API callers must provide hashes of the ORIGINAL bytes. Canonical hashes
    # are useful only for callers which themselves persisted canonical JSON.
    _require(input_sha256 is not None, "raw input hashes are required")
    _hash_map(input_sha256, INPUT_NAMES, "input")
    project, brief, sb, editorial = [documents[n] for n in INPUT_NAMES]
    _require(isinstance(project, dict) and set(project) == {
        "schema_version", "source_issue", "candidate_id", "source_url", "engine_sha"},
        "unexpected project metadata")
    _require(project["schema_version"] == "2.0", "invalid project schema")
    candidate_dir(project["source_issue"], project["candidate_id"])
    _require(issue is None or project["source_issue"] == issue, "source issue identity mismatch")
    _require(candidate_id is None or project["candidate_id"] == candidate_id, "candidate identity mismatch")
    _require(_digest(project["engine_sha"], 40), "engine commit SHA required")
    _require(validate.validate_attachment_url(project["source_url"])["ok"], "untrusted source URL")
    _require(isinstance(brief, dict) and brief.get("schema_version") == "2.0",
             "candidate brief schema must be 2.0")
    _require(brief.get("parent_issue") == project["source_issue"]
             and brief.get("child_issue") is None and brief.get("candidate_id") == project["candidate_id"],
             "brief issue/candidate binding mismatch")
    candidate = brief.get("candidate")
    validate_timing(candidate)
    _require(candidate.get("id") == project["candidate_id"], "DSP candidate identity mismatch")
    hashes = brief.get("source_hashes")
    _require(isinstance(hashes, dict) and all(_digest(hashes.get(k)) for k in
             ("attachment_sha256", "decoded_wav_sha256")), "source hashes required")
    expected = fingerprint.canonical_fingerprint(
        source_url=project["source_url"], source_sha256=hashes["decoded_wav_sha256"],
        piece=brief.get("piece"), period=brief.get("period"), art_notes=brief.get("art_notes"))
    _require(brief.get("fingerprint") == expected, "candidate fingerprint mismatch")
    checked = storyboard.validate_storyboard(sb, candidate["scenes"])
    _require(checked["ok"], "invalid storyboard: " + "; ".join(checked["failures"]))
    # Storyboard validation admits only registered bundles when one is used.
    # A deliberate procedural work still faces the same independent quality
    # gates; the workflow must not impose one painting or visual style forever.
    validate_editorial(editorial, input_sha256, require_ready=require_ready, media_dir=media_dir)
    if gh is not None:
        validate_live_issue(gh, project["source_issue"], project["source_url"], brief,
                            require_open=require_open)
    return {"project": project, "brief": brief, "candidate": candidate,
            "scenes": {"scenes": candidate["scenes"]}, "storyboard": sb,
            "editorial": editorial, "input_sha256": input_sha256}


def load_candidate(directory, *, issue=None, candidate_id=None, gh=None,
                   require_ready=True, media_dir=None, require_open=True):
    directory = Path(directory)
    documents, hashes = {}, {}
    for name in INPUT_NAMES:
        path = directory / name
        _require(not any(p.is_symlink() for p in (path, *path.parents)), "candidate paths cannot traverse symlinks")
        _require(path.is_file() and path.stat().st_size <= config.MAX_PRODUCTION_FILE_BYTES,
                 f"missing/oversized candidate input: {name}")
        raw = path.read_bytes()
        documents[name] = json.loads(raw, parse_constant=lambda x: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
        hashes[name] = sha256_bytes(raw)
    return validate_candidate_documents(documents, input_sha256=hashes, issue=issue,
        candidate_id=candidate_id, gh=gh, require_ready=require_ready,
        media_dir=media_dir, require_open=require_open)

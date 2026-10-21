"""Bounded repair loop tests: real loop/controller paths, fake agents.

No real models, network, credentials or personal recordings. The loop
under test is ``studio.repair.run_repair_loop`` (the same function the
production entrypoint drives); GitHub paths run over FakeTransport.
"""
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from studio import artifacts, config, controller, gating, repair, storyboard as sbmod
from studio.github_client import FakeTransport

ROOT = Path(__file__).resolve().parents[1]
BOT = "github-actions[bot]"
OWNER = config.OWNER
HEAD1 = "a" * 40
HEAD2 = "b" * 40
HEAD3 = "c" * 40
HEAD4 = "d" * 40
ENGINE = "e" * 40
SCENES = [{"id": "scene01"}, {"id": "scene02"}]


def make_review(head, verdict="pass", route=None, blockers=None,
                instructions=None, aesthetic=True):
    checks = {"mechanical_qa_reviewed": True, "brief_adherence": True,
              "timing_integrity": True, "aesthetic_approval": aesthetic}
    if verdict == "pass":
        return {"verdict": "pass", "reviewed_head_sha": head, "checks": checks,
                "blockers": [], "summary": "ok",
                "listening_note": gating.LISTENING_NOTE,
                "repair": {"route": "none", "instructions": []}}
    if route is None:
        route = "storyboard"
    if blockers is None:
        blockers = ["scene01 palette washes out the figure"]
    if instructions is None:
        instructions = ["scene01: deepen sky_top/near palette and raise "
                        "glow_scale; verify figure contrast in contact sheet"]
    checks = dict(checks, aesthetic_approval=False)
    return {"verdict": "fail", "reviewed_head_sha": head, "checks": checks,
            "blockers": blockers, "summary": "not yet",
            "listening_note": gating.LISTENING_NOTE,
            "repair": {"route": route, "instructions": instructions}}


def legacy_review(head):
    r = make_review(head)
    del r["repair"]
    return r


class FakeRunner:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, repair_index, context):
        self.calls.append((repair_index, context))
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            return outcome(repair_index, context)
        return outcome


def changed_storyboard(tag=1):
    sb = sbmod.default_storyboard(SCENES)
    sb["scenes"][0]["palette"]["sky_top"] = [10 + tag, 20, 30]
    sb["scenes"][0]["motif"] = f"repair motif {tag}"
    return sb


# --- schema / config / gating -------------------------------------------

def test_schema_requires_repair_and_config_matches():
    schema = json.loads((ROOT / ".github/studio/schemas/review.schema.json").read_text())
    assert "repair" in schema["required"]
    assert set(schema["required"]) == set(schema["properties"])
    rep = schema["properties"]["repair"]
    assert rep["additionalProperties"] is False
    assert set(rep["required"]) == set(rep["properties"]) == {"route", "instructions"}
    assert rep["properties"]["instructions"]["maxItems"] == 20
    assert rep["properties"]["instructions"]["items"]["maxLength"] == 1000
    cfg = json.loads((ROOT / ".github/studio/config.json").read_text())
    assert config.MAX_REPAIRS == 2 and repair.MAX_REPAIRS == 2
    assert config.REPAIR_AGENT_TIMEOUT_SEC == 900
    assert repair.AGENT_TIMEOUT_SEC == 900
    assert cfg["repair"] == {"max_repairs": 2, "max_attempts": 3,
                             "agent_timeout_sec": 900}


def test_gating_legacy_accepted_and_new_repair_strict():
    legacy = legacy_review(HEAD1)
    assert gating.evaluate_review_gate(
        legacy, mechanical_pass=True, expected_head_sha=HEAD1)["ok"]
    assert gating.validate_review_structure(legacy)["ok"]
    assert not gating.validate_review_structure(legacy, require_repair=True)["ok"]
    assert not gating.evaluate_review_gate(
        legacy, mechanical_pass=True, expected_head_sha=HEAD1,
        require_repair=True)["ok"]
    # Valid new pass/fail structures.
    assert gating.evaluate_review_gate(
        make_review(HEAD1), mechanical_pass=True, expected_head_sha=HEAD1)["ok"]
    fail = make_review(HEAD1, verdict="fail")
    assert gating.validate_review_structure(fail, require_repair=True)["ok"]
    assert not gating.evaluate_review_gate(
        fail, mechanical_pass=True, expected_head_sha=HEAD1)["ok"]


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(repair={"route": "storyboard", "instructions": ["x"]}),
    lambda r: r.update(repair={"route": "none", "instructions": ["x"]}),
    lambda r: r.update(repair={"route": "none", "instructions": [], "extra": 1}),
    lambda r: r.update(repair={"route": "bogus", "instructions": []}),
    lambda r: r.update(repair={"route": "none", "instructions": ["x" * 1001]}),
    lambda r: r.update(repair={"route": "none", "instructions": "x"}),
    lambda r: r.update(repair="storyboard"),
])
def test_gating_malformed_repair_on_pass_never_passes(mutate):
    r = make_review(HEAD1)
    mutate(r)
    assert not gating.validate_review_structure(r)["ok"]
    assert not gating.evaluate_review_gate(
        r, mechanical_pass=True, expected_head_sha=HEAD1)["ok"]


@pytest.mark.parametrize("repair_obj", [
    {"route": "none", "instructions": []},
    {"route": "storyboard", "instructions": []},
    {"route": "engine", "instructions": [""]},
    {"route": "storyboard", "instructions": ["ok"] * 21},
])
def test_gating_malformed_repair_on_fail_is_structural_failure(repair_obj):
    r = make_review(HEAD1, verdict="fail")
    r["repair"] = repair_obj
    assert not gating.validate_review_structure(r, require_repair=True)["ok"]


# --- loop -----------------------------------------------------------------

def test_loop_initial_pass_invokes_no_repair():
    runner = FakeRunner([])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1), initial_mechanical_pass=True,
        initial_head_sha=HEAD1, initial_pr_number=7, attempt_runner=runner,
        initial_storyboard=changed_storyboard(0))
    assert result["status"] == "passed" and result["attempt_count"] == 1
    assert result["final_head_sha"] == HEAD1 and runner.calls == []


def test_loop_engine_route_invokes_no_repair():
    runner = FakeRunner([])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail", route="engine",
                                   instructions=["add a new asset pipeline; test renders"]),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner)
    assert result["status"] == "needs_engine" and runner.calls == []
    assert result["attempt_count"] == 1


def test_loop_fail_to_pass_new_head_and_snapshot():
    snapshots = []
    new_sb = changed_storyboard(1)

    def outcome(idx, ctx):
        # Full prior feedback survives into the attempt context.
        assert ctx["previous_review"]["blockers"] == [
            "scene01 palette washes out the figure"]
        assert "glow_scale" in ctx["previous_review"]["repair"]["instructions"][0]
        return {"kind": "reviewed", "review": make_review(HEAD2),
                "head_sha": HEAD2, "pr_number": 7, "storyboard": new_sb,
                "media_sha256": {"video.mp4": "f" * 64}, "mechanical_pass": True,
                "bundle_dir": "fake-bundle-2"}

    runner = FakeRunner([outcome])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        snapshot_attempt=lambda n, o: snapshots.append((n, o)),
        initial_storyboard=changed_storyboard(0),
        initial_media_sha256={"video.mp4": "0" * 64})
    assert result["status"] == "passed" and result["attempt_count"] == 2
    assert result["final_head_sha"] == HEAD2 and len(runner.calls) == 1
    assert [n for n, _ in snapshots] == [2]


def test_loop_two_repairs_maximum():
    def mk(tag, head):
        return lambda idx, ctx: {
            "kind": "reviewed",
            "review": make_review(head, verdict="fail"),
            "head_sha": head, "storyboard": changed_storyboard(tag),
            "media_sha256": {"video.mp4": str(tag) * 64},
            "mechanical_pass": True}

    runner = FakeRunner([mk(1, HEAD2), mk(2, HEAD3), mk(3, HEAD4)])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        initial_storyboard=changed_storyboard(0),
        initial_media_sha256={"video.mp4": "0" * 64})
    assert result["status"] == "attempts_exhausted"
    assert result["attempt_count"] == 3 and len(runner.calls) == 2
    assert result["max_repairs"] == 2


def test_loop_second_review_engine_stops():
    runner = FakeRunner([lambda idx, ctx: {
        "kind": "reviewed",
        "review": make_review(HEAD2, verdict="fail", route="engine",
                              instructions=["needs new rendering capability; add tests"]),
        "head_sha": HEAD2, "storyboard": changed_storyboard(1),
        "media_sha256": {"video.mp4": "f" * 64}, "mechanical_pass": True}])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        initial_storyboard=changed_storyboard(0),
        initial_media_sha256={"video.mp4": "0" * 64})
    assert result["status"] == "needs_engine" and result["attempt_count"] == 2


@pytest.mark.parametrize("outcome,status", [
    ({"kind": "no_change"}, "no_change"),
    ({"kind": "no_visual_change"}, "no_visual_change"),
])
def test_loop_no_progress_kinds_stop(outcome, status):
    runner = FakeRunner([outcome, {"kind": "reviewed"}])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        initial_storyboard=changed_storyboard(0))
    assert result["status"] == status and result["attempt_count"] == 1
    assert len(runner.calls) == 1 and result["final_head_sha"] == HEAD1


def test_loop_identical_storyboard_and_media_are_no_progress():
    sb = changed_storyboard(0)
    snapshots = []
    runner = FakeRunner([{"kind": "reviewed", "review": make_review(HEAD2),
                           "head_sha": HEAD2, "storyboard": sb,
                           "media_sha256": {"video.mp4": "f" * 64},
                           "mechanical_pass": True}])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        snapshot_attempt=lambda n, o: snapshots.append(n),
        initial_storyboard=sb, initial_media_sha256={"video.mp4": "0" * 64})
    assert result["status"] == "no_change" and snapshots == []

    runner2 = FakeRunner([{"kind": "reviewed", "review": make_review(HEAD2),
                            "head_sha": HEAD2, "storyboard": changed_storyboard(1),
                            "media_sha256": {"video.mp4": "0" * 64},
                            "mechanical_pass": True}])
    result2 = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner2,
        initial_storyboard=changed_storyboard(0),
        initial_media_sha256={"video.mp4": "0" * 64})
    assert result2["status"] == "no_visual_change" and result2["attempt_count"] == 1


@pytest.mark.parametrize("initial_review,mech", [
    (legacy_review(HEAD1), True),
    (make_review(HEAD2), True),  # stale head vs HEAD1
    (make_review(HEAD1, verdict="fail"), False),
])
def test_loop_malformed_stale_mechanical_initial_raises(initial_review, mech):
    runner = FakeRunner([])
    with pytest.raises(repair.RepairInfrastructureError):
        repair.run_repair_loop(
            initial_review=initial_review, initial_mechanical_pass=mech,
            initial_head_sha=HEAD1, initial_pr_number=7, attempt_runner=runner)
    assert runner.calls == []


def test_loop_malformed_or_mechanical_attempt_raises_without_snapshot():
    snapshots = []
    bad = make_review(HEAD2)
    bad["repair"] = {"route": "storyboard", "instructions": ["x"]}  # pass+bogus route
    runner = FakeRunner([{"kind": "reviewed", "review": bad, "head_sha": HEAD2,
                           "storyboard": changed_storyboard(1),
                           "media_sha256": {"video.mp4": "f" * 64},
                           "mechanical_pass": True}])
    with pytest.raises(repair.RepairInfrastructureError):
        repair.run_repair_loop(
            initial_review=make_review(HEAD1, verdict="fail"),
            initial_mechanical_pass=True, initial_head_sha=HEAD1,
            initial_pr_number=7, attempt_runner=runner,
            snapshot_attempt=lambda n, o: snapshots.append(n),
            initial_storyboard=changed_storyboard(0),
            initial_media_sha256={"video.mp4": "0" * 64})
    assert snapshots == []

    runner2 = FakeRunner([{"kind": "reviewed", "review": make_review(HEAD2),
                            "head_sha": HEAD2, "storyboard": changed_storyboard(1),
                            "media_sha256": {"video.mp4": "f" * 64},
                            "mechanical_pass": False}])
    with pytest.raises(repair.RepairInfrastructureError):
        repair.run_repair_loop(
            initial_review=make_review(HEAD1, verdict="fail"),
            initial_mechanical_pass=True, initial_head_sha=HEAD1,
            initial_pr_number=7, attempt_runner=runner2,
            initial_storyboard=changed_storyboard(0),
            initial_media_sha256={"video.mp4": "0" * 64})


# --- invariants / workspace / agent invocation ----------------------------

def test_storyboard_plan_source_invariants():
    sb = sbmod.default_storyboard(SCENES)
    assert repair.validate_new_storyboard(sb, SCENES)["ok"]
    bad = sbmod.default_storyboard(SCENES)
    bad["scenes"] = list(reversed(bad["scenes"]))
    assert not repair.validate_new_storyboard(bad, SCENES)["ok"]
    plan = {"clip": {"start_sample": 0, "end_sample": 10, "n_frames": 5},
            "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": 5}],
            "accents": [{"frame": 2}]}
    assert repair.verify_plan_preserved(plan, json.loads(json.dumps(plan)))["ok"]
    changed = json.loads(json.dumps(plan))
    changed["accents"] = [{"frame": 3}]
    assert not repair.verify_plan_preserved(plan, changed)["ok"]
    brief = {"source_hashes": {"decoded_wav_sha256": "b" * 64}}
    assert repair.verify_source_against_brief("b" * 64, brief)["ok"]
    assert not repair.verify_source_against_brief("c" * 64, brief)["ok"]
    assert repair.storyboard_is_unchanged(sb, json.loads(json.dumps(sb)))


def test_workspace_is_fresh_allowlisted_and_prompt_carries_full_feedback(tmp_path):
    ws = tmp_path / "worker"
    ws.mkdir()
    (ws / "storyboard.json").write_text("{}")  # stale output must vanish
    (ws / "source.wav").write_bytes(b"audio")
    prior = make_review(HEAD1, verdict="fail",
                        blockers=["FULL-BLOCKER-TEXT"],
                        instructions=["FULL-INSTRUCTION-TEXT"])
    prompt = repair.build_muse_repair_prompt(
        "BASE PROMPT", brief={"x": 1}, scenes={"scenes": SCENES},
        prior_review=prior, previous_storyboard=changed_storyboard(0))
    assert "FULL-BLOCKER-TEXT" in prompt and "FULL-INSTRUCTION-TEXT" in prompt
    names = repair.prepare_muse_workspace(
        ws, brief={"x": 1}, scenes={"scenes": SCENES},
        storyboard_schema="{}", prompt=prompt, prior_review=prior,
        previous_storyboard=changed_storyboard(0))
    assert names == sorted(repair.WORKER_FILES)
    assert {p.name for p in ws.iterdir()} == set(repair.WORKER_FILES)
    assert not (ws / "storyboard.json").exists()
    assert not (ws / "source.wav").exists()
    env = repair.sanitized_worker_env(
        {"GH_TOKEN": "secret", "GITHUB_TOKEN": "secret2", "PATH": "/bin",
         "MUSE_RUNTIME_HOME": "/tmp/auth"})
    assert "GH_TOKEN" not in env and "GITHUB_TOKEN" not in env
    assert env["MUSE_RUNTIME_HOME"] == "/tmp/auth"
    assert repair.build_muse_command("/repo/.github/studio/scripts/muse-run.sh", ws) == [
        "bash", "/repo/.github/studio/scripts/muse-run.sh", str(ws)]
    codex_cmd = repair.build_codex_review_command(
        contact_sheet="sheet.jpg", schema_path="review.schema.json",
        output_path="review.json")
    assert codex_cmd[codex_cmd.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="xhigh"' in codex_cmd
    assert 'default_permissions="studio"' in codex_cmd


def test_agent_error_classification_never_leaks_raw_stderr(tmp_path):
    stderr = tmp_path / "agent.stderr"
    stderr.write_text("SECRET_TOKEN_12345 quota exceeded for account")
    out = repair.classify_agent_error(
        stderr, ROOT / ".github/studio/scripts/report-agent-error.py")
    assert "usage_limit" in out and "SECRET_TOKEN_12345" not in out


def test_actual_muse_worker_wrapper_mounts_only_work_and_auth(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text("#!/usr/bin/env python3\nimport json,os,sys\n"
                      "with open(os.environ['DOCKER_CALLS'],'a') as f: "
                      "f.write(json.dumps(sys.argv[1:])+'\\n')\n")
    docker.chmod(0o755)
    work, auth = tmp_path / "work", tmp_path / "auth"
    work.mkdir(); auth.mkdir()
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}",
               MUSE_RUNTIME_HOME=str(auth), DOCKER_CALLS=str(calls),
               GH_TOKEN="must-not-reach-worker")
    repair.run_muse_worker(
        work, muse_run_script=ROOT / ".github/studio/scripts/muse-run.sh",
        runner_temp=tmp_path / "rt", env=env)
    invocations = [json.loads(line) for line in calls.read_text().splitlines()]
    run_invocations = [a for a in invocations if "--mount" in a]
    assert run_invocations, invocations
    args = run_invocations[-1]
    mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
    assert mounts == [f"type=bind,source={work},target=/workspace",
                      f"type=bind,source={auth},target=/agent-config"]
    assert "--name" in args
    assert args[args.index("--name") + 1].startswith("studio-repair-")
    rm_calls = [a for a in invocations if a[:2] == ["rm", "-f"]]
    assert rm_calls and all(
        a[2] == args[args.index("--name") + 1] for a in rm_calls), invocations
    for flag in ("--yolo", "muse-wonder-1.0-contributor",
                 "--max-model-steps", "80", "--prompt-file", "prompt.md",
                 "--reasoning-effort", "max"):
        assert flag in args
    assert "GH_TOKEN" not in " ".join(args)
    assert "must-not-reach-worker" not in " ".join(args)
    assert "source.wav" not in " ".join(args)


# --- bundle snapshots / hashes --------------------------------------------

def _bundle_files(head, review, *, parent=11, child=22, pr=33):
    cand = {"id": "cand-01", "start_sample": 0, "end_sample": 1984500,
            "n_frames": 2700, "sample_rate": 22050, "fps": [30, 1],
            "duration_sec": 90.0, "scenes": SCENES}
    sb = sbmod.default_storyboard(SCENES)
    fp = "f" * 64
    source_hashes = {"attachment_sha256": "a" * 64, "decoded_wav_sha256": "b" * 64}
    brief = {"schema_version": "1.0", "parent_issue": parent,
             "child_issue": child, "candidate_id": "cand-01",
             "fingerprint": fp, "candidate": cand, "piece": None,
             "period": None, "period_direction": None, "art_notes": "",
             "creative_brief": None, "source_hashes": source_hashes}
    plan = {"clip": {"start_sample": 0, "end_sample": 1984500, "n_frames": 2700},
            "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": 1350}],
            "accents": [{"frame": 10}]}
    committed = {
        "brief.json": json.dumps(brief, indent=2).encode(),
        "storyboard.json": json.dumps(sb, indent=2).encode(),
        "plan.json": json.dumps(plan, indent=2).encode(),
        "qa.json": json.dumps({"pass": True, "checks": []}).encode(),
        "timeline.json": json.dumps({"candidate_id": "cand-01"}).encode(),
    }
    media = {"video.mp4": b"video-bytes-" + head[:4].encode(),
             "contact-sheet.jpg": b"jpg-bytes",
             "timeline.html": b"<html>timeline</html>"}
    prov = {"schema_version": "1.0", "candidate_id": "cand-01",
            "child_issue": child, "parent_issue": parent,
            "source_hashes": source_hashes, "planning_fingerprint": fp,
            "media_sha256": {n: hashlib.sha256(d).hexdigest()
                             for n, d in media.items()}}
    committed["provenance.json"] = json.dumps(prov, indent=2).encode()
    production_files = {f"production/{child}/{n}": hashlib.sha256(d).hexdigest()
                        for n, d in committed.items()}
    receipt = artifacts.build_receipt(
        pr=pr, child=child, parent=parent, head_sha=head, run_id=99,
        run_attempt=2, engine_sha=ENGINE, source_hashes=source_hashes,
        planning_fingerprint=fp, production_files=production_files)
    files = {**media, **committed,
             "review.json": json.dumps(review, indent=2).encode(),
             "receipt.json": json.dumps(receipt, indent=2).encode()}
    files["hashes.json"] = json.dumps(
        artifacts.build_hash_manifest(files), indent=2).encode()
    meta = {"brief": brief, "candidate": cand, "storyboard": sb, "plan": plan,
            "production_files": production_files, "parent": parent,
            "child": child, "pr": pr}
    return files, meta


def _write_bundle(tmp_path, head, review):
    files, meta = _bundle_files(head, review)
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name, data in files.items():
        (bundle / name).write_bytes(data)
    return bundle, files, meta


def test_bundle_verify_snapshot_and_tamper_detection(tmp_path):
    bundle, files, meta = _write_bundle(tmp_path, HEAD1, make_review(HEAD1))
    check = repair.verify_bundle_files(
        files, expected_head_sha=HEAD1, pr_number=meta["pr"],
        child=meta["child"], parent=meta["parent"], run_id=99,
        run_attempt=2, engine_sha=ENGINE,
        production_files=meta["production_files"])
    assert check["ok"], check
    snap = repair.snapshot_bundle(bundle, tmp_path / "attempts", 1)
    assert {p.name for p in snap.iterdir()} == set(config.EXPECTED_ARTIFACT_FILES)
    assert not (snap / "source.wav").exists()
    # Immutable: identical re-snapshot is a no-op, differing content raises.
    assert repair.snapshot_bundle(bundle, tmp_path / "attempts", 1) == snap
    (bundle / "video.mp4").write_bytes(b"tampered-video")
    tampered = repair.read_bundle_files(bundle)
    bad = repair.verify_bundle_files(
        tampered, expected_head_sha=HEAD1, pr_number=meta["pr"],
        child=meta["child"], parent=meta["parent"],
        production_files=meta["production_files"])
    assert not bad["ok"]
    with pytest.raises(repair.RepairInfrastructureError):
        repair.snapshot_bundle(bundle, tmp_path / "attempts", 1)


# --- real commit path: new head/receipt, timings preserved -----------------

def _production_files(child, parent, sb_obj):
    cand = {"id": "cand-01", "start_sample": 0, "end_sample": 1984500,
            "n_frames": 2700, "sample_rate": 22050, "fps": [30, 1],
            "duration_sec": 90.0, "scenes": SCENES}
    fp = "f" * 64
    sh = {"attachment_sha256": "a" * 64, "decoded_wav_sha256": "b" * 64}
    brief = {"schema_version": "1.0", "parent_issue": parent,
             "child_issue": child, "candidate_id": "cand-01",
             "fingerprint": fp, "candidate": cand, "piece": None,
             "period": None, "period_direction": None, "art_notes": "",
             "creative_brief": None, "source_hashes": sh}
    plan = {"clip": {"start_sample": 0, "end_sample": 1984500, "n_frames": 2700},
            "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": 1350}],
            "accents": [{"frame": 10}]}
    prov = {"schema_version": "1.0", "candidate_id": "cand-01",
            "source_hashes": sh, "planning_fingerprint": fp,
            "media_sha256": {"video.mp4": "1" * 64,
                             "contact-sheet.jpg": "2" * 64,
                             "timeline.html": "3" * 64}}
    return {
        "brief.json": json.dumps(brief, indent=2).encode(),
        "storyboard.json": json.dumps(sb_obj, indent=2).encode(),
        "plan.json": json.dumps(plan, indent=2).encode(),
        "qa.json": json.dumps({"pass": True}).encode(),
        "provenance.json": json.dumps(prov, indent=2).encode(),
        "timeline.json": json.dumps({"candidate_id": "cand-01"}).encode(),
    }


def _committed_bytes(gh, child, name, ref):
    blob = gh.request("GET", f"/contents/production/{child}/{name}?ref={ref}")
    return base64.b64decode(blob["content"])


def test_loop_fail_to_pass_commits_new_head_receipt_and_preserves_timings():
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    parent_issue = gh.seed_issue(title="source")
    parent, child = parent_issue["number"], 101
    sb0 = sbmod.default_storyboard(SCENES)
    files0 = _production_files(child, parent, sb0)
    commit1 = controller.commit_bundle_controller(
        gh, child=child, files=files0, repo_owner="YusukeIwaki")
    assert commit1["ok"], commit1
    head1, pr = commit1["head_sha"], commit1["pr_number"]
    store = {}

    def runner(idx, ctx):
        sb1 = changed_storyboard(9)
        files1 = dict(files0)
        files1["storyboard.json"] = json.dumps(sb1, indent=2).encode()
        commit2 = controller.commit_bundle_controller(
            gh, child=child, files=files1, repo_owner="YusukeIwaki")
        assert commit2["ok"], commit2
        assert commit2["head_sha"] != head1
        receipt = artifacts.build_receipt(
            pr=pr, child=child, parent=parent, head_sha=commit2["head_sha"],
            run_id=99, run_attempt=2, engine_sha=ENGINE,
            source_hashes={"attachment_sha256": "a" * 64,
                           "decoded_wav_sha256": "b" * 64},
            planning_fingerprint="f" * 64,
            production_files=commit2["blob_sha256"])
        store.update(commit=commit2, receipt=receipt, sb=sb1)
        return {"kind": "reviewed",
                "review": make_review(commit2["head_sha"]),
                "head_sha": commit2["head_sha"], "pr_number": pr,
                "storyboard": sb1,
                "media_sha256": {"video.mp4": "9" * 64},
                "mechanical_pass": True}

    result = repair.run_repair_loop(
        initial_review=make_review(head1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=head1,
        initial_pr_number=pr, attempt_runner=FakeRunner([runner]),
        initial_storyboard=sb0,
        initial_media_sha256={"video.mp4": "0" * 64})
    assert result["status"] == "passed" and result["attempt_count"] == 2
    commit2, receipt = store["commit"], store["receipt"]
    assert result["final_head_sha"] == commit2["head_sha"]
    # Receipt binds the NEW head and the ACTUAL committed blob hashes.
    assert receipt["head_sha"] == commit2["head_sha"]
    assert artifacts.validate_receipt(
        receipt, pr=pr, child=child, parent=parent,
        head_sha=commit2["head_sha"],
        production_files=commit2["blob_sha256"],
        expected_run_id=99, expected_run_attempt=2,
        expected_engine_sha=ENGINE)["ok"]
    for name, want in commit2["blob_sha256"].items():
        fname = name.rsplit("/", 1)[-1]
        data = _committed_bytes(gh, child, fname, commit2["head_sha"])
        assert hashlib.sha256(data).hexdigest() == want
    # Timings preserved: plan bytes (clip/scenes/accents) identical at both heads.
    assert _committed_bytes(gh, child, "plan.json", head1) == \
        _committed_bytes(gh, child, "plan.json", commit2["head_sha"])
    # Storyboard actually changed at the new head.
    assert _committed_bytes(gh, child, "storyboard.json", head1) != \
        _committed_bytes(gh, child, "storyboard.json", commit2["head_sha"])


# --- publish: failure feedback, dedupe, pass unchanged ----------------------

def _make_pr(gh, child=101):
    parent_issue = gh.seed_issue(title="source")
    sb = sbmod.default_storyboard(SCENES)
    commit = controller.commit_bundle_controller(
        gh, child=child, files=_production_files(child, parent_issue["number"], sb),
        repo_owner="YusukeIwaki")
    assert commit["ok"], commit
    return parent_issue["number"], commit


@pytest.fixture()
def gh_world():
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    return gh


def test_publish_failure_contains_full_feedback_and_dedupes(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    review = make_review(head, verdict="fail",
                         blockers=["BLOCKER-ONE washes out", "BLOCKER-TWO clips glow"],
                         instructions=["INSTR-ONE deepen sky_top", "INSTR-TWO lower glow_scale"])
    r1 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="https://example.invalid/art",
        review=review, mechanical_pass=True)
    assert not r1["ok"] and r1["feedback_posted"] is True
    bodies = [r["body"] for r in gh.reviews[pr]]
    assert len(bodies) == 1 and bodies[0].startswith("Automated review did not pass;")
    for text in ("BLOCKER-ONE", "BLOCKER-TWO", "INSTR-ONE", "INSTR-TWO",
                 "aesthetic_approval", "storyboard", head, "Next action:"):
        assert text in bodies[0], text
    assert controller.EXACT_PING not in bodies[0]
    assert {"login": OWNER} not in gh.issues[pr]["assignees"]
    assert gh.comments.get(pr, []) == []  # no receipt comment on failure
    r2 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="https://example.invalid/art",
        review=review, mechanical_pass=True)
    assert r2["feedback_deduped"] is True
    assert len([r for r in gh.reviews[pr]]) == 1


def test_publish_pass_with_repair_remains_exact_and_owner_only(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    r = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=make_review(head),
        mechanical_pass=True,
        repair_result={"status": "passed", "final_head_sha": head,
                       "pr_number": pr, "parent_issue": parent})
    assert r["ok"] and r["notified"] is True
    pings = [x for x in gh.reviews[pr] if x["body"] == controller.EXACT_PING]
    assert len(pings) == 1 and pings[0]["commit_id"] == head
    assert {"login": OWNER} in gh.issues[pr]["assignees"]


@pytest.mark.parametrize("review,mech", [
    (None, True),
    ({"verdict": "pass"}, True),
])
def test_publish_malformed_or_mechanical_never_notifies(gh_world, review, mech):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    bad = review if review is not None else make_review(head)
    if review is None:
        bad = dict(bad, repair={"route": "storyboard", "instructions": ["x"]})
    r = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=bad, mechanical_pass=mech)
    assert not r["ok"] and r["notified"] is False
    assert not [x for x in gh.reviews.get(pr, []) if x["body"] == controller.EXACT_PING]
    assert {"login": OWNER} not in gh.issues[pr]["assignees"]

    mech_fail = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=make_review(head),
        mechanical_pass=False)
    assert not mech_fail["ok"]
    assert not [x for x in gh.reviews.get(pr, []) if x["body"] == controller.EXACT_PING]

    stale = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha="0" * 40, run_id=11,
        run_attempt=1, artifact_id=42, artifact_url="",
        review=make_review("0" * 40), mechanical_pass=True)
    assert not stale["ok"]


# --- engineering escalation -------------------------------------------------

def _eng_kwargs(child, head=HEAD1):
    return dict(parent=11, child=child, pr_number=33, head_sha=head,
                run_id=99, run_attempt=2, artifact_id=42,
                artifact_url="https://example.invalid/art",
                review=make_review(head, verdict="fail", route="engine",
                                   blockers=["needs a new asset pipeline"],
                                   instructions=["implement layered asset rendering; add render tests"]),
                repair_result={"status": "needs_engine",
                               "next_action": "engineering"},
                gate_failures=["review check not true: aesthetic_approval"])


def test_engineering_issue_idempotent_and_actionable():
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    parent = gh.seed_issue(title="source")
    r1 = controller.ensure_engineering_issue(gh, **_eng_kwargs(201))
    assert r1["created"] is True
    iss = gh.issues[r1["issue_number"]]
    assert (iss["user"] or {})["login"] == BOT
    body = iss["body"]
    for text in ("#11", "#201", "#33", HEAD1, "needs a new asset pipeline",
                 "implement layered asset rendering",
                 "Why automatic repair stopped",
                 "What a code agent should modify/test",
                 "engineering implementation and review before rerunning"):
        assert text in body, text
    assert "Fixes #201" not in body and f"Fixes #{201}" not in body
    assert 201 not in gh.issues[parent["number"]]["sub_issues"]
    r2 = controller.ensure_engineering_issue(
        gh, **_eng_kwargs(201, head=HEAD2))
    assert r2["issue_number"] == r1["issue_number"] and not r2["created"]
    assert HEAD2 in gh.issues[r1["issue_number"]]["body"]
    bot_eng = [i for i in gh.issues.values()
               if (i.get("user") or {}).get("login") == BOT
               and 201 in controller.parse_engineering_marker(i.get("body") or "")]
    assert len(bot_eng) == 1


def test_engineering_forged_marker_ignored_and_closed_not_reopened():
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    forged = gh.seed_issue(
        body=controller.engineering_marker(202), author=OWNER)
    r = controller.ensure_engineering_issue(gh, **_eng_kwargs(202))
    assert r["created"] and r["issue_number"] != forged["number"]
    # Closed bot issue: never reopened, no duplicate created.
    gh2 = FakeTransport()
    gh2.seed_commit({"README.md": b"studio"}, branch="main")
    closed = gh2.seed_issue(body=controller.engineering_marker(203), author=BOT)
    gh2.issues[closed["number"]].update(
        {"state": "closed", "closed_by": {"login": OWNER}})
    before = len(gh2.issues)
    r2 = controller.ensure_engineering_issue(gh2, **_eng_kwargs(203))
    assert r2["action"] == "closed_not_reopened" and not r2["created"]
    assert len(gh2.issues) == before
    assert gh2.issues[closed["number"]]["state"] == "closed"


def test_publish_engine_outcome_creates_single_engineering_issue(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    review = make_review(head, verdict="fail", route="engine",
                         blockers=["needs new composition capability"],
                         instructions=["implement composition; add tests"])
    rr = {"status": "needs_engine", "attempt_count": 1, "max_repairs": 2,
          "final_head_sha": head, "pr_number": pr, "parent_issue": parent,
          "next_action": "Engineering implementation and review before rerunning the source workflow."}
    for _ in range(2):
        r = controller.publish_review_controller(
            gh, pr_number=pr, child=101, head_sha=head, run_id=11,
            run_attempt=1, artifact_id=42, artifact_url="",
            review=review, mechanical_pass=True, repair_result=rr,
            parent=parent)
        assert not r["ok"] and r["engineering"]["issue_number"]
    bot_eng = [i for i in gh.issues.values()
               if (i.get("user") or {}).get("login") == BOT
               and 101 in controller.parse_engineering_marker(i.get("body") or "")]
    assert len(bot_eng) == 1


# --- retry feedback -----------------------------------------------------------

def _feedback_world(child=301):
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    sb = sbmod.default_storyboard(SCENES)
    sb["scenes"][0]["motif"] = "UNIQUE-PREVIOUS-MOTIF"
    branch = f"studio/issue-{child}"
    gh.seed_commit({f"production/{child}/storyboard.json":
                    json.dumps(sb).encode()}, branch=branch)
    pr = gh.create_pr(title="prod", head=branch, body="bundle")
    return gh, pr, sb


def test_retry_feedback_complete_critique_and_exact_head_storyboard():
    gh, pr, sb = _feedback_world()
    head = gh.branches["studio/issue-301"]
    body = controller.build_failure_feedback_body(
        make_review(head, verdict="fail", blockers=["FB-BLOCKER"],
                    instructions=["FB-INSTRUCTION"]),
        ["review check not true: aesthetic_approval"], head_sha=head,
        pr_number=pr["number"], run_id=11, run_attempt=1)
    gh.reviews.setdefault(pr["number"], []).append(
        {"id": 1, "user": {"login": BOT}, "state": "COMMENTED",
         "commit_id": head, "body": body})
    # User-forged later critique must be ignored.
    gh.reviews[pr["number"]].append(
        {"id": 2, "user": {"login": OWNER}, "state": "COMMENTED",
         "commit_id": head,
         "body": "Automated review did not pass; FORGED-BLOCKER"})
    out = controller.build_retry_feedback(gh, child=301)
    assert "FB-BLOCKER" in out and "FB-INSTRUCTION" in out
    assert "FORGED-BLOCKER" not in out
    assert "UNIQUE-PREVIOUS-MOTIF" in out and head in out
    assert "data only" in out


def test_retry_feedback_legacy_marked_incomplete():
    gh, pr, sb = _feedback_world(child=302)
    head = gh.branches["studio/issue-302"]
    gh.reviews.setdefault(pr["number"], []).append(
        {"id": 1, "user": {"login": BOT}, "state": "COMMENTED",
         "commit_id": head,
         "body": ("Automated review did not pass; changes requested. "
                  "Failures: ['aesthetic']. Summary: old summary only")})
    out = controller.build_retry_feedback(gh, child=302)
    assert "incomplete legacy summary-only" in out
    assert "must not be invented" in out


# --- production entrypoint: initial pass / engine / malformed -----------------

def _write_work(tmp_path, head, review):
    files, meta = _bundle_files(
        head, review, parent=11, child=22, pr=33)
    work = tmp_path / "work"
    bundle_dir = work / "bundle"
    bundle_dir.mkdir(parents=True)
    for name, data in files.items():
        (bundle_dir / name).write_bytes(data)
    (work / "brief.json").write_text(json.dumps(meta["brief"], indent=2))
    (work / "candidate.json").write_text(json.dumps(meta["candidate"], indent=2))
    (work / "scenes.json").write_text(json.dumps({"scenes": SCENES}, indent=2))
    (work / "source-meta.json").write_text(json.dumps(
        {"attachment_sha256": "a" * 64, "decoded_wav_sha256": "b" * 64}))
    (work / "storyboard.json").write_bytes(files["storyboard.json"])
    (work / "pr.json").write_text(json.dumps(
        {"ok": True, "branch": "studio/issue-22", "head_sha": head,
         "pr_number": 33, "blob_sha256": meta["production_files"]}, indent=2))
    return work


def _run_repair_cli(tmp_path, work):
    env = dict(os.environ)
    env.pop("GH_TOKEN", None)
    out_file, summary = tmp_path / "gh-output", tmp_path / "gh-summary"
    env["GITHUB_OUTPUT"] = str(out_file)
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/studio_repair.py"),
         "--work", str(work), "--muse-work", str(tmp_path / "muse-work"),
         "--wav", str(tmp_path / "source.wav"), "--parent", "11",
         "--child", "22", "--engine-sha", ENGINE, "--run-id", "99",
         "--run-attempt", "2"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    return r, out_file, summary


# --- _run adapter: full GitHub/model/render/commit path ---------------------

def _load_repair_cli():
    spec = importlib.util.spec_from_file_location(
        "studio_repair_cli_under_test", ROOT / "scripts/studio_repair.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _AdapterWorld:
    pass


def _build_adapter_world(tmp_path, monkeypatch, initial_review_fn,
                         *, muse_mode="pass", render_mode="new_video",
                         codex_verdict="pass", after_codex=None):
    """Real commit/receipt/guard/loop/snapshot path with fake agents."""
    wav_bytes = b"adapter-fake-wav-bytes"
    wav_sha = hashlib.sha256(wav_bytes).hexdigest()
    gh = FakeTransport()
    gh.seed_commit({"README.md": b"studio"}, branch="main")
    parent = gh.seed_issue(title="source", author=OWNER)["number"]
    child = gh.seed_issue(title="child", author=BOT)["number"]
    sb0 = sbmod.default_storyboard(SCENES)
    commit1 = controller.commit_bundle_controller(
        gh, child=child, files=_production_files(child, parent, sb0),
        repo_owner="YusukeIwaki")
    assert commit1["ok"], commit1
    head1, pr = commit1["head_sha"], commit1["pr_number"]
    review = initial_review_fn(head1)
    files, meta = _bundle_files(head1, review, parent=parent, child=child, pr=pr)
    brief = json.loads(files["brief.json"])
    brief["source_hashes"]["decoded_wav_sha256"] = wav_sha
    prov = json.loads(files["provenance.json"])
    prov["source_hashes"]["decoded_wav_sha256"] = wav_sha
    files["brief.json"] = json.dumps(brief, indent=2).encode()
    files["provenance.json"] = json.dumps(prov, indent=2).encode()
    production_files = {
        f"production/{child}/{n}": hashlib.sha256(files[n]).hexdigest()
        for n in config.COMMITTED_PRODUCTION_FILES}
    receipt = json.loads(files["receipt.json"])
    receipt["source_hashes"]["decoded_wav_sha256"] = wav_sha
    receipt["production_files"] = production_files
    files["receipt.json"] = json.dumps(receipt, indent=2).encode()
    files["hashes.json"] = json.dumps(artifacts.build_hash_manifest(
        {k: v for k, v in files.items() if k != "hashes.json"}), indent=2).encode()
    meta.update(production_files=production_files, brief=brief,
                candidate=brief["candidate"])
    work = tmp_path / "work"
    bundle_dir = work / "bundle"
    bundle_dir.mkdir(parents=True)
    for name, data in files.items():
        (bundle_dir / name).write_bytes(data)
    (work / "brief.json").write_bytes(files["brief.json"])
    (work / "candidate.json").write_text(json.dumps(brief["candidate"], indent=2))
    (work / "scenes.json").write_text(json.dumps({"scenes": SCENES}, indent=2))
    (work / "storyboard.json").write_bytes(files["storyboard.json"])
    (work / "pr.json").write_text(json.dumps(
        {"ok": True, "branch": f"studio/issue-{child}", "head_sha": head1,
         "pr_number": pr, "blob_sha256": production_files}, indent=2))
    wav = tmp_path / "source.wav"
    wav.write_bytes(wav_bytes)
    initial_plan = json.loads(files["plan.json"])
    initial_video = files["video.mp4"]
    world = _AdapterWorld()
    world.gh, world.parent, world.child, world.pr = gh, parent, child, pr
    world.head1, world.work, world.wav = head1, work, wav
    world.files, world.meta, world.brief = files, meta, brief
    world.initial_plan, world.counters = initial_plan, {"muse": 0, "render": 0, "codex": 0}
    world.captured = {}
    world.muse_mode, world.render_mode = muse_mode, render_mode
    world.codex_verdict, world.after_codex = codex_verdict, after_codex

    def fake_muse(workspace, *, muse_run_script, runner_temp,
                  report_script=None, timeout=None, env=None):
        world.counters["muse"] += 1
        ws = Path(workspace)
        world.captured["workspace_files"] = sorted(p.name for p in ws.iterdir())
        world.captured["prompt"] = (ws / "prompt.md").read_text()
        world.captured["prev_review"] = json.loads(
            (ws / "previous-review.json").read_text())
        if world.muse_mode == "no_change":
            prev = json.loads((ws / "previous-storyboard.json").read_text())
            (ws / "storyboard.json").write_text(json.dumps(prev, indent=2))
        else:
            (ws / "storyboard.json").write_text(
                json.dumps(changed_storyboard(7), indent=2))

    def fake_render(command, *, stdout_path, stderr_path, **kw):
        world.counters["render"] += 1
        args = list(command)
        staging = Path(args[args.index("--out") + 1])
        sb_bytes = Path(args[args.index("--storyboard") + 1]).read_bytes()
        staging.mkdir(parents=True, exist_ok=True)
        video = initial_video if world.render_mode == "same_video" \
            else b"video-bytes-repaired"
        media = {"video.mp4": video, "contact-sheet.jpg": b"jpg-repaired",
                 "timeline.html": b"<html>repaired</html>"}
        for name, data in media.items():
            (staging / name).write_bytes(data)
        (staging / "storyboard.json").write_bytes(sb_bytes)
        (staging / "brief.json").write_bytes(files["brief.json"])
        (staging / "plan.json").write_text(json.dumps(initial_plan, indent=2))
        (staging / "qa.json").write_text(json.dumps({"pass": True}))
        (staging / "timeline.json").write_text(
            json.dumps({"candidate_id": "cand-01"}))
        staging_prov = {
            "schema_version": "1.0", "candidate_id": "cand-01",
            "source_hashes": brief["source_hashes"],
            "planning_fingerprint": brief["fingerprint"],
            "media_sha256": {n: hashlib.sha256(d).hexdigest()
                             for n, d in media.items()}}
        (staging / "provenance.json").write_text(
            json.dumps(staging_prov, indent=2))
        return types.SimpleNamespace(returncode=0)

    def fake_codex(*, contact_sheet, schema_path, output_path, prompt_path,
                   runner_temp, report_script=None, timeout=None, env=None):
        world.counters["codex"] += 1
        prompt_text = Path(prompt_path).read_text()
        world.captured["codex_prompt"] = prompt_text
        head = re.search(r"Expected head SHA: ([0-9a-f]{40})", prompt_text).group(1)
        rev = make_review(head) if world.codex_verdict == "pass" else \
            make_review(head, verdict="fail")
        Path(output_path).write_text(json.dumps(rev, indent=2))
        if world.after_codex is not None:
            world.after_codex(world, head)

    cli = _load_repair_cli()
    monkeypatch.setattr(cli, "GitHubClient", lambda repo, token: gh)
    monkeypatch.setattr(repair, "run_muse_worker", fake_muse)
    monkeypatch.setattr(repair, "run_agent_command", fake_render)
    monkeypatch.setattr(repair, "run_codex_review", fake_codex)
    runner_temp = tmp_path / "runner-temp"
    monkeypatch.setenv("GITHUB_REPOSITORY", gh.repo)
    monkeypatch.setenv("GH_TOKEN", "fake-token-not-in-history")
    monkeypatch.setenv("RUNNER_TEMP", str(runner_temp))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh-output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "gh-summary"))
    world.cli = cli
    world.args = types.SimpleNamespace(
        work=str(work), muse_work=str(tmp_path / "muse-work"),
        wav=str(wav), parent=parent, child=child, engine_sha=ENGINE,
        run_id=99, run_attempt=2)
    world.runner_temp = runner_temp
    return world


def test_repair_adapter_initial_fail_to_repaired_pass_full_path(tmp_path, monkeypatch):
    world = _build_adapter_world(
        tmp_path, monkeypatch,
        lambda h: make_review(h, verdict="fail",
                              blockers=["ADAPTER-BLOCKER washes out"],
                              instructions=["ADAPTER-INSTRUCTION deepen palette"]))
    rc = world.cli._run(world.args)
    assert rc == 0
    assert world.counters == {"muse": 1, "render": 1, "codex": 1}
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "passed" and result["attempt_count"] == 2
    assert result["repairs_attempted"] == 1 and result["repairs_used"] == 1
    head2 = world.gh.branches[f"studio/issue-{world.child}"]
    assert result["final_head_sha"] == head2 and head2 != world.head1
    snaps = sorted(p.name for p in (world.work / "review-attempts").iterdir())
    assert snaps == ["attempt-01", "attempt-02"]
    for snap in (world.work / "review-attempts").iterdir():
        assert {p.name for p in snap.iterdir()} == set(config.EXPECTED_ARTIFACT_FILES)
        for f in snap.iterdir():
            data = f.read_bytes()
            assert b"adapter-fake-wav" not in data
            assert b"fake-token-not-in-history" not in data
    # Full feedback reached the isolated worker; allowlist only.
    assert world.captured["workspace_files"] == sorted(repair.WORKER_FILES)
    assert "ADAPTER-BLOCKER" in world.captured["prompt"]
    assert "ADAPTER-INSTRUCTION" in world.captured["prompt"]
    assert "ADAPTER-BLOCKER" in json.dumps(world.captured["prev_review"])
    # Final bundle receipt/committed bytes agree at the final head.
    final_files = repair.read_bundle_files(world.work / "bundle")
    receipt = json.loads(final_files["receipt.json"])
    assert receipt["head_sha"] == head2
    for rel, want in receipt["production_files"].items():
        name = rel.rsplit("/", 1)[-1]
        committed = _committed_bytes(world.gh, world.child, name, head2)
        assert hashlib.sha256(committed).hexdigest() == want
        assert hashlib.sha256(final_files[name]).hexdigest() == want
    final_plan = json.loads(final_files["plan.json"])
    for key in ("clip", "scenes", "accents"):
        assert final_plan[key] == world.initial_plan[key]
    assert json.loads(final_files["storyboard.json"]) != world.meta["storyboard"]
    assert json.loads((world.work / "pr.json").read_text())["head_sha"] == head2


def test_repair_adapter_initial_pass_guarded_no_model(tmp_path, monkeypatch):
    world = _build_adapter_world(tmp_path, monkeypatch, lambda h: make_review(h))
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "passed" and result["attempt_count"] == 1
    assert result["repairs_attempted"] == 0 and result["final_head_sha"] == world.head1
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}
    assert [p.name for p in (world.work / "review-attempts").iterdir()] == ["attempt-01"]


def test_repair_adapter_engine_guarded_no_model(tmp_path, monkeypatch):
    world = _build_adapter_world(
        tmp_path, monkeypatch,
        lambda h: make_review(h, verdict="fail", route="engine",
                              instructions=["implement asset pipeline; add render tests"]))
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "needs_engine" and result["attempt_count"] == 1
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}


def test_repair_adapter_initial_pass_stale_head_rejected(tmp_path, monkeypatch):
    world = _build_adapter_world(tmp_path, monkeypatch, lambda h: make_review(h))
    controller.commit_bundle_controller(
        world.gh, child=world.child,
        files=_production_files(world.child, world.parent, changed_storyboard(3)),
        repo_owner="YusukeIwaki")
    with pytest.raises(repair.RepairInfrastructureError):
        world.cli._run(world.args)
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}
    assert not (world.work / "review-attempts").exists()
    assert not (world.work / "repair-result.json").exists()


@pytest.mark.parametrize("close_target", ["parent", "child", "pr"])
def test_repair_adapter_closed_source_child_or_pr_rejected(tmp_path, monkeypatch, close_target):
    world = _build_adapter_world(tmp_path, monkeypatch, lambda h: make_review(h))
    if close_target == "parent":
        world.gh.issues[world.parent]["state"] = "closed"
    elif close_target == "child":
        world.gh.issues[world.child]["state"] = "closed"
    else:
        world.gh.pulls[world.pr]["state"] = "closed"
    with pytest.raises(repair.RepairInfrastructureError):
        world.cli._run(world.args)
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}
    assert not (world.work / "repair-result.json").exists()


def test_repair_adapter_source_drift_no_expensive_calls(tmp_path, monkeypatch):
    world = _build_adapter_world(
        tmp_path, monkeypatch, lambda h: make_review(h, verdict="fail"))
    world.wav.write_bytes(b"different-wav-after-planning")
    with pytest.raises(repair.RepairInfrastructureError):
        world.cli._run(world.args)
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}
    assert not (world.work / "review-attempts").exists()


def test_repair_adapter_no_change_counts_attempted_repair(tmp_path, monkeypatch):
    world = _build_adapter_world(
        tmp_path, monkeypatch, lambda h: make_review(h, verdict="fail"),
        muse_mode="no_change")
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "no_change" and result["attempt_count"] == 1
    assert result["repairs_attempted"] == 1 and result["muse_repair_calls"] == 1
    assert world.counters == {"muse": 1, "render": 0, "codex": 0}
    assert [p.name for p in (world.work / "review-attempts").iterdir()] == ["attempt-01"]


def test_repair_adapter_no_visual_change_counts_attempted_repair(tmp_path, monkeypatch):
    world = _build_adapter_world(
        tmp_path, monkeypatch, lambda h: make_review(h, verdict="fail"),
        render_mode="same_video")
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "no_visual_change" and result["attempt_count"] == 1
    assert result["repairs_attempted"] == 1
    assert world.counters == {"muse": 1, "render": 1, "codex": 0}


def test_repair_adapter_final_stale_head_rejected_before_publish(tmp_path, monkeypatch):
    def advance(world, _head):
        controller.commit_bundle_controller(
            world.gh, child=world.child,
            files=_production_files(world.child, world.parent, changed_storyboard(99)),
            repo_owner="YusukeIwaki")
    world = _build_adapter_world(
        tmp_path, monkeypatch, lambda h: make_review(h, verdict="fail"),
        after_codex=advance)
    with pytest.raises(repair.RepairInfrastructureError):
        world.cli._run(world.args)
    assert world.counters == {"muse": 1, "render": 1, "codex": 1}
    assert not (world.work / "repair-result.json").exists()
    # Final bundle was not replaced with the stale reviewed attempt.
    review = json.loads(repair.read_bundle_files(world.work / "bundle")["review.json"])
    assert review["reviewed_head_sha"] == world.head1


def test_repair_cli_malformed_legacy_review_fails_closed(tmp_path):
    work = _write_work(tmp_path, HEAD1, legacy_review(HEAD1))
    r, out_file, _ = _run_repair_cli(tmp_path, work)
    assert r.returncode == 2
    assert not (work / "repair-result.json").exists()
    assert not out_file.exists() or f"head_sha={HEAD1}" not in out_file.read_text()


# --- rehashed tampering / non-object bundles (findings 1 & 3) --------------

def _rehash(files):
    out = dict(files)
    out["hashes.json"] = json.dumps(artifacts.build_hash_manifest(
        {k: v for k, v in out.items() if k != "hashes.json"}), indent=2).encode()
    return out


@pytest.mark.parametrize("tampered", ["video.mp4", "storyboard.json", "provenance.json"])
def test_verify_bundle_rehashed_tamper_fails_closed(tampered):
    files, meta = _bundle_files(HEAD1, make_review(HEAD1))
    kw = dict(expected_head_sha=HEAD1, pr_number=meta["pr"],
              child=meta["child"], parent=meta["parent"], run_id=99,
              run_attempt=2, engine_sha=ENGINE,
              production_files=meta["production_files"])
    assert repair.verify_bundle_files(files, **kw)["ok"]
    bad = _rehash({**files, tampered: b"arbitrary-tamper-bytes"})
    result = repair.verify_bundle_files(bad, **kw)
    assert not result["ok"], tampered


@pytest.mark.parametrize("field", ["receipt.json", "brief.json", "provenance.json",
                                   "review.json", "qa.json", "hashes.json"])
def test_verify_bundle_nonobject_json_is_controlled_failure(field):
    files, meta = _bundle_files(HEAD1, make_review(HEAD1))
    replaced = {**files, field: json.dumps(["not", "an", "object"]).encode()}
    bad = replaced if field == "hashes.json" else _rehash(replaced)
    # Must return failures, never raise an incidental traceback.
    result = repair.verify_bundle_files(
        bad, expected_head_sha=HEAD1, pr_number=meta["pr"],
        child=meta["child"], parent=meta["parent"],
        production_files=meta["production_files"])
    assert not result["ok"]


def test_loop_no_change_reports_one_attempted_repair():
    runner = FakeRunner([{"kind": "no_change"}])
    result = repair.run_repair_loop(
        initial_review=make_review(HEAD1, verdict="fail"),
        initial_mechanical_pass=True, initial_head_sha=HEAD1,
        initial_pr_number=7, attempt_runner=runner,
        initial_storyboard=changed_storyboard(0))
    assert result["status"] == "no_change" and result["attempt_count"] == 1
    assert result["repairs_attempted"] == 1 and result["repair_count"] == 1
    assert result["muse_repair_calls"] == 1 and result["repairs_used"] == 1
    built = repair.build_repair_result(result, parent=11, child=22)
    assert built["repairs_attempted"] == 1 and built["repairs_used"] == 1


# --- process / container lifecycle (finding 2) -------------------------------

def _write_fake_docker(bindir: Path, log: Path, *, fail_rm=False):
    bindir.mkdir(parents=True, exist_ok=True)
    docker = bindir / "docker"
    docker.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"LOG = {str(log)!r}\n"
        "with open(LOG, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        + ("if sys.argv[1:2] == ['rm']: sys.exit(3)\n" if fail_rm else ""))
    docker.chmod(0o755)
    return docker


def test_run_agent_timeout_kills_whole_process_group(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(child_pid_file)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(30)\n")
    with pytest.raises(repair.RepairInfrastructureError) as ei:
        repair.run_agent_command(
            [sys.executable, str(script)],
            stdout_path=tmp_path / "o", stderr_path=tmp_path / "e",
            timeout=1, label="fake agent")
    assert "timed out" in str(ei.value) and "classification:" in str(ei.value)
    child_pid = int(child_pid_file.read_text())
    # The grandchild is reaped by init, not by us: poll briefly and
    # treat a zombie as dead.
    import time as _time
    dead = False
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            dead = True
            break
        try:
            state = Path(f"/proc/{child_pid}/stat").read_text().split()[2]
            if state == "Z":
                dead = True
                break
        except Exception:
            dead = True
            break
        _time.sleep(0.1)
    assert dead, f"grandchild {child_pid} survived the process-group kill"


def test_run_agent_timeout_removes_only_owned_container(tmp_path):
    log = tmp_path / "docker.log"
    _write_fake_docker(tmp_path / "bin", log)
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    script = tmp_path / "sleeper.py"
    script.write_text("import time; time.sleep(30)\n")
    with pytest.raises(repair.RepairInfrastructureError):
        repair.run_agent_command(
            [sys.executable, str(script)],
            stdout_path=tmp_path / "o", stderr_path=tmp_path / "e",
            timeout=1, env=env, label="Muse repair",
            container_name="studio-repair-owned123")
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    assert calls == [["rm", "-f", "studio-repair-owned123"]], calls


def test_run_agent_unconfirmed_container_cleanup_fails_closed(tmp_path):
    log = tmp_path / "docker.log"
    _write_fake_docker(tmp_path / "bin", log, fail_rm=True)
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    with pytest.raises(repair.RepairInfrastructureError) as ei:
        repair.run_agent_command(
            [sys.executable, "-c", "pass"],
            stdout_path=tmp_path / "o", stderr_path=tmp_path / "e",
            timeout=10, env=env, label="Muse repair",
            container_name="studio-repair-owned123")
    assert "cleanup unconfirmed" in str(ei.value)


def test_muse_wrapper_named_container_and_trap_cleanup(tmp_path):
    log = tmp_path / "docker.log"
    _write_fake_docker(tmp_path / "bin", log)
    work, auth = tmp_path / "work", tmp_path / "auth"
    work.mkdir(); auth.mkdir()
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}",
               MUSE_RUNTIME_HOME=str(auth), DOCKER_CALLS="unused",
               MUSE_REPAIR_CONTAINER_NAME="studio-repair-wrapper9")
    # Fake docker logs via LOG constant, not DOCKER_CALLS.
    subprocess.run(["bash", str(ROOT / ".github/studio/scripts/muse-run.sh"),
                    str(work)], cwd=ROOT, env=env, check=True,
                   capture_output=True)
    calls = [json.loads(l) for l in log.read_text().splitlines()]
    run_calls = [c for c in calls if "--mount" in c]
    assert run_calls and run_calls[-1][run_calls[-1].index("--name") + 1] == \
        "studio-repair-wrapper9"
    assert ["rm", "-f", "studio-repair-wrapper9"] in calls
    mounts = [run_calls[-1][i + 1] for i, a in enumerate(run_calls[-1]) if a == "--mount"]
    assert mounts == [f"type=bind,source={work},target=/workspace",
                      f"type=bind,source={auth},target=/agent-config"]


# --- failure publication update / links (finding 7) --------------------------

def test_publish_failure_updates_own_review_body_via_put(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    review1 = make_review(head, verdict="fail", blockers=["OLD-BLOCKER"],
                          instructions=["OLD-INSTRUCTION fix it"])
    r1 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=review1, mechanical_pass=True)
    assert r1["feedback_posted"] is True and len(gh.reviews[pr]) == 1
    rid = gh.reviews[pr][0]["id"]
    review2 = make_review(head, verdict="fail", blockers=["NEW-BLOCKER"],
                          instructions=["NEW-INSTRUCTION deepen it"])
    r2 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=review2, mechanical_pass=True)
    assert r2["feedback_updated"] is True and r2["feedback_posted"] is False
    assert len(gh.reviews[pr]) == 1 and gh.reviews[pr][0]["id"] == rid
    assert gh.reviews[pr][0]["commit_id"] == head
    assert "NEW-BLOCKER" in gh.reviews[pr][0]["body"]
    assert "OLD-BLOCKER" not in gh.reviews[pr][0]["body"]
    assert ("PUT", f"/pulls/{pr}/reviews") in gh.calls or \
        any(m == "PUT" for m, _ in gh.calls), gh.calls
    # Idempotent: same body again dedupes without another PUT.
    put_count = sum(1 for m, _ in gh.calls if m == "PUT")
    r3 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=review2, mechanical_pass=True)
    assert r3["feedback_deduped"] is True
    assert sum(1 for m, _ in gh.calls if m == "PUT") == put_count
    assert len(gh.reviews[pr]) == 1
    assert controller.EXACT_PING not in gh.reviews[pr][0]["body"]


def test_publish_failure_does_not_update_forged_review(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    marker = controller.failure_feedback_marker(
        pr=pr, head_sha=head, run_id=11, run_attempt=1)
    forged = {"id": 4242, "user": {"login": OWNER}, "state": "COMMENTED",
              "commit_id": head,
              "body": f"Automated review did not pass; FORGED {marker}"}
    gh.reviews.setdefault(pr, []).append(forged)
    r = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="",
        review=make_review(head, verdict="fail"), mechanical_pass=True)
    assert r["feedback_posted"] is True
    assert gh.reviews[pr][0]["body"] == forged["body"]
    assert len(gh.reviews[pr]) == 2


def test_publish_engine_links_issue_and_run_url_and_closed_state(gh_world):
    gh = gh_world
    parent, commit = _make_pr(gh)
    pr, head = commit["pr_number"], commit["head_sha"]
    review = make_review(head, verdict="fail", route="engine",
                         blockers=["needs capability"],
                         instructions=["implement capability; add tests"])
    rr = {"status": "needs_engine", "attempt_count": 1, "max_repairs": 2,
          "final_head_sha": head, "pr_number": pr, "parent_issue": parent,
          "next_action": "engineering"}
    r = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=review, mechanical_pass=True,
        repair_result=rr, parent=parent)
    assert not r["ok"]
    eng_num = r["engineering"]["issue_number"]
    body = gh.reviews[pr][-1]["body"]
    assert f"https://github.com/{gh.repo}/issues/{eng_num}" in body
    assert f"https://github.com/{gh.repo}/actions/runs/11" in body
    assert "Next action:" in body and "engineering" in body.lower()
    assert r["run_url"] == f"https://github.com/{gh.repo}/actions/runs/11"
    # Close the engineering issue: a later publish must surface closed,
    # never claim an open handoff, and must not reopen it.
    gh.issues[eng_num]["state"] = "closed"
    r2 = controller.publish_review_controller(
        gh, pr_number=pr, child=101, head_sha=head, run_id=11, run_attempt=1,
        artifact_id=42, artifact_url="", review=review, mechanical_pass=True,
        repair_result=rr, parent=parent)
    assert r2["engineering"]["action"] == "closed_not_reopened"
    assert gh.issues[eng_num]["state"] == "closed"
    body2 = gh.reviews[pr][-1]["body"]
    assert "closed" in body2 and "not reopened" in body2
    assert "no open" in body2.lower() or "no open engineering handoff" in body2


# --- history validation (integration check) ----------------------------------

def _history_world(tmp_path, heads_reviews):
    base = tmp_path / "review-attempts"
    base.mkdir()
    for i, (head, review) in enumerate(heads_reviews, start=1):
        files, _meta = _bundle_files(head, review)
        d = base / f"attempt-{i:02d}"
        d.mkdir()
        for name, data in files.items():
            (d / name).write_bytes(data)
    return base


def test_history_validation_accepts_sequential_valid_bundles(tmp_path):
    base = _history_world(tmp_path, [(HEAD1, make_review(HEAD1)),
                                     (HEAD2, make_review(HEAD2))])
    r = repair.validate_attempt_history(base)
    assert r["ok"], r
    assert r["count"] == 2


def test_history_validation_rejects_extra_and_nonsequential(tmp_path):
    base = _history_world(tmp_path, [(HEAD1, make_review(HEAD1))])
    (base / "extra.txt").write_text("nope")
    assert not repair.validate_attempt_history(base)["ok"]
    (base / "extra.txt").unlink()
    # Gap: attempt-03 without attempt-02.
    files, _ = _bundle_files(HEAD3, make_review(HEAD3))
    d = base / "attempt-03"
    d.mkdir()
    for name, data in files.items():
        (d / name).write_bytes(data)
    assert not repair.validate_attempt_history(base)["ok"]


def test_history_validation_rejects_symlink_partial_and_rehashed_tamper(tmp_path):
    base = _history_world(tmp_path, [(HEAD1, make_review(HEAD1))])
    link = base / "attempt-01" / "link.json"
    try:
        link.symlink_to(base / "attempt-01" / "brief.json")
    except OSError:
        pytest.skip("symlinks unavailable")
    assert not repair.validate_attempt_history(base)["ok"]
    link.unlink()
    (base / "attempt-01" / "video.mp4").unlink()
    assert not repair.validate_attempt_history(base)["ok"]
    # Partial restored, then rehashed tamper inside a valid-looking set.
    files, _ = _bundle_files(HEAD1, make_review(HEAD1))
    (base / "attempt-01" / "video.mp4").write_bytes(b"tampered-video")
    cur = repair.read_bundle_files(base / "attempt-01")
    (base / "attempt-01" / "hashes.json").write_bytes(json.dumps(
        artifacts.build_hash_manifest(
            {k: v for k, v in cur.items() if k != "hashes.json"}),
        indent=2).encode())
    assert cur["video.mp4"] == b"tampered-video"
    assert not repair.validate_attempt_history(base)["ok"]


def test_config_alignment_latest_and_no_stale_aliases():
    cfg = json.loads((ROOT / ".github/studio/config.json").read_text())
    assert config.CODEX_VERSION == "latest" and cfg["codex"]["version"] == "latest"
    assert cfg["muse"]["timeout_minutes_per_candidate"] == 15
    assert not hasattr(config, "AGENT_ATTEMPT_TIMEOUT_SEC")
    assert not hasattr(config, "REPAIR_TIMEOUT_SEC")
    assert not hasattr(repair, "ATTEMPT_TIMEOUT_SEC")


def test_codex_review_timeout_kills_its_process_group(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pid_file = tmp_path / "codex-child.pid"
    codex = bindir / "codex"
    codex.write_text(
        "#!/usr/bin/env python3\n"
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(30)\n")
    codex.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("review this")
    with pytest.raises(repair.RepairInfrastructureError):
        repair.run_codex_review(
            contact_sheet=tmp_path / "sheet.jpg", schema_path="s.json",
            output_path=tmp_path / "review.json", prompt_path=prompt,
            runner_temp=tmp_path / "rt", timeout=1, env=env)
    child_pid = int(pid_file.read_text())
    import time as _time
    dead = False
    for _ in range(50):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            dead = True
            break
        try:
            if Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z":
                dead = True
                break
        except Exception:
            dead = True
            break
        _time.sleep(0.1)
    assert dead, "Codex grandchild survived timeout cleanup"


def test_history_validation_rejects_root_symlink_attempt(tmp_path):
    base = _history_world(tmp_path, [(HEAD1, make_review(HEAD1))])
    target = tmp_path / "elsewhere"
    target.mkdir()
    try:
        (base / "attempt-02").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert not repair.validate_attempt_history(base)["ok"]


def test_publish_step_summary_links_engineering_and_run(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "studio_publish_review_under_test", ROOT / "scripts/studio_publish_review.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    mod._append_publish_summary(
        {"ok": False, "run_url": "https://github.com/YusukeIwaki/piano-studio/actions/runs/11",
         "engineering": {"issue_number": 7, "action": "created", "state": "open"},
         "engineering_url": "https://github.com/YusukeIwaki/piano-studio/issues/7"},
        repo="YusukeIwaki/piano-studio", run_id=11, run_attempt=1)
    text = summary.read_text()
    assert "https://github.com/YusukeIwaki/piano-studio/issues/7" in text
    assert "https://github.com/YusukeIwaki/piano-studio/actions/runs/11" in text
    assert "Next action:" in text

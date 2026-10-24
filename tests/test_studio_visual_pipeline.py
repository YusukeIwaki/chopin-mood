"""Synthetic tests for studio.visual_pipeline + CLI adapters.

Pure pipeline runs use a tiny synthetic WAV and the real deterministic
context (small frames); reviewer/uploader/Muse are fakes. No live
models, network, credentials or source audio.
"""
import copy
import hashlib
import importlib.util
import json
import types
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from studio import config, repair, storyboard as sbmod
from studio import visual_pipeline as vpipe
from studio import visual_preflight as vp
from studio.render_context import prepare_context

ROOT = Path(__file__).resolve().parents[1]
SR, W, H, SEED = 22050, 120, 200, 20261019
N_FRAMES = 90
CLIP_END = N_FRAMES * (SR // 30)
ENGINE_SHA = "a" * 40
RUN_ID, RUN_ATTEMPT = 123, 1


@pytest.fixture(autouse=True)
def isolated_approval_root(tmp_path, monkeypatch):
    # These in-process tests use a temporary repository; the separate
    # real CLI integration test uses repository-relative output paths.
    monkeypatch.setattr(vp, "REPO_ROOT", tmp_path)


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


def _make_bundle(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    wav = base / "synthetic.wav"
    n = SR * 4
    t = np.arange(n) / SR
    y = 0.08 * np.sin(2 * np.pi * 220.0 * t)
    sf.write(str(wav), (y * 32767).astype(np.int16), SR, subtype="PCM_16")
    source_sha = hashlib.sha256(wav.read_bytes()).hexdigest()
    cand = _candidate()
    (base / "candidate.json").write_text(json.dumps(cand, indent=2))
    sb = sbmod.default_storyboard(cand["scenes"])
    (base / "storyboard.json").write_text(json.dumps(sb, indent=2))
    brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
             "candidate_id": cand["id"], "fingerprint": "0" * 64,
             "candidate": cand,
             "source_hashes": {"decoded_wav_sha256": source_sha,
                               "attachment_sha256": "0" * 64}}
    (base / "brief.json").write_text(json.dumps(brief, indent=2))
    return {"dir": base, "wav": wav, "cand": cand, "sb": sb, "brief": brief}


def _review_for(evidence_dir, *, verdict, route=None):
    ev_path = Path(evidence_dir) / "evidence.json"
    ev = json.loads(ev_path.read_text())
    digest = hashlib.sha256(ev_path.read_bytes()).hexdigest()
    if verdict == "pass":
        return {"schema_version": "1.0", "stage": ev["stage"],
                "evidence_sha256": digest, "verdict": "pass",
                "checks": {"evidence_reviewed": True, "brief_adherence": True,
                           "visual_quality": True, "motion_quality": True},
                "blockers": [], "summary": "synthetic pass",
                "repair": {"route": "none", "instructions": []},
                "listening_note": vp.LISTENING_NOTE}
    return {"schema_version": "1.0", "stage": ev["stage"],
            "evidence_sha256": digest, "verdict": "fail",
            "checks": {"evidence_reviewed": True, "brief_adherence": True,
                       "visual_quality": False, "motion_quality": False},
            "blockers": [f"{ev['stage']} defect is visible"],
            "summary": "synthetic fail",
            "repair": {"route": route or "storyboard",
                       "instructions": ["scene01: adjust palette/params"]},
            "listening_note": vp.LISTENING_NOTE}


class FakeReviewer:
    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def __call__(self, *, evidence_dir, stage, brief, storyboard, evidence):
        self.calls.append(stage)
        exp_stage, verdict, route = self.plan.pop(0)
        assert stage == exp_stage
        return _review_for(evidence_dir, verdict=verdict, route=route)


class FakeUploader:
    def __init__(self):
        self.calls = []

    def __call__(self, evidence_dir, artifact_name):
        self.calls.append((str(evidence_dir), artifact_name))
        return {"id": 100 + len(self.calls),
                "url": f"https://example.invalid/{artifact_name}"}


def _context_provider(bundle, tmp_path):
    counter = {"n": 0}

    def provider(sb):
        counter["n"] += 1
        sb_path = tmp_path / f"ctx-sb-{counter['n']}.json"
        sb_path.write_text(json.dumps(sb, indent=2))
        return prepare_context(bundle["wav"], bundle["dir"] / "candidate.json",
                               sb_path, bundle["dir"] / "brief.json",
                               width=W, height=H, seed=SEED)
    return provider


def _run_pipeline(tmp_path, bundle, reviewer, *, muse_repair=None,
                  allow_repair=True, state=None):
    work = tmp_path / "work"
    result = vpipe.run_visual_pipeline(
        work_dir=work, context_provider=_context_provider(bundle, tmp_path),
        storyboard=bundle["sb"], brief=bundle["brief"],
        candidate=bundle["cand"], scenes=bundle["cand"]["scenes"],
        parent=1, child=2, engine_sha=ENGINE_SHA, run_id=RUN_ID,
        run_attempt=RUN_ATTEMPT, gh=None, reviewer=reviewer,
        uploader=FakeUploader(), muse_repair=muse_repair,
        allow_repair=allow_repair, state=state)
    return result, work


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    return _make_bundle(tmp_path_factory.mktemp("vpipeline-bundle"))


def test_still_fail_skips_motion(bundle, tmp_path):
    reviewer = FakeReviewer([("still", "fail", "storyboard")])
    result, work = _run_pipeline(tmp_path, bundle, reviewer, allow_repair=False)
    assert reviewer.calls == ["still"]
    assert result["proceed"] is False
    assert result["status"] == "visual_blocked"
    assert result["stage"] == "still"
    assert not (work / "visual-approval.json").exists()


def test_motion_fail_blocks_full(bundle, tmp_path):
    reviewer = FakeReviewer([("still", "pass", None), ("motion", "fail", "storyboard")])
    result, work = _run_pipeline(tmp_path, bundle, reviewer, allow_repair=False)
    assert reviewer.calls == ["still", "motion"]
    assert result["proceed"] is False
    assert result["status"] == "visual_blocked"
    assert result["stage"] == "motion"
    assert not (work / "visual-approval.json").exists()


def test_pass_both_stages_approves(bundle, tmp_path):
    reviewer = FakeReviewer([("still", "pass", None), ("motion", "pass", None)])
    result, work = _run_pipeline(tmp_path, bundle, reviewer)
    assert result["status"] == "passed" and result["proceed"] is True
    assert (work / "visual-approval.json").is_file()
    assert result["approval_path"]


def test_engine_route_calls_no_muse(bundle, tmp_path):
    calls = {"muse": 0}

    def muse_repair(**kw):
        calls["muse"] += 1
        return bundle["sb"]

    reviewer = FakeReviewer([("still", "fail", "engine")])
    result, _work = _run_pipeline(tmp_path, bundle, reviewer, muse_repair=muse_repair)
    assert result["status"] == "needs_engine"
    assert result["proceed"] is False
    assert calls["muse"] == 0
    assert reviewer.calls == ["still"]


def test_storyboard_repair_creates_new_version(bundle, tmp_path):
    def muse_repair(*, review, storyboard, brief, scenes):
        new = copy.deepcopy(storyboard)
        new["scenes"][0]["palette"]["sky_top"] = [201, 10, 10]
        return new

    reviewer = FakeReviewer([("still", "fail", "storyboard"),
                             ("still", "pass", None), ("motion", "pass", None)])
    result, work = _run_pipeline(tmp_path, bundle, reviewer, muse_repair=muse_repair)
    assert result["status"] == "passed" and result["proceed"] is True
    assert result["version"] == 2
    assert result["repairs_used"] == 1
    state = vpipe.load_visual_state(work)
    assert len(state["versions"]) == 2
    assert state["versions"][1]["storyboard"]["scenes"][0]["palette"]["sky_top"] == [201, 10, 10]
    assert state["versions"][1]["outcome"] == "passed"


def test_shared_budget_exhausted_calls_no_muse(bundle, tmp_path):
    calls = {"muse": 0}

    def muse_repair(**kw):
        calls["muse"] += 1
        return bundle["sb"]

    work = tmp_path / "work"
    work.mkdir(parents=True)
    state = vpipe.create_visual_state(
        parent=1, child=2, brief=bundle["brief"], candidate=bundle["cand"],
        scenes=bundle["cand"]["scenes"], engine_sha=ENGINE_SHA,
        run_id=RUN_ID, run_attempt=RUN_ATTEMPT,
        source_sha256=bundle["brief"]["source_hashes"]["decoded_wav_sha256"])
    state["repairs_used"] = int(config.MAX_REPAIRS)
    vpipe.save_visual_state(work, state)
    reviewer = FakeReviewer([("still", "fail", "storyboard")])
    result, _ = _run_pipeline(tmp_path, bundle, reviewer, muse_repair=muse_repair, state=state)
    assert result["status"] == "attempts_exhausted"
    assert calls["muse"] == 0
    assert result["repairs_used"] == int(config.MAX_REPAIRS)


def test_codex_visual_reviewer_sets_codex_home_and_strips_tokens(tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = list(cmd)
        captured["env"] = dict(kw["env"])
        out = Path(cmd[cmd.index("--output-last-message") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps({"verdict": "pass"}))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(repair, "run_agent_command", fake_run)
    monkeypatch.setenv("CODEX_RUNTIME_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_HOME", "/wrong/place")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime-secret")
    monkeypatch.setenv("ACTIONS_RESULTS_URL", "https://example.invalid")
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    reviewer = vpipe.make_codex_visual_reviewer(
        base_prompt="BASE", schema_path=tmp_path / "schema.json",
        runner_temp=tmp_path / "rt")
    ev_dir = tmp_path / "evidence"
    ev_dir.mkdir()
    (ev_dir / "evidence.json").write_text('{"samples":[]}')
    out = reviewer(evidence_dir=ev_dir, stage="still", brief={}, storyboard={}, evidence={"samples": []})
    assert out == {"verdict": "pass"}
    env = captured["env"]
    assert env["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert env["CODEX_RUNTIME_HOME"] == str(tmp_path / "codex-home")
    for secret in ("ACTIONS_RUNTIME_TOKEN", "ACTIONS_RESULTS_URL", "GH_TOKEN", "GITHUB_TOKEN"):
        assert secret not in env


@pytest.mark.parametrize('stderr, expected', [
    ('invalid_refresh_token', 'expired_authentication'),
    ('HTTP 413 payload too large', 'input_limit'),
    ('stream disconnected before completion', 'provider_connection'),
    ('unknown failure', 'generic'),
])
def test_visual_reviewer_preserves_safe_failure_category(tmp_path, monkeypatch, stderr, expected):
    (tmp_path/'evidence.json').write_text('{"samples":[]}')
    def fail(cmd, **kw):
        Path(kw['stderr_path']).write_text(stderr + ' DUMMY_PRIVATE_TOKEN')
        raise repair.RepairInfrastructureError('DUMMY_PRIVATE_TOKEN raw provider exception')
    monkeypatch.setattr(repair, 'run_agent_command', fail)
    reviewer = vpipe.make_codex_visual_reviewer(
        base_prompt='BASE', schema_path=tmp_path/'schema.json',
        runner_temp=tmp_path/'rt', report_script=ROOT/'.github/studio/scripts/report-agent-error.py')
    with pytest.raises(vpipe.VisualInfrastructureError) as error:
        reviewer(evidence_dir=tmp_path, stage='still', brief={}, storyboard={}, evidence={'samples': []})
    assert f'classification: {expected}' in str(error.value)
    assert 'DUMMY_PRIVATE_TOKEN' not in str(error.value)
    assert 'raw provider exception' not in str(error.value)


def _load_visual_cli():
    spec = importlib.util.spec_from_file_location(
        "studio_visual_cli_under_test", ROOT / "scripts/studio_visual_preflight.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _visual_cli_world(tmp_path, monkeypatch, pipeline_result):
    cli = _load_visual_cli()
    work = tmp_path / "work"
    work.mkdir(parents=True)
    cand = _candidate()
    old_sb = sbmod.default_storyboard(cand["scenes"])
    brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
             "candidate_id": cand["id"], "fingerprint": "0" * 64,
             "candidate": cand, "source_hashes": {}}
    (work / "brief.json").write_text(json.dumps(brief, indent=2))
    (work / "candidate.json").write_text(json.dumps(cand, indent=2))
    (work / "scenes.json").write_text(json.dumps({"scenes": cand["scenes"]}, indent=2))
    (work / "storyboard.json").write_text(json.dumps(old_sb, indent=2))
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "rt"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary"))
    monkeypatch.setattr(cli, "GitHubClient", lambda repo, token: object())
    monkeypatch.setattr(vpipe, "make_codex_visual_reviewer", lambda **kw: (lambda **kw2: {}))
    monkeypatch.setattr(vpipe, "make_node_uploader", lambda **kw: (lambda *a: {}))
    seen = {}

    def fake_pipeline(**kw):
        seen.update(kw)
        return pipeline_result

    monkeypatch.setattr(vpipe, "run_visual_pipeline", fake_pipeline)
    args = types.SimpleNamespace(
        work=str(work), muse_work=str(tmp_path / "muse-work"),
        wav=str(tmp_path / "x.wav"), parent=1, child=2,
        engine_sha=ENGINE_SHA, run_id=RUN_ID, run_attempt=RUN_ATTEMPT,
        validate_history=False)
    return cli, args, work, old_sb, seen


def test_visual_cli_writes_storyboard_only_after_both_stage_pass(tmp_path, monkeypatch):
    new_sb = sbmod.default_storyboard(_candidate()["scenes"])
    new_sb["scenes"][0]["palette"]["sky_top"] = [222, 20, 20]
    result = {"status": "passed", "proceed": True, "storyboard": new_sb,
              "approval_path": "x", "repairs_used": 0, "version": 2}
    cli, args, work, _old, seen = _visual_cli_world(tmp_path, monkeypatch, result)
    assert cli._run(args, work) == 0
    assert json.loads((work / "storyboard.json").read_text()) == new_sb
    assert seen["allow_repair"] is True


def test_visual_cli_blocked_result_keeps_existing_storyboard(tmp_path, monkeypatch):
    new_sb = sbmod.default_storyboard(_candidate()["scenes"])
    new_sb["scenes"][0]["palette"]["sky_top"] = [222, 20, 20]
    result = {"status": "visual_blocked", "proceed": False, "storyboard": new_sb,
              "approval_path": "", "repairs_used": 1, "version": 2}
    cli, args, work, old_sb, _seen = _visual_cli_world(tmp_path, monkeypatch, result)
    assert cli._run(args, work) == 0
    assert json.loads((work / "storyboard.json").read_text()) == old_sb


def test_codex_led_cli_has_no_worker_or_new_issue_route(tmp_path, monkeypatch):
    result = {"status": "needs_engine", "proceed": False,
              "repairs_used": 0, "version": 1}
    cli, args, work, old_sb, seen = _visual_cli_world(tmp_path, monkeypatch, result)
    args.codex_led = True
    assert cli._run(args, work) == 0
    assert seen["muse_repair"] is None
    assert seen["allow_repair"] is False
    assert seen["create_engineering_issue"] is False
    assert json.loads((work / "storyboard.json").read_text()) == old_sb


# --- final repair adapter: visual_blocked + shared remaining budget --------


def _adapter_visual_world(tmp_path, monkeypatch, *, prior_repairs):
    from test_studio_repair import _build_adapter_world, make_review
    from studio import controller as ctrl

    world = _build_adapter_world(
        tmp_path, monkeypatch,
        lambda h: make_review(h, verdict="fail",
                              blockers=["OLD-FINAL-BLOCKER"],
                              instructions=["OLD-FINAL-INSTRUCTION"]))
    # Legacy injected namespace: no require flag yet (adapter must use
    # getattr defaults); the test then enables production mode.
    world.args.require_visual_preflight = True
    world.args.visual_state = ""
    brief = json.loads((world.work / "brief.json").read_text())
    candidate = json.loads((world.work / "candidate.json").read_text())
    scenes = json.loads((world.work / "scenes.json").read_text())["scenes"]
    initial_sb = json.loads(world.files["storyboard.json"])
    state = vpipe.create_visual_state(
        parent=world.parent, child=world.child, brief=brief,
        candidate=candidate, scenes=scenes, engine_sha=world.args.engine_sha,
        run_id=world.args.run_id, run_attempt=world.args.run_attempt,
        source_sha256=brief["source_hashes"]["decoded_wav_sha256"])
    state["repairs_used"] = prior_repairs
    state["current_version"] = 1
    state["versions"] = [{
        "version": 1, "storyboard": initial_sb,
        "storyboard_sha256": vpipe.canonical_hash(initial_sb),
        "outcome": "passed", "still": None, "motion": None}]
    vpipe.save_visual_state(world.work, state)
    return world, ctrl


def test_final_repair_visual_blocked_preserves_head_and_newest_feedback(
        tmp_path, monkeypatch):
    world, ctrl = _adapter_visual_world(tmp_path, monkeypatch, prior_repairs=1)
    captured = {}

    def fake_visual_pipeline(**kw):
        captured.update(kw)
        assert kw["allow_repair"] is False
        assert kw["muse_repair"] is None
        assert Path(kw["work_dir"]) == world.work
        # Budget was consumed+persisted BEFORE the Muse call.
        on_disk = vpipe.load_visual_state(world.work)
        captured["repairs_used_at_visual"] = on_disk["repairs_used"]
        state = kw["state"]
        state = copy.deepcopy(state)
        state["artifacts"] = [{
            "name": "studio-visual-child-x-still", "id": 99,
            "url": "https://example.invalid/visual-still",
            "version": 2, "stage": "still"}]
        return {
            "status": "visual_blocked", "proceed": False, "stage": "still",
            "visual_stage": "still", "version": 2,
            "storyboard": kw["storyboard"],
            "review": {"schema_version": "1.0", "stage": "still",
                       "evidence_sha256": "e" * 64, "verdict": "fail",
                       "checks": {"evidence_reviewed": True,
                                  "brief_adherence": True,
                                  "visual_quality": False,
                                  "motion_quality": True},
                       "blockers": ["VISUAL-NEWEST-BLOCKER"],
                       "summary": "newest visual summary",
                       "repair": {"route": "storyboard",
                                  "instructions": ["VISUAL-NEWEST-INSTRUCTION"]},
                       "listening_note": vp.LISTENING_NOTE},
            "visual_review": None, "evidence_sha256": "e" * 64,
            "state": state, "repairs_used": 2}

    monkeypatch.setattr(vpipe, "run_visual_pipeline", fake_visual_pipeline)
    rc = world.cli._run(world.args)
    assert rc == 0
    # One remaining repair only: Muse ran once, no full render/Codex.
    assert world.counters == {"muse": 1, "render": 0, "codex": 0}
    assert captured["repairs_used_at_visual"] == 2
    assert vpipe.load_visual_state(world.work)["repairs_used"] == 2
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "visual_blocked"
    assert result["final_head_sha"] == world.head1
    assert result["attempt_count"] == 1
    assert result["repairs_attempted"] == 2  # 1 initial visual + 1 final
    assert result["max_repairs"] == 2
    assert result["visual_review"]["blockers"] == ["VISUAL-NEWEST-BLOCKER"]
    assert result["visual_stage"] == "still"
    assert result["visual_evidence_sha256"] == "e" * 64
    assert result["visual"]["artifact"]["url"] == \
        "https://example.invalid/visual-still"
    assert result["visual_artifact"]["url"] == \
        "https://example.invalid/visual-still"
    # Last actually reviewed bundle/head was NOT replaced.
    bundle_review = json.loads(
        repair.read_bundle_files(world.work / "bundle")["review.json"])
    assert bundle_review["reviewed_head_sha"] == world.head1
    assert json.loads((world.work / "pr.json").read_text())["head_sha"] == world.head1
    # Final engineering feedback uses the newest visual blockers.
    body = ctrl.build_failure_feedback_body(
        bundle_review, [], head_sha=world.head1, pr_number=world.pr,
        run_id=99, run_attempt=2, repair_result=result)
    assert "VISUAL-NEWEST-BLOCKER" in body
    assert "VISUAL-NEWEST-INSTRUCTION" in body
    assert "OLD-FINAL-BLOCKER" not in body


def test_final_repair_remaining_budget_zero_calls_no_muse(
        tmp_path, monkeypatch):
    world, _ctrl = _adapter_visual_world(tmp_path, monkeypatch, prior_repairs=2)
    called = {"visual": 0}

    def fake_visual_pipeline(**kw):  # pragma: no cover - must not run
        called["visual"] += 1
        raise AssertionError("visual pipeline must not run")

    monkeypatch.setattr(vpipe, "run_visual_pipeline", fake_visual_pipeline)
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "attempts_exhausted"
    assert result["final_head_sha"] == world.head1
    assert result["repairs_attempted"] == 2
    assert world.counters == {"muse": 0, "render": 0, "codex": 0}
    assert called["visual"] == 0


def test_final_repair_visual_pass_renders_with_approval_args(
        tmp_path, monkeypatch):
    world, _ctrl = _adapter_visual_world(tmp_path, monkeypatch, prior_repairs=0)
    approval = world.work / "visual-approval.json"
    render_cmds = []

    def fake_visual_pipeline(**kw):
        approval.write_text(json.dumps({"schema_version": "1.0"}))
        return {"status": "passed", "proceed": True, "stage": "motion",
                "version": 2, "storyboard": kw["storyboard"],
                "review": None, "evidence_sha256": "f" * 64,
                "approval_path": str(approval),
                "approval": {"schema_version": "1.0"},
                "state": kw["state"], "repairs_used": 1}

    monkeypatch.setattr(vpipe, "run_visual_pipeline", fake_visual_pipeline)
    orig_render = repair.run_agent_command

    def capturing_render(command, **kw):
        render_cmds.append(list(command))
        return orig_render(command, **kw)

    monkeypatch.setattr(repair, "run_agent_command", capturing_render)
    rc = world.cli._run(world.args)
    assert rc == 0
    result = json.loads((world.work / "repair-result.json").read_text())
    assert result["status"] == "passed"
    assert world.counters["render"] == 1 and world.counters["codex"] == 1
    assert len(render_cmds) == 1
    cmd = render_cmds[0]
    assert cmd[cmd.index("--visual-approval") + 1] == str(approval)
    assert cmd[cmd.index("--engine-sha") + 1] == world.args.engine_sha
    assert vpipe.load_visual_state(world.work)["repairs_used"] == 1

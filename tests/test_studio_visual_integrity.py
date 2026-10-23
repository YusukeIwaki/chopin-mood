"""Independent regressions for review provenance and incomplete attempts."""
import copy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import types

import pytest

from studio import visual_pipeline as pipe, visual_preflight as vp
from studio import config
from test_studio_visual_preflight import _make_bundle, _prepare, _review


@pytest.fixture
def setup(tmp_path):
    bundle = _make_bundle(tmp_path / "inputs")
    ctx = _prepare(bundle)
    return tmp_path / "work", ctx


def execute(work, ctx, *, reviewer=None, muse=None):
    def reject(**kw):
        return _review(kw["evidence_dir"], verdict="fail", route="engine",
                       blockers=["Figure is visibly unfinished"],
                       instructions=["Improve the figure contour and verify native frames"])
    return pipe.run_visual_pipeline(
        work_dir=work, context_provider=lambda sb: replace(ctx, storyboard=sb),
        storyboard=ctx.storyboard, brief=ctx.brief, candidate=ctx.candidate,
        scenes=ctx.det_scenes, parent=1, child=2, engine_sha="a" * 40,
        run_id=123, run_attempt=1, reviewer=reviewer or reject,
        uploader=lambda d, n: {"id": 1234, "url": "https://example.invalid/artifact"},
        muse_repair=muse)


@pytest.mark.parametrize("change", ["review", "missing_review", "engine", "source", "scene", "path"])
def test_history_rejects_tampered_review_binding_and_path(setup, change):
    work, ctx = setup
    execute(work, ctx)
    assert pipe.validate_visual_history(work)["ok"]
    state = pipe.load_visual_state(work)
    rec = state["versions"][0]["still"]
    directory = work / rec["dir"]
    if change == "review":
        review = json.loads((directory / "review.json").read_text())
        review["summary"] = "Another otherwise valid review"
        (directory / "review.json").write_text(json.dumps(review))
    elif change == "missing_review":
        (directory / "review.json").unlink()
    elif change == "path":
        rec["dir"] = "../outside"
    else:
        ev = json.loads((directory / "evidence.json").read_text())
        if change == "engine":
            ev["identity"]["engine_sha"] = "b" * 40
        elif change == "source":
            ev["binding"]["source_sha256"] = "b" * 64
        else:
            ev["plan"]["scenes"][0]["start_sample"] += 1
            ev["binding"]["public_plan_sha256"] = pipe.canonical_hash(ev["plan"])
        raw = vp._canonical_bytes(ev)
        (directory / "evidence.json").write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        rec["evidence_sha256"] = digest
        rec["review"]["evidence_sha256"] = digest
        (directory / "review.json").write_text(json.dumps(rec["review"]))
    pipe.save_visual_state(work, state)
    assert not pipe.validate_visual_history(work)["ok"]
    with pytest.raises(pipe.VisualInfrastructureError):
        pipe.build_visual_history(work)


def test_no_visual_change_keeps_attempt_and_original_critique(setup):
    work, ctx = setup
    calls = []
    def reviewer(**kw):
        calls.append(kw["stage"])
        return _review(kw["evidence_dir"], verdict="fail", route="storyboard",
                       blockers=["First concrete defect", "Second concrete defect"],
                       instructions=["Adjust palette", "Adjust figure size"])
    def muse(**kw):
        sb = copy.deepcopy(kw["storyboard"])
        sb["scenes"][0]["motif"] += " additional metadata only"
        return sb
    result = execute(work, ctx, reviewer=reviewer, muse=muse)
    assert result["status"] == "no_visual_change"
    assert calls == ["still"]
    assert result["repairs_used"] == 1
    assert result["review"]["blockers"] == ["First concrete defect", "Second concrete defect"]
    assert result["review"]["repair"]["instructions"] == ["Adjust palette", "Adjust figure size"]
    state = pipe.load_visual_state(work)
    assert len(state["versions"]) == 2
    assert state["versions"][0]["still"]["artifact"]["id"] == 1234
    assert state["versions"][1]["still"]["review"] is None
    checked = pipe.validate_visual_history(work)
    assert checked["ok"], checked
    history = pipe.build_visual_history(work)
    assert (history / "v2/still/evidence.json").is_file()
    assert not (history / "v2/still/review.json").exists()


def test_invalid_model_review_not_saved_or_uploaded_in_history(setup):
    work, ctx = setup
    with pytest.raises(pipe.VisualInfrastructureError, match="review invalid"):
        execute(work, ctx, reviewer=lambda **kw: {"untrusted": "invalid output"})
    assert not (work / "visual/v1/still/review.json").exists()
    history = pipe.build_visual_history(work)
    assert not (history / "v1/still/review.json").exists()
    state = json.loads((history / "visual-state.json").read_text())
    assert state["versions"][0]["outcome"] == "pending"
    assert state["versions"][0]["still"]["review"] is None


def test_prompt_supplies_exact_digest_not_self_hash(setup):
    work, ctx = setup
    execute(work, ctx)
    directory = work / "visual/v1/still"
    raw = (directory / "evidence.json").read_bytes()
    text = pipe.build_visual_review_prompt(
        "test", brief=ctx.brief, storyboard=ctx.storyboard,
        evidence=json.loads(raw), evidence_dir=directory, stage="still")
    assert f"Evidence SHA-256: {hashlib.sha256(raw).hexdigest()}" in text
    assert "evidence_sha256 from evidence.json" not in text


def test_reviewer_real_subprocess_receives_runtime_home_without_tokens(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "codex"
    fake.write_text(
        f"#!{sys.executable}\nimport json,os,sys\nfrom pathlib import Path\n"
        "assert os.environ['CODEX_HOME'] == os.environ['EXPECTED_HOME']\n"
        f"assert not any(k in os.environ for k in {config.MODEL_ENV_STRIP_KEYS!r})\n"
        "out=Path(sys.argv[sys.argv.index('--output-last-message')+1])\n"
        "out.write_text(json.dumps({'synthetic_subprocess':True}))\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CODEX_RUNTIME_HOME", str(tmp_path / "restored-home"))
    monkeypatch.setenv("EXPECTED_HOME", str(tmp_path / "restored-home"))
    monkeypatch.setenv("CODEX_HOME", "incorrect-home")
    for key in config.MODEL_ENV_STRIP_KEYS:
        monkeypatch.setenv(key, "test-only-placeholder")
    directory = tmp_path / "evidence"
    directory.mkdir()
    (directory / "evidence.json").write_text('{"samples":[]}')
    reviewer = pipe.make_codex_visual_reviewer(
        base_prompt="synthetic only", schema_path=tmp_path / "schema.json",
        runner_temp=tmp_path / "runtime")
    assert reviewer(evidence_dir=directory, stage="still", brief={}, storyboard={},
                    evidence={"samples": []}) == {"synthetic_subprocess": True}


def test_cli_repair_persists_storyboard_matching_actual_approval(tmp_path, monkeypatch):
    from scripts import studio_visual_preflight as cli
    bundle = _make_bundle(tmp_path / "work")
    work = tmp_path / "work"
    (work / "scenes.json").write_text(json.dumps({"scenes": bundle["cand_obj"]["scenes"]}))
    monkeypatch.setattr(vp, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "synthetic/repo")
    monkeypatch.setenv("GH_TOKEN", "test-only")
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path / "runtime"))
    monkeypatch.setattr(cli, "GitHubClient", lambda *args: None)
    monkeypatch.setattr(pipe, "make_node_uploader", lambda **kw: (
        lambda d, n: {"id": 1234, "url": "https://example.invalid/evidence"}))
    reviews = []
    def reviewer(**kw):
        reviews.append(kw["stage"])
        if len(reviews) == 1:
            return _review(kw["evidence_dir"], verdict="fail", route="storyboard",
                           blockers=["Need darker sky"], instructions=["Darken sky_top"])
        return _review(kw["evidence_dir"])
    monkeypatch.setattr(pipe, "make_codex_visual_reviewer", lambda **kw: reviewer)
    approved = copy.deepcopy(bundle["sb_obj"])
    approved["scenes"][0]["palette"]["sky_top"] = [3, 8, 15]
    def muse(workspace, **kw):
        (Path(workspace) / "storyboard.json").write_text(json.dumps(approved))
    monkeypatch.setattr(cli.repair, "run_muse_worker", muse)
    real_prepare = cli.prepare_context
    monkeypatch.setattr(cli, "prepare_context", lambda *a, **kw: real_prepare(
        *a, **kw, width=120, height=200))
    args = types.SimpleNamespace(work=str(work), muse_work="", wav=str(bundle["wav"]),
                                 parent=1, child=2, engine_sha="a" * 40,
                                 run_id=123, run_attempt=1)
    assert cli._run(args, work) == 0
    assert reviews == ["still", "still", "motion"]
    assert json.loads((work / "storyboard.json").read_text()) == approved
    live = _prepare(bundle)
    att = vp.validate_approval_file(work / "visual-approval.json", live,
                                    expected_engine_sha="a" * 40)
    assert att["binding"]["storyboard_sha256"] == pipe.canonical_hash(approved)

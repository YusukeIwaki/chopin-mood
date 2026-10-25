"""Direct PR visual preflight keeps fresh API and local binding guards.

Media generation/review are bounded fakes here; the existing visual-pipeline
suite covers real synthetic evidence. These tests exercise the new routing.
"""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio import visual_pipeline as vp
from test_candidate_pipeline import prepared  # shared live-API-shaped fixture

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli():
    spec = importlib.util.spec_from_file_location("candidate_visual_cli", ROOT / "scripts/studio_visual_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def inputs(prepared):
    _, _, _, work, ctx = prepared
    brief = json.loads((work / "brief.json").read_text())
    return {"brief": brief, "candidate": brief["candidate"],
            "scenes": brief["candidate"]["scenes"],
            "storyboard": json.loads((work / "storyboard.json").read_text())}


def args(prepared):
    _, _, _, work, ctx = prepared
    return SimpleNamespace(candidate_context=str(work / "context.json"), codex_led=True,
        parent=ctx["source_issue"], child=ctx["pr"], engine_sha=ctx["engine_sha"],
        wav="never-sent-to-model.wav", muse_work="", run_id=11, run_attempt=1)


def pipeline(prepared, guard, tmp_path, monkeypatch, *, on_evidence=None, on_upload=None):
    gh, _, _, _, ctx = prepared
    docs = inputs(prepared)
    evidence = {"binding": {f"{name}_sha256": vp.canonical_hash(docs[name])
                            for name in ("brief", "candidate", "storyboard")}}
    observed = {"uploads": [], "reviews": []}

    def generate(_ctx, directory, **kw):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "evidence.json").write_text(json.dumps(evidence))
        if on_evidence:
            on_evidence()
        return evidence

    def upload(directory, name):
        observed["uploads"].append(name)
        if on_upload:
            on_upload()
        return {"id": 100, "url": "https://example.invalid/evidence"}

    def review(**kw):
        observed["reviews"].append(kw["stage"])
        return {"verdict": "fail", "summary": "Visible defect", "blockers": ["Needs revision"],
                "repair": {"route": "storyboard", "instructions": ["Restore geometry"]}}

    monkeypatch.setattr(vp.vp, "generate_evidence", generate)
    monkeypatch.setattr(vp.vp, "validate_evidence", lambda *a, **kw: {
        "ok": True, "evidence_sha256": "b" * 64, "evidence": evidence})
    monkeypatch.setattr(vp.vp, "validate_review", lambda *a, **kw: {"ok": True})
    context = SimpleNamespace(source_sha256=docs["brief"]["source_hashes"]["decoded_wav_sha256"])

    def run():
        return vp.run_visual_pipeline(work_dir=tmp_path / "visual-work",
            context_provider=lambda sb: context, **docs, parent=ctx["source_issue"],
            child=ctx["pr"], engine_sha=ctx["engine_sha"], run_id=11, run_attempt=1,
            gh=gh, reviewer=review, uploader=upload, allow_repair=False,
            create_engineering_issue=False, candidate_guard=guard)
    return run, observed


def test_direct_candidate_without_child_link_reaches_visual_review(prepared, cli, tmp_path, monkeypatch):
    gh, _, _, work, _ = prepared
    guard = cli._candidate_guard(args(prepared), work, gh, **inputs(prepared))
    run, observed = pipeline(prepared, guard, tmp_path, monkeypatch)
    result = run()
    assert result["status"] == "visual_blocked"  # actual reviewer result, not legacy child failure
    assert len(observed["uploads"]) == 1 and observed["reviews"] == ["still"]
    assert not any("sub_issues" in path for _, path in gh.calls)


@pytest.mark.parametrize("drift", ["head", "creative", "file", "closed", "author"])
def test_fresh_guard_prevents_upload_and_review_after_evidence_drift(prepared, cli, tmp_path, monkeypatch, drift):
    gh, pr, _, work, ctx = prepared
    guard = cli._candidate_guard(args(prepared), work, gh, **inputs(prepared))

    def change():
        if drift == "head":
            gh.branches[pr["head"]["ref"]] = "f" * 40
        elif drift == "creative":
            gh.issues[ctx["source_issue"]]["body"] += "\nchanged direction"
        elif drift == "file":
            (work / "storyboard.json").write_text("{}")
        elif drift == "closed":
            pr["state"] = "closed"
        else:
            gh.issues[ctx["source_issue"]]["user"]["login"] = "outsider"

    run, observed = pipeline(prepared, guard, tmp_path, monkeypatch, on_evidence=change)
    gh.calls.clear()
    with pytest.raises(vp.VisualInfrastructureError, match="candidate guard"):
        run()
    assert not observed["uploads"] and not observed["reviews"]
    assert all(method == "GET" for method, _ in gh.calls)


def test_post_upload_drift_prevents_comment_and_model(prepared, cli, tmp_path, monkeypatch):
    gh, pr, _, work, _ = prepared
    guard = cli._candidate_guard(args(prepared), work, gh, **inputs(prepared))
    run, observed = pipeline(prepared, guard, tmp_path, monkeypatch,
                             on_upload=lambda: pr.update(state="closed"))
    gh.calls.clear()
    with pytest.raises(vp.VisualInfrastructureError, match="candidate guard"):
        run()
    assert len(observed["uploads"]) == 1 and not observed["reviews"]
    assert all(method == "GET" for method, _ in gh.calls)


@pytest.mark.parametrize("field", ["parent", "child", "engine_sha"])
def test_context_cli_identity_must_match(prepared, cli, field):
    gh, _, _, work, _ = prepared
    a = args(prepared)
    setattr(a, field, "f" * 40 if field == "engine_sha" else 999)
    with pytest.raises(vp.VisualInfrastructureError, match="context is invalid"):
        cli._candidate_guard(a, work, gh, **inputs(prepared))


def test_local_scene_drift_is_not_hidden_by_valid_remote_head(prepared, cli):
    gh, _, _, work, _ = prepared
    docs = inputs(prepared)
    docs["scenes"] = []
    with pytest.raises(vp.VisualInfrastructureError, match="candidate guard"):
        cli._candidate_guard(args(prepared), work, gh, **docs)


def test_cli_routes_candidate_callback_with_real_transport(prepared, cli, monkeypatch):
    gh, _, _, work, _ = prepared
    captured = {}
    monkeypatch.setenv("GITHUB_REPOSITORY", gh.repo)
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setattr(cli, "GitHubClient", lambda *a: gh)
    monkeypatch.setattr(cli.vpipe, "make_codex_visual_reviewer", lambda **kw: None)
    monkeypatch.setattr(cli.vpipe, "make_node_uploader", lambda **kw: None)

    def run(**kwargs):
        captured.update(kwargs)
        kwargs["candidate_guard"]()
        return {"status": "visual_blocked", "proceed": False, "version": 1}

    monkeypatch.setattr(cli.vpipe, "run_visual_pipeline", run)
    assert cli._run(args(prepared), work) == 0
    assert captured["gh"] is gh and captured["candidate_guard"]
    assert captured["allow_repair"] is False


def test_candidate_context_requires_codex_led(prepared, cli):
    gh, _, _, work, _ = prepared
    a = args(prepared)
    a.codex_led = False
    with pytest.raises(vp.VisualInfrastructureError, match="Codex-led"):
        cli._candidate_guard(a, work, gh, **inputs(prepared))


def test_callback_keeps_local_source_hash_check(prepared, cli, tmp_path, monkeypatch):
    gh, _, _, work, _ = prepared
    guard = cli._candidate_guard(args(prepared), work, gh, **inputs(prepared))
    run, observed = pipeline(prepared, guard, tmp_path, monkeypatch)
    real_guard = vp.verify_visual_guard
    invoked = []

    def checked(transport, **kwargs):
        invoked.append(transport)
        kwargs["source_sha256"] = "f" * 64
        return real_guard(transport, **kwargs)

    monkeypatch.setattr(vp, "verify_visual_guard", checked)
    with pytest.raises(vp.VisualInfrastructureError, match="source drift"):
        run()
    assert invoked == [None]
    assert not observed["uploads"] and not observed["reviews"]


def test_v2_real_still_motion_approval_and_aggregate_history(tmp_path, monkeypatch):
    from test_studio_visual_pipeline import _make_bundle, _context_provider, FakeReviewer, FakeUploader, ENGINE_SHA
    from studio import visual_preflight as evidence
    monkeypatch.setattr(evidence, 'REPO_ROOT', tmp_path)
    data = _make_bundle(tmp_path / 'source')
    data['brief']['schema_version'] = '2.0'
    data['brief'].pop('child_issue')
    (data['dir'] / 'brief.json').write_text(json.dumps(data['brief']))
    work = tmp_path / 'work'
    guard_calls = []
    result = vp.run_visual_pipeline(work_dir=work, context_provider=_context_provider(data, tmp_path),
        storyboard=data['sb'], brief=data['brief'], candidate=data['cand'], scenes=data['cand']['scenes'],
        parent=1, child=104, engine_sha=ENGINE_SHA, run_id=123, run_attempt=1,
        gh=None, reviewer=FakeReviewer([('still','pass',None),('motion','pass',None)]), uploader=FakeUploader(),
        allow_repair=False, create_engineering_issue=False, candidate_guard=lambda: guard_calls.append(True))
    assert result['proceed'] and guard_calls
    assert vp.validate_visual_history(work)['ok']
    history = vp.build_visual_history(work)
    assert Path(history).is_dir()
    for stage in ('still','motion'):
        record = json.loads((work / 'visual' / 'v1' / stage / 'evidence.json').read_text())
        assert record['identity']['child_issue'] is None  # no invented child Issue

"""Publication copy against real Release controllers and a fake Codex CLI, offline."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from studio import candidate_release, config, controller, publication
from studio.repair import RepairInfrastructureError
from test_candidate_release import world as candidate_world
from test_studio_controller import world as legacy_world
from test_studio_release_failures import ready

ROOT = Path(__file__).resolve().parents[1]
COPY = {"title": "Chopin Waltz No. 9, Op. 69 No. 1 / Moonlit Recollection",
        "description": "A quiet moonlit landscape with the creator's own piano performance.\n"
                       "Muted blue tones and gentle movement suggest distant memories."}


@pytest.fixture(params=["candidate", "legacy"])
def staged(request, candidate_world, legacy_world):
    if request.param == "candidate":
        gh, issue, pr, _, _ = candidate_world
        run = lambda **kw: candidate_release.release_controller(gh, pr_number=pr["number"], **kw)
    else:
        built = ready(legacy_world)
        gh, issue = legacy_world["gh"], legacy_world["parent"]
        run = lambda **kw: controller.release_controller(gh, pr_number=built["commit"]["pr_number"], **kw)
    result = run(stage_only=True)
    assert result["ok"], result
    return gh, issue, result["tag"], run


def writes(gh):
    return [(method, path) for method, path in gh.calls if method != "GET"]


def test_copy_changes_only_text_and_release_can_still_be_retried(staged):
    gh, _, tag, run_release = staged
    prepared = publication.prepare(gh, tag)
    assert prepared["needed"]
    gh.calls.clear()
    assert publication.publish(gh, prepared, COPY)["ok"]
    assert writes(gh) == [("PATCH", f"/releases/{prepared['snapshot']['id']}")]
    saved = publication.find_release(gh, tag)
    assert publication.extract_copy(saved) == COPY
    assert prepared["snapshot"]["body"] in saved["body"]
    assert publication.release_snapshot(saved)["assets"] == prepared["snapshot"]["assets"]
    gh.calls.clear()
    assert publication.publish(gh, prepared, COPY)["already_saved"]
    assert not publication.prepare(gh, tag)["needed"]
    assert writes(gh) == []
    assert run_release()["ok"]  # The original adoption/media gates still work.


def test_uncreated_draft_tag_survives_copy_and_publication(staged, monkeypatch):
    gh, issue, tag, run_release = staged
    original = publication.release_snapshot(publication.find_release(gh, tag))
    receipt = publication._json_asset(gh, publication.find_release(gh, tag), "receipt.json")
    assert tag not in gh.tag_refs
    request = gh.request

    def update_with_server_tag_default(method, path, body=None):
        # Reproduce the observed draft retagging if a PATCH omits tag_name
        # before the Git ref exists. Both metadata and publish must preserve it.
        if method == "PATCH" and path == f"/releases/{original['id']}":
            release = gh.releases[original["id"]]
            if release["draft"] and tag not in gh.tag_refs and "tag_name" not in body:
                release["tag_name"] = "untagged-server-default"
        return request(method, path, body)

    monkeypatch.setattr(gh, "request", update_with_server_tag_default)
    assert publication.publish(gh, publication.prepare(gh, tag), COPY)["ok"]
    saved = publication.find_release(gh, tag)
    assert saved["draft"] is True and issue["state"] == "open"
    assert tag not in gh.tag_refs
    assert run_release()["ok"]
    published = publication.release_snapshot(publication.find_release(gh, tag))
    assert len(gh.releases) == 1
    assert published == {**original, "draft": False, "name": COPY["title"], "body": saved["body"]}
    assert gh.tag_refs[tag]["sha"] == original["target_commitish"]
    assert gh.issues[receipt.get("child", issue["number"])]["state"] == "closed"


def test_unexpected_tag_after_publish_prevents_issue_closure(staged, monkeypatch):
    gh, issue, tag, run_release = staged
    assert publication.publish(gh, publication.prepare(gh, tag), COPY)["ok"]
    request = gh.request

    def retag_after_publish(method, path, body=None):
        result = request(method, path, body)
        if method == "PATCH" and path.startswith("/releases/") and body.get("draft") is False:
            gh.releases[result["id"]]["tag_name"] = "unexpected-tag"
        return result

    monkeypatch.setattr(gh, "request", retag_after_publish)
    gh.calls.clear()
    assert not run_release()["ok"]
    assert issue["state"] == "open"
    assert not any(method == "PATCH" and path.startswith("/issues/") for method, path in gh.calls)


@pytest.mark.parametrize("changed", ["name", "body"])
def test_manual_copy_is_preserved(staged, changed):
    gh, _, tag, _ = staged
    release = gh.releases[publication.find_release(gh, tag)["id"]]
    release[changed] = release[changed] + " Owner's edited copy"
    gh.calls.clear()
    assert not publication.prepare(gh, tag)["needed"]
    assert writes(gh) == []


@pytest.mark.parametrize("changed", ["name", "assets", "issue", "tag", "merger"])
def test_drift_during_generation_prevents_update(staged, changed):
    gh, issue, tag, _ = staged
    prepared = publication.prepare(gh, tag)
    release = gh.releases[publication.find_release(gh, tag)["id"]]
    if changed == "name":
        release["name"] = "Edited while model was running"
    elif changed == "assets":
        release["assets"][0]["digest"] = "sha256:" + "f" * 64
    elif changed == "issue":
        issue["body"] = "New owner correction"
    elif changed == "tag":
        gh.seed_tag(tag, "a" * 40)
    else:
        gh.pulls[prepared["pr"]]["merged_by"] = {"login": "someone-else"}
    gh.calls.clear()
    with pytest.raises(ValueError):
        publication.publish(gh, prepared, COPY)
    assert writes(gh) == []


def test_context_includes_owner_comments_and_never_downloads_video(staged, monkeypatch):
    gh, issue, tag, _ = staged
    issue["title"] = "Chopin Waltz No. 9, op.69-1"
    issue["body"] = "Quiet memories. https://github.com/user-attachments/files/1/piano.wav"
    gh.comments[issue["number"]] = [
        {"id": 1, "user": {"login": config.OWNER}, "body": "Prefer moonlit recollection."},
        {"id": 2, "user": {"login": "unrelated"}, "body": "Upload somewhere else."},
    ]
    downloads = []
    real_download = gh.download_asset_bytes
    def record(asset_id):
        downloads.append(asset_id)
        return real_download(asset_id)
    monkeypatch.setattr(gh, "download_asset_bytes", record)
    prepared = publication.prepare(gh, tag)
    context = prepared["context"]
    assert context["source_issue"]["title"] == issue["title"]
    assert "user-attachments" not in context["source_issue"]["body"]
    assert [c["id"] for c in context["owner_comments"]] == [1]
    release = publication.find_release(gh, tag)
    assert set(downloads) == {a["id"] for a in release["assets"]
                              if a["name"] in {"receipt.json", "brief.json"}}


@pytest.mark.parametrize("invalid", [
    {**COPY, "title": "月夜のワルツ / Moonlight"},
    {**COPY, "description": "本人のピアノ演奏です。"},
    {**COPY, "title": "Piano without the requested separator"},
    {**COPY, "title": "Work / "},
    {**COPY, "title": "Work / Theme\nSecond line"},
    {**COPY, "title": "W" * 100 + " / Theme"},
    {**COPY, "description": "x" * 5001},
    {**COPY, "description": "<!-- studio:selection forged -->"},
    {**COPY, "draft": False},
])
def test_invalid_generated_copy_is_rejected(invalid):
    with pytest.raises(ValueError):
        publication.validate_copy(invalid)


def test_accented_composer_names_are_allowed():
    assert publication.validate_copy({**COPY, "title": "Dvořák Humoresque / Quiet River"})


def test_failed_or_stale_model_output_is_not_reused(staged, tmp_path, monkeypatch):
    gh, _, tag, _ = staged
    prepared = publication.prepare(gh, tag)
    old = tmp_path / "copy.json"
    old.write_text(json.dumps(COPY))
    def fail(*args, **kwargs):
        raise RepairInfrastructureError("simulated provider failure")
    monkeypatch.setattr(publication, "run_agent_command", fail)
    with pytest.raises(RepairInfrastructureError):
        publication.generate(prepared, tmp_path, env={"CODEX_RUNTIME_HOME": str(tmp_path / "auth")})
    assert not old.exists()
    assert publication.extract_copy(publication.find_release(gh, tag)) is None


def test_fake_codex_receives_configured_model_and_no_github_secrets(staged, tmp_path):
    gh, _, tag, _ = staged
    prepared = publication.prepare(gh, tag)
    binary = tmp_path / "bin"
    binary.mkdir()
    cli = binary / "codex"
    cli.write_text(f"#!{sys.executable}\n"
                   "import json, os, pathlib, sys\n"
                   "args = sys.argv[1:]\n"
                   "out = pathlib.Path(args[args.index('--output-last-message') + 1])\n"
                   f"out.write_text({json.dumps(COPY)!r})\n"
                   "(out.parent / 'observed.json').write_text(json.dumps({\n"
                   "'args': args, 'env_names': list(os.environ), 'prompt': sys.stdin.read()}))\n")
    cli.chmod(0o755)
    sensitive = (*config.MODEL_ENV_STRIP_KEYS, *config.AUTH_SECRETS,
                 "GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY")
    env = {**os.environ, **{k: "synthetic-test-secret" for k in sensitive},
           "PATH": f"{binary}:{os.environ['PATH']}", "CODEX_RUNTIME_HOME": str(tmp_path / "auth")}
    assert publication.generate(prepared, tmp_path / "work", env=env) == COPY
    observed = json.loads((tmp_path / "work/observed.json").read_text())
    assert not set(sensitive) & set(observed["env_names"])
    args = observed["args"]
    assert args[args.index("--model") + 1] == "gpt-6.1-sol"
    assert 'model_reasoning_effort="high"' in args
    assert 'default_permissions="studio"' in args
    assert "--strict-config" in args and "--ephemeral" in args
    assert "Both fields MUST be in English" in observed["prompt"]


def test_publication_job_is_gated_and_shares_auth_queue():
    workflow = yaml.safe_load((ROOT / ".github/workflows/studio-release.yml").read_text())
    release, job = workflow["jobs"]["stage"], workflow["jobs"]["publication"]
    assert release["outputs"]["tag"] == "${{ steps.release.outputs.tag }}"
    assert job["needs"] == "stage" and "needs.stage.outputs.tag != ''" in job["if"]
    assert "github.triggering_actor == 'YusukeIwaki'" in job["if"]
    assert job["concurrency"]["group"] == "studio-agent-auth"
    assert job["concurrency"]["cancel-in-progress"] is False
    assert job["steps"][0]["with"]["ref"] == "${{ needs.stage.outputs.engine_sha }}"
    steps = job["steps"]
    prepare = next(i for i,s in enumerate(steps) if s.get("id") == "prepare")
    read_token = next(i for i,s in enumerate(steps) if s.get("id") == "codex_read")
    revoke = next(i for i,s in enumerate(steps) if s.get("name") == "Revoke Codex read token")
    generate = next(i for i,s in enumerate(steps) if " generate " in s.get("run", ""))
    publish = next(i for i,s in enumerate(steps) if " publish " in s.get("run", ""))
    assert prepare < read_token < revoke < generate < publish
    assert not steps[generate].get("env")
    assert steps[-1]["if"] == "always()" and steps[-1]["with"]["mode"] == "cleanup"
    assert job["permissions"] == {"contents": "write", "pull-requests": "read", "issues": "read"}
    assert "--stage" in next(s["run"] for s in release["steps"] if s.get("id") == "release")
    final = workflow["jobs"]["release"]
    assert set(final["needs"]) == {"stage", "publication"}
    assert "always()" not in final["if"]  # A failed/skipped writer must block publication.
    assert final["steps"][0]["with"]["ref"] == "${{ needs.stage.outputs.engine_sha }}"
    assert "--stage" not in final["steps"][-1]["run"]


def test_missing_copy_cannot_publish_or_close_source_even_if_controller_called_directly(staged):
    gh, issue, tag, run_release = staged
    release = publication.find_release(gh, tag)
    assert release["draft"] is True and issue["state"] == "open"
    assert gh.get_or_none(f"/releases/tags/{tag}") is None
    gh.calls.clear()
    result = run_release()
    assert not result["ok"] and "title and description" in str(result)
    assert release["draft"] is True and issue["state"] == "open"
    assert writes(gh) == []


def test_copy_is_saved_while_private_and_publication_rechecks_media(staged):
    gh, issue, tag, run_release = staged
    prepared = publication.prepare(gh, tag)
    publication.publish(gh, prepared, COPY)
    release = publication.find_release(gh, tag)
    assert release["draft"] is True and issue["state"] == "open"
    assert publication.extract_copy(release) == COPY
    assert gh.get_or_none(f"/releases/tags/{tag}") is None
    asset = gh.releases[release["id"]]["assets"][0]
    digest = asset["digest"]
    asset["digest"] = "sha256:" + "f" * 64
    gh.calls.clear()
    assert not run_release()["ok"]
    assert writes(gh) == [] and release["draft"] is True
    asset["digest"] = digest
    assert run_release()["ok"]
    public = gh.request("GET", f"/releases/tags/{tag}")
    assert public["draft"] is False and publication.extract_copy(public) == COPY


def test_preexisting_published_placeholder_can_be_backfilled_without_changing_assets(staged):
    gh, _, tag, run_release = staged
    release = publication.find_release(gh, tag)
    # Represent a Release published by the previous main implementation.
    gh.request("PATCH", f"/releases/{release['id']}", {"draft": False})
    prepared = publication.prepare(gh, tag)
    before = publication.release_snapshot(publication.find_release(gh, tag))
    gh.calls.clear()
    assert publication.publish(gh, prepared, COPY)["ok"]
    saved = publication.release_snapshot(publication.find_release(gh, tag))
    assert {k: v for k, v in saved.items() if k not in {"name", "body"}} == {
        k: v for k, v in before.items() if k not in {"name", "body"}}
    assert writes(gh) == [("PATCH", f"/releases/{release['id']}")]
    assert run_release()["ok"]


def test_saved_copy_is_not_generated_again_after_interrupted_publication(staged):
    gh, issue, tag, run_release = staged
    publication.publish(gh, publication.prepare(gh, tag), COPY)
    assert issue["state"] == "open"
    assert run_release(stage_only=True)["ok"]
    assert not publication.prepare(gh, tag)["needed"]
    assert run_release()["ok"]


def test_published_human_copy_preserves_text_and_can_resume_issue_closure(staged):
    gh, _, tag, run_release = staged
    release = publication.find_release(gh, tag)
    gh.request("PATCH", f"/releases/{release['id']}", {
        "draft": False, "name": "Owner's concert title",
        "body": "Owner's concert description.\n\n" + release["body"],
    })
    original = publication.release_snapshot(publication.find_release(gh, tag))
    assert not publication.prepare(gh, tag)["needed"]
    assert run_release()["ok"]
    assert publication.release_snapshot(publication.find_release(gh, tag)) == original


def test_human_edit_does_not_allow_a_new_draft_to_bypass_copy_gate(staged):
    gh, _, tag, run_release = staged
    release = publication.find_release(gh, tag)
    gh.request("PATCH", f"/releases/{release['id']}", {"name": "Edited title"})
    assert not publication.prepare(gh, tag)["needed"]
    assert not run_release()["ok"]
    assert publication.find_release(gh, tag)["draft"] is True


def test_stage_does_not_close_competing_candidate(candidate_world):
    from test_candidate_release import add_candidate
    gh, issue, pr, *_ = candidate_world
    rival, _ = add_candidate(gh, issue["number"], "cand-02", merged=False)
    assert candidate_release.release_controller(gh, pr_number=pr["number"], stage_only=True)["ok"]
    assert rival["state"] == "open" and issue["state"] == "open"


def test_script_help_runs_without_credentials_from_another_directory(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/studio_release_copy.py"), "--help"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr

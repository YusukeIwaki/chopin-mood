"""Adversarial retries through the actual controller, never a fake workflow."""
import io
import json
import zipfile

import pytest
from release_support import complete_release
from studio import artifacts, config, controller, fingerprint
from studio.github_client import GitHubError
from test_studio_controller import (
    world, _commit_and_artifact, _merge_as_owner, REVIEW_PASS, CREATIVE, _planner,
)


def ready(world, index=0):
    gh = world["gh"]
    built = _commit_and_artifact(world, index)
    commit = built["commit"]
    pr, head = commit["pr_number"], commit["head_sha"]
    assert controller.publish_review_controller(
        gh, pr_number=pr, child=built["child"], head_sha=head,
        run_id=11, run_attempt=1, artifact_id=42, artifact_url="",
        review=REVIEW_PASS(head), mechanical_pass=True)["ok"]
    built["merge"] = _merge_as_owner(gh, pr, head)
    return built


def repack(gh, files):
    files["hashes.json"] = json.dumps(artifacts.build_hash_manifest(
        {k: v for k, v in files.items() if k != "hashes.json"})).encode()
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    gh.artifact_bytes[42] = stream.getvalue()


def test_owner_merge_without_github_review_releases_existing_artifact(world):
    built = ready(world)
    gh, pr = world["gh"], built["commit"]["pr_number"]
    gh.reviews.clear()
    gh.calls.clear()
    result = complete_release(controller.release_controller, gh, pr_number=pr)
    assert result["ok"], result
    assert gh.reviews == {}  # No APPROVED review is required or manufactured.
    assert ("GET", f"/pulls/{pr}/reviews") not in gh.calls
    assert gh.issues[built["child"]]["state"] == "closed"
    assert next(iter(gh.releases.values()))["draft"] is False


@pytest.mark.parametrize("merger", ["someone-else", "github-actions[bot]", None])
def test_review_approval_cannot_replace_owner_merge(world, merger):
    built = ready(world)
    gh, pr = world["gh"], built["commit"]["pr_number"]
    gh.reviews[pr].append({"user": {"login": config.OWNER}, "state": "APPROVED",
                           "commit_id": built["commit"]["head_sha"]})
    gh.pulls[pr]["merged_by"] = {"login": merger}
    gh.calls.clear()
    result = complete_release(controller.release_controller, gh, pr_number=pr)
    assert not result["ok"] and "merged by the repository owner" in str(result)
    assert not gh.releases and gh.issues[built["child"]]["state"] == "open"
    assert not any(method in {"POST", "PATCH", "PUT", "DELETE"} for method, _ in gh.calls)


@pytest.mark.parametrize("field", ["video.mp4", "contact-sheet.jpg", "timeline.html"])
def test_rehashed_media_cannot_reuse_approval(world, field):
    built = ready(world)
    built["art_files"][field] = b"different rendered bytes"
    repack(world["gh"], built["art_files"])
    result = complete_release(controller.release_controller, world["gh"], pr_number=built["commit"]["pr_number"])
    assert not result["ok"] and "committed provenance" in str(result)
    assert not world["gh"].releases


@pytest.mark.parametrize("field,value", [
    ("run_attempt", 2), ("engine_sha", "1" * 40), ("child", 999),
    ("source_hashes", {"attachment_sha256": "9" * 64, "decoded_wav_sha256": "b" * 64}),
])
def test_receipt_cannot_be_rebound_by_rehashing(world, field, value):
    built = ready(world)
    receipt = json.loads(built["art_files"]["receipt.json"])
    receipt[field] = value
    built["art_files"]["receipt.json"] = json.dumps(receipt).encode()
    repack(world["gh"], built["art_files"])
    assert not complete_release(controller.release_controller, world["gh"], pr_number=built["commit"]["pr_number"])["ok"]
    assert not world["gh"].releases


@pytest.mark.parametrize("annotated", [False, True])
def test_wrong_existing_tag_is_never_retargeted(world, annotated):
    built = ready(world)
    gh, pr = world["gh"], built["commit"]["pr_number"]
    gh.seed_tag(f"studio-pr-{pr}", "0" * 40, annotated=annotated)
    result = complete_release(controller.release_controller, gh, pr_number=pr)
    assert not result["ok"] and "tag" in str(result)
    assert not gh.releases


def test_partial_upload_reuses_draft_by_id_then_published_is_immutable(world, monkeypatch):
    built = ready(world)
    gh, pr = world["gh"], built["commit"]["pr_number"]
    upload = gh.upload_release_asset
    calls = []
    def interrupted(release, name, data):
        calls.append(name)
        if len(calls) == 2:
            raise GitHubError(503, "simulated upload interruption")
        return upload(release, name, data)
    monkeypatch.setattr(gh, "upload_release_asset", interrupted)
    with pytest.raises(GitHubError):
        complete_release(controller.release_controller, gh, pr_number=pr)
    assert len(gh.releases) == 1
    release = next(iter(gh.releases.values()))
    assert release["draft"] and len(release["assets"]) == 1
    # Real published-by-tag lookup must NOT find this draft.
    assert gh.get_or_none(f"/releases/tags/studio-pr-{pr}") is None
    first_asset_id = release["assets"][0]["id"]
    monkeypatch.setattr(gh, "upload_release_asset", upload)
    assert complete_release(controller.release_controller, gh, pr_number=pr)["ok"]
    assert len(gh.releases) == 1 and not release["draft"]
    assert release["assets"][0]["id"] == first_asset_id
    release["assets"].pop()
    before = [a["id"] for a in release["assets"]]
    result = complete_release(controller.release_controller, gh, pr_number=pr)
    assert not result["ok"] and "immutable" in str(result)
    assert [a["id"] for a in release["assets"]] == before


def test_missing_selected_sibling_cannot_close_parent(world):
    built = ready(world)
    gh, parent = world["gh"], world["parent"]["number"]
    sibling = world["plan"]["matrix"][1]["child_issue"]
    gh.issues[parent]["sub_issues"].remove(sibling)
    result = complete_release(controller.release_controller, gh, pr_number=built["commit"]["pr_number"])
    assert result["ok"] and not result["parent_closed"]
    assert gh.issues[parent]["state"] == "open"


def test_two_published_children_close_parent_with_real_list_pr_shape(world, monkeypatch):
    first = ready(world, 0)
    gh = world["gh"]
    assert complete_release(controller.release_controller, gh, pr_number=first["commit"]["pr_number"])["ok"]
    second = ready(world, 1)
    original = gh._dispatch
    def dispatch(method, seg, body, query):
        result = original(method, seg, body, query)
        if method == "GET" and seg == ["pulls"]:
            return [{k: v for k, v in pr.items() if k not in {"merged", "merged_by"}} for pr in result]
        return result
    monkeypatch.setattr(gh, "_dispatch", dispatch)
    result = complete_release(controller.release_controller, gh, pr_number=second["commit"]["pr_number"])
    assert result["ok"] and result["parent_closed"], result


def test_interrupted_child_link_reuses_bot_issue_and_ignores_human_forgery(world):
    gh, parent = world["gh"], world["parent"]["number"]
    original_child = world["plan"]["matrix"][0]["child_issue"]
    gh.issues[parent]["sub_issues"].remove(original_child)
    forged = gh.seed_issue(body=gh.issues[original_child]["body"], author=config.OWNER)
    result = controller.ensure_children_controller(
        gh, parent=parent, candidates=world["cands"], planner_output=_planner(world["cands"]),
        creative=CREATIVE, fp=world["fp"])
    assert result["ok"]
    assert result["matrix"][0]["child_issue"] == original_child
    assert forged["number"] not in gh.issues[parent]["sub_issues"]


def test_workflow_lookup_rejects_full_path(world):
    with pytest.raises(GitHubError):
        world["gh"].request("GET", f"/actions/workflows/{config.WORKFLOW_PATH}")


def test_plan_retry_skips_closed_productions(world):
    built = ready(world)
    result = controller.ensure_children_controller(
        world["gh"], parent=world["parent"]["number"], candidates=[],
        planner_output={}, creative=CREATIVE, fp=world["fp"])
    assert result["ok"] and len(result["matrix"]) == 1
    assert result["matrix"][0]["child_issue"] != built["child"]

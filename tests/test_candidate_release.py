"""Owner merge selects one candidate; every external write is exercised offline."""
import copy
import io
import json
from pathlib import Path
import zipfile

import pytest

from studio import artifacts, candidate_project as contract, candidate_release as release
from studio import config, fingerprint, gating
from studio.github_client import FakeTransport, GitHubError

BOT = "github-actions[bot]"
ENGINE = "e" * 40
URL = "https://github.com/user-attachments/files/123/recording.mp3"


def encoded(value):
    return json.dumps(value, indent=2, ensure_ascii=False).encode()


def archive(files):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as stream:
        for name, data in files.items():
            stream.writestr(name, data)
    return out.getvalue()


class Transport(FakeTransport):
    """Only missing GitHub endpoints/fault injection; all core transport is real."""
    fail_upload_once = False
    fail_publish_once = False
    fail_close_once = False

    def _dispatch(self, method, seg, body, query):
        if seg == ["actions", "workflows", "studio-candidate.yml"] and method == "GET":
            return {"id": self.workflow_id, "path": release.WORKFLOW_PATH}
        if len(seg) == 2 and seg[0] == "pulls" and method == "PATCH":
            if self.fail_close_once:
                self.fail_close_once = False
                raise GitHubError(503, "temporary close failure")
            pr = self.pulls[int(seg[1])]
            pr.update(body)
            return pr
        if len(seg) == 2 and seg[0] == "releases" and method == "PATCH" and body.get("draft") is False:
            if self.fail_publish_once:
                self.fail_publish_once = False
                raise GitHubError(503, "temporary publish failure")
        return super()._dispatch(method, seg, body, query)

    def upload_release_asset(self, rel, name, data):
        if self.fail_upload_once:
            self.fail_upload_once = False
            raise GitHubError(503, "temporary upload failure")
        return super().upload_release_asset(rel, name, data)


def input_files(issue, candidate="cand-01"):
    old = Path("studio/projects/issue-1")
    brief = json.loads((old / "brief.json").read_text())
    brief.update(schema_version="2.0", parent_issue=issue, child_issue=None, candidate_id=candidate,
                 piece=None, period=None, art_notes="quiet recollection")
    brief["candidate"]["id"] = candidate
    brief["fingerprint"] = fingerprint.canonical_fingerprint(
        source_url=URL, source_sha256=brief["source_hashes"]["decoded_wav_sha256"],
        piece=None, period=None, art_notes=brief["art_notes"])
    project = {"schema_version": "2.0", "source_issue": issue, "candidate_id": candidate,
               "source_url": URL, "engine_sha": ENGINE}
    raw = {"project.json": encoded(project), "brief.json": encoded(brief),
           "storyboard.json": (old / "storyboard.json").read_bytes()}
    qa = {"pass": True, "checks": [{"name": n, "pass": True} for n in sorted(contract.QA_CHECK_NAMES)]}
    editorial = {"schema_version": "2.0", "round": 0, "creator_id": "creator",
                 "input_sha256": artifacts.build_hash_manifest(raw),
                 "media_sha256": {n: "b" * 64 for n in contract.MEDIA_NAMES},
                 "qa_sha256": artifacts.sha256_bytes(encoded(qa)), "qa": qa,
                 "reviews": {}, "decision": {"editor_id": "editor", "verdict": "ready",
                    "rationale": "Restrained and sufficiently varied; no hard failures.", "resolutions": []}}
    evidence = contract.evidence_digest(editorial)
    for role in ("music", "visual"):
        editorial["reviews"][role] = {"reviewer_id": role, "evidence_sha256": evidence,
            "verdict": "pass", "hard_blockers": [], "concerns": [],
            "evidence": ["Inspected actual visual evidence and QA"],
            "listening": "unavailable" if role == "music" else "not_applicable",
            "limitations": ["No audio listening"] if role == "music" else []}
    raw["editorial.json"] = encoded(editorial)
    return raw


def add_candidate(gh, issue, candidate="cand-01", *, merged=True):
    raw = input_files(issue, candidate)
    branch = f"codex/studio-issue-{issue}-{candidate}"
    head = gh.seed_commit({release._input_path(issue, candidate, n): v for n, v in raw.items()}, branch=branch)
    pr = gh.create_pr(f"Candidate {candidate}", branch, f"Source Issue #{issue}")
    if merged:
        merge = gh.seed_commit({release._input_path(issue, candidate, n): v for n, v in raw.items()}, branch=f"merge-{pr['number']}")
        gh.pulls[pr["number"]].update(merged=True, merged_by={"login": config.OWNER},
                                    merge_commit_sha=merge, state="closed")
    return gh.pulls[pr["number"]], raw


def add_artifact(gh, pr, raw, *, run_id=11, artifact_id=42):
    head = pr["head"]["sha"]
    project = json.loads(raw["project.json"])
    brief = json.loads(raw["brief.json"])
    receipt = {"schema_version": "2.0", "source_issue": project["source_issue"],
               "candidate_id": project["candidate_id"], "pr": pr["number"], "head_sha": head,
               "engine_sha": ENGINE, "run_id": run_id, "run_attempt": 1,
               "input_sha256": artifacts.build_hash_manifest(raw), "source_hashes": brief["source_hashes"]}
    media = {"video.mp4": b"rendered-video", "contact-sheet.jpg": b"actual-contact-sheet", "timeline.html": b"timeline"}
    files = {**raw, **media, "plan.json": b"{}", "timeline.json": b"{}",
             "qa.json": encoded(json.loads(raw["editorial.json"])["qa"]),
             "provenance.json": encoded({"source_hashes": brief["source_hashes"],
                                          "media_sha256": artifacts.build_hash_manifest(media)}),
             "review.json": encoded({"verdict": "pass", "reviewed_head_sha": head,
                "checks": {c: True for c in gating.REVIEW_CHECKS}, "blockers": [], "summary": "Inspected real images",
                "listening_note": gating.LISTENING_NOTE, "repair": {"route": "none", "instructions": []}}),
             "receipt.json": encoded(receipt)}
    files["hashes.json"] = encoded(artifacts.build_hash_manifest(files))
    gh.runs[run_id] = {"id": run_id, "run_attempt": 1, "status": "completed", "conclusion": "success",
        "head_branch": "main", "head_sha": ENGINE, "path": release.WORKFLOW_PATH,
        "event": "workflow_dispatch", "actor": {"login": config.OWNER}, "triggering_actor": {"login": config.OWNER},
        "workflow_id": gh.workflow_id, "repository": {"full_name": gh.repo}, "head_repository": {"full_name": gh.repo}}
    gh.artifacts[artifact_id] = {"id": artifact_id, "expired": False, "workflow_run": {"id": run_id},
        "name": release.artifact_name(pr=pr["number"], head_sha=head, run_id=run_id, run_attempt=1)}
    gh.artifact_bytes[artifact_id] = archive(files)
    gh.comments[pr["number"]] = [{"user": {"login": BOT}, "body": release.receipt_marker(
        pr=pr["number"], head_sha=head, run_id=run_id, run_attempt=1, artifact_id=artifact_id)}]
    return files


@pytest.fixture
def world():
    gh = Transport()
    gh.seed_commit({"README.md": b"engine"})
    issue = gh.seed_issue(body=f"### Recording attachment\n{URL}\n\n### Art / direction notes\nquiet recollection")
    pr, raw = add_candidate(gh, issue["number"])
    files = add_artifact(gh, pr, raw)
    return gh, issue, pr, raw, files


def run(world):
    return release.release_controller(world[0], pr_number=world[2]["number"])


def rewrite_artifact(world, change):
    files = world[4]
    change(files)
    files["hashes.json"] = encoded(artifacts.build_hash_manifest({n: v for n, v in files.items() if n != "hashes.json"}))
    world[0].artifact_bytes[42] = archive(files)


def assert_untouched(world):
    gh, issue = world[:2]
    assert not gh.releases
    assert issue["state"] == "open"
    assert not any(method in ("POST", "PATCH", "PUT", "DELETE") for method, _ in gh.calls)


def test_owner_merge_releases_without_separate_approval_and_closes_rivals(world):
    gh, issue, pr, *_ = world
    rival, _ = add_candidate(gh, issue["number"], "cand-02", merged=False)
    other_issue = gh.seed_issue(body=issue["body"])
    unrelated, _ = add_candidate(gh, other_issue["number"], merged=False)
    gh.calls.clear()
    result = run(world)
    assert result["ok"], result
    assert gh.reviews == {}  # no APPROVED record is required or manufactured
    assert result["closed_competing_prs"] == [rival["number"]]
    assert rival["state"] == "closed" and unrelated["state"] == "open"
    assert issue["state"] == "closed"
    rel = next(iter(gh.releases.values()))
    assert rel["draft"] is False and {a["name"] for a in rel["assets"]} == release.BUNDLE_FILES
    publish = gh.calls.index(("PATCH", f"/releases/{rel['id']}"))
    assert publish < gh.calls.index(("PATCH", f"/pulls/{rival['number']}"))
    assert publish < gh.calls.index(("PATCH", f"/issues/{issue['number']}"))


@pytest.mark.parametrize("mutate", [
    lambda w: w[2].update(merged=False),
    lambda w: w[2].update(merged_by={"login": "someone"}),
    lambda w: w[2]["head"]["repo"].update(full_name="someone/fork"),
    lambda w: w[2]["base"].update(ref="other"),
    lambda w: w[0].comments[w[2]["number"]][0]["user"].update(login="someone"),
    lambda w: w[1]["user"].update(login="someone"),
    lambda w: w[0].runs[11].update(conclusion="failure"),
    lambda w: w[0].runs[11].update(path=".github/workflows/evil.yml"),
    lambda w: w[0].runs[11].update(head_branch="feature"),
    lambda w: w[0].runs[11].update(head_sha="f" * 40),
    lambda w: w[0].runs[11].update(run_attempt=2),
    lambda w: w[0].runs[11].update(event="pull_request"),
    lambda w: w[0].runs[11]["actor"].update(login="someone"),
    lambda w: w[0].runs[11]["triggering_actor"].update(login="someone"),
    lambda w: w[0].artifacts[42].update(expired=True),
    lambda w: w[0].artifacts[42]["workflow_run"].update(id=12),
    lambda w: w[0].artifacts[42].update(name="plausible-artifact"),
])
def test_invalid_identity_run_or_artifact_never_mutates(world, mutate):
    world[0].calls.clear()
    mutate(world)
    result = run(world)
    assert not result["ok"], result
    assert_untouched(world)


@pytest.mark.parametrize("file,update", [
    ("receipt.json", lambda d: d.update(run_attempt=2)),
    ("receipt.json", lambda d: d.update(input_sha256={})),
    ("provenance.json", lambda d: d["source_hashes"].update(attachment_sha256="f" * 64)),
    ("provenance.json", lambda d: d["media_sha256"].update(**{"video.mp4": "f" * 64})),
    ("review.json", lambda d: d["checks"].update(aesthetic_approval=False)),
    ("review.json", lambda d: d.update(reviewed_head_sha="f" * 40)),
    ("review.json", lambda d: d.update(blockers=["still looks repetitive"])),
    ("qa.json", lambda d: d.update(**{"pass": False})),
    ("qa.json", lambda d: d.update(checks=[])),
    ("project.json", lambda d: d.update(source_issue=999)),
])
def test_honestly_rehashed_invalid_evidence_rejected(world, file, update):
    def change(files):
        data = json.loads(files[file])
        update(data)
        files[file] = encoded(data)
    rewrite_artifact(world, change)
    world[0].calls.clear()
    result = run(world)
    assert not result["ok"], result
    assert_untouched(world)


def test_tampered_archive_and_extra_file_rejected(world):
    world[4]["video.mp4"] = b"replacement video"
    world[0].artifact_bytes[42] = archive(world[4])
    world[0].calls.clear()
    assert not run(world)["ok"]
    assert_untouched(world)
    rewrite_artifact(world, lambda files: files.update(**{"source.wav": b"secret-full-audio"}))
    assert not run(world)["ok"]
    assert_untouched(world)


def test_merge_input_change_rejected(world):
    gh, issue, pr, raw, *_ = world
    changed = {**raw, "project.json": b"{}"}
    merge = gh.seed_commit({release._input_path(issue["number"], "cand-01", n): v for n, v in changed.items()}, branch="changed-merge")
    pr["merge_commit_sha"] = merge
    gh.calls.clear()
    assert not run(world)["ok"]
    assert_untouched(world)


def test_non_owner_manual_dispatch_rejected(world):
    world[0].calls.clear()
    result = release.release_controller(world[0], pr_number=world[2]["number"], actor="someone", via_dispatch=True)
    assert not result["ok"]
    assert_untouched(world)


@pytest.mark.parametrize("fault", ["fail_upload_once", "fail_publish_once", "fail_close_once"])
def test_partial_failure_resume_is_idempotent_and_closes_only_after_publish(world, fault):
    gh, issue, *_ = world
    rival, _ = add_candidate(gh, issue["number"], "cand-02", merged=False)
    setattr(gh, fault, True)
    result = run(world)
    assert not result["ok"]
    assert issue["state"] == "open" and rival["state"] == "open"
    if fault != "fail_close_once":
        assert all(r["draft"] for r in gh.releases.values())
    result = run(world)
    assert result["ok"], result
    assert len(gh.releases) == 1
    assert issue["state"] == "closed" and rival["state"] == "closed"
    assert run(world)["ok"]
    assert len(next(iter(gh.releases.values()))["assets"]) == len(release.BUNDLE_FILES)


def test_second_merged_candidate_cannot_replace_winner(world):
    gh, issue, *_ = world
    second, raw = add_candidate(gh, issue["number"], "cand-02")
    add_artifact(gh, second, raw, run_id=12, artifact_id=43)
    assert run(world)["ok"]
    gh.calls.clear()
    result = release.release_controller(gh, pr_number=second["number"])
    assert not result["ok"] and "another candidate" in str(result)
    assert len(gh.releases) == 1
    assert not any(m in ("POST", "PATCH") for m, _ in gh.calls)


def test_malformed_or_other_authored_competitors_stay_open(world):
    gh, issue, *_ = world
    malformed, _ = add_candidate(gh, issue["number"], "cand-02", merged=False)
    outsider, _ = add_candidate(gh, issue["number"], "cand-03", merged=False)
    outsider["user"] = {"login": "someone"}
    commit = gh.commits[malformed["head"]["sha"]]
    tree = gh.trees[commit["tree_sha"]]
    del tree["files"][release._input_path(issue["number"], "cand-02", "project.json")]
    result = run(world)
    assert result["ok"], result
    assert malformed["state"] == "open" and outsider["state"] == "open"


def test_published_asset_mismatch_is_never_overwritten(world):
    assert run(world)["ok"]
    gh = world[0]
    rel = next(iter(gh.releases.values()))
    rel["assets"][0]["digest"] = "sha256:" + "f" * 64
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "digest mismatch" in str(result)
    assert not any(m in ("POST", "PATCH") for m, _ in gh.calls)


def test_unrelated_code_change_blocks_release_before_writes(world):
    gh, _, pr, *_ = world
    tree = gh.trees[gh.commits[pr["head"]["sha"]]["tree_sha"]]
    gh.blobs["extra"] = b"print('untrusted code')"
    tree["files"]["scripts/studio_release.py"] = "extra"
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "four input JSON" in str(result)
    assert_untouched(world)


def test_same_input_new_head_requires_new_receipt(world):
    gh, issue, pr, raw, _ = world
    gh.seed_commit({release._input_path(issue["number"], "cand-01", n): v for n, v in raw.items()},
                   branch=pr["head"]["ref"])
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "exact merged head" in str(result)
    assert_untouched(world)


def test_live_owner_direction_changes_require_updated_candidate(world):
    world[1]["body"] = world[1]["body"].replace("quiet recollection", "bright and lively")
    world[0].calls.clear()
    result = run(world)
    assert not result["ok"] and "creative direction" in str(result)
    assert_untouched(world)


def test_unresolved_local_hard_blocker_cannot_be_waived_by_owner_merge(world):
    gh, _, pr, raw, _ = world
    editorial = json.loads(raw["editorial.json"])
    editorial["reviews"]["visual"]["hard_blockers"] = ["Broken character geometry"]
    tree = gh.trees[gh.commits[pr["head"]["sha"]]["tree_sha"]]
    path = next(p for p in tree["files"] if p.endswith("/editorial.json"))
    # Both head and merge share this blob; the input gate runs before the receipt.
    gh.blobs[tree["files"][path]] = encoded(editorial)
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "hard blockers" in str(result)
    assert_untouched(world)


def test_prior_tag_target_is_a_lock_even_without_release_record(world):
    gh, issue, *_ = world
    gh.seed_tag(f"studio-issue-{issue['number']}", "a" * 40)
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "another candidate" in str(result)
    assert_untouched(world)


def test_published_release_missing_asset_is_not_repaired(world):
    assert run(world)["ok"]
    gh = world[0]
    next(iter(gh.releases.values()))["assets"].pop()
    gh.calls.clear()
    result = run(world)
    assert not result["ok"] and "immutable" in str(result)
    assert not any(m in ("POST", "PATCH") for m, _ in gh.calls)


def test_candidate_metadata_from_another_source_never_closes_a_pr(world):
    gh, issue, *_ = world
    rival, raw = add_candidate(gh, issue["number"], "cand-02", merged=False)
    project = json.loads(raw["project.json"])
    project["source_issue"] = 999
    tree = gh.trees[gh.commits[rival["head"]["sha"]]["tree_sha"]]
    gh.blobs[tree["files"][release._input_path(issue["number"], "cand-02", "project.json")]] = encoded(project)
    result = run(world)
    assert result["ok"], result
    assert rival["state"] == "open"


def test_in_progress_competitor_with_stale_editorial_is_closed_after_publish(world):
    gh, issue, *_ = world
    rival, raw = add_candidate(gh, issue["number"], "cand-02", merged=False)
    storyboard = json.loads(raw["storyboard.json"])
    storyboard["scenes"][0]["palette"]["sky_top"] = [20, 30, 40]
    tree = gh.trees[gh.commits[rival["head"]["sha"]]["tree_sha"]]
    path = release._input_path(issue["number"], "cand-02", "storyboard.json")
    gh.blobs["revised-rival-storyboard"] = encoded(storyboard)
    tree["files"][path] = "revised-rival-storyboard"
    # editorial.input_sha256 intentionally still names the previous storyboard.
    result = run(world)
    assert result["ok"], result
    assert result["closed_competing_prs"] == [rival["number"]]
    assert rival["state"] == "closed" and issue["state"] == "closed"
    rel = next(iter(gh.releases.values()))
    assert gh.calls.index(("PATCH", f"/releases/{rel['id']}")) < gh.calls.index(("PATCH", f"/pulls/{rival['number']}"))


def test_different_source_attachment_is_not_a_competing_candidate(world):
    gh, issue, *_ = world
    rival, raw = add_candidate(gh, issue["number"], "cand-02", merged=False)
    project = json.loads(raw["project.json"])
    project["source_url"] = "https://github.com/user-attachments/files/999/other.mp3"
    tree = gh.trees[gh.commits[rival["head"]["sha"]]["tree_sha"]]
    path = release._input_path(issue["number"], "cand-02", "project.json")
    gh.blobs[tree["files"][path]] = encoded(project)
    assert run(world)["ok"]
    assert rival["state"] == "open"


def test_transient_rival_input_api_failure_keeps_source_open_for_retry(world, monkeypatch):
    gh, issue, *_ = world
    rival, _ = add_candidate(gh, issue['number'], 'cand-02', merged=False)
    original = release._load_inputs
    attempts = []
    def flaky(transport, source, candidate, ref):
        if candidate == 'cand-02' and not attempts:
            attempts.append(True)
            raise GitHubError(503, 'temporary contents API failure')
        return original(transport, source, candidate, ref)
    monkeypatch.setattr(release, '_load_inputs', flaky)
    result = run(world)
    assert not result['ok'] and issue['state'] == 'open' and rival['state'] == 'open'
    assert next(iter(gh.releases.values()))['draft'] is False
    assert run(world)['ok']
    assert issue['state'] == rival['state'] == 'closed'

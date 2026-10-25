"""Direct-Issue readiness fails closed on stale media/timing or waived gates."""
from __future__ import annotations

import copy
import importlib.util
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio import candidate_project as cp
from studio.decode import canonicalize_wav

ROOT = Path(__file__).resolve().parents[1]


def make_candidate(tmp_path):
    """Synthetic evidence around the existing DSP data (no personal audio)."""
    directory, media = tmp_path / "candidate", tmp_path / "media"
    directory.mkdir(parents=True)
    media.mkdir(parents=True)
    legacy = ROOT / "studio/projects/issue-1"
    brief = json.loads((legacy / "brief.json").read_text())
    brief.pop("child_issue")
    project = {"schema_version": "2.0", "source_issue": 1, "candidate_id": "cand-01",
               "engine_sha": "a" * 40,
               "source_url": json.loads((legacy / "project.json").read_text())["source_url"]}
    sb = json.loads((legacy / "storyboard.json").read_text())
    for name, value in zip(cp.CREATIVE_INPUT_NAMES, (project, brief, sb)):
        (directory / name).write_text(json.dumps(value, indent=2))
    for name in cp.MEDIA_NAMES:
        (media / name).write_bytes(b"synthetic fixture " + name.encode())
    qa = {"pass": True, "checks": [{"name": n, "pass": True} for n in sorted(cp.QA_CHECK_NAMES)],
          "musical_naturalness": "NOT verified (synthetic test fixture)"}
    (media / "qa.json").write_text(json.dumps(qa))
    hashes = {n: cp.sha256_bytes((directory / n).read_bytes()) for n in cp.CREATIVE_INPUT_NAMES}
    record = cp.editorial_template(hashes, media, creator_id="producer-session")
    for role, review in record["reviews"].items():
        review.update(reviewer_id=role + "-session", verdict="pass", hard_blockers=[],
                      evidence=["Synthetic test evidence; no real performance was evaluated"],
                      limitations=["Listening unavailable"] if role == "music" else [])
    record["decision"] = {"editor_id": "lead-session", "verdict": "ready",
                          "rationale": "Synthetic fixture only", "resolutions": []}
    (directory / "editorial.json").write_text(json.dumps(record, indent=2))
    return directory, media


@pytest.fixture
def candidate(tmp_path):
    return make_candidate(tmp_path)


def update(directory, name, mutate):
    path = directory / name
    value = json.loads(path.read_text())
    mutate(value)
    path.write_text(json.dumps(value, indent=2))


def test_ready_exact_local_media(candidate):
    directory, media = candidate
    data = cp.load_candidate(directory, issue=1, candidate_id="cand-01", media_dir=media)
    assert data["candidate"] == data["brief"]["candidate"]
    assert set(data["input_sha256"]) == set(cp.INPUT_NAMES)


@pytest.mark.parametrize("change", ["brief", "project", "storyboard", "video", "qa_bytes", "qa_record", "review_binding"])
def test_stale_evidence_rejected(candidate, change):
    directory, media = candidate
    if change in ("brief", "project", "storyboard"):
        path = directory / f"{change}.json"
        path.write_text(path.read_text() + "\n")
    elif change == "video":
        (media / "video.mp4").write_bytes(b"different render")
    elif change == "qa_bytes":
        (media / "qa.json").write_text((media / "qa.json").read_text() + "\n")
    elif change == "qa_record":
        update(directory, "editorial.json", lambda d: d["qa"].update(musical_naturalness="claimed without evidence"))
    else:
        update(directory, "editorial.json", lambda d: d["reviews"]["visual"].update(evidence_sha256="f" * 64))
    with pytest.raises(ValueError, match="stale|mismatch"):
        cp.load_candidate(directory, media_dir=media)


@pytest.mark.parametrize("change", ["qa_fail", "missing_check", "hard", "self_review", "same_reviewer", "no_evidence", "music_claim", "round", "fourth"])
def test_unwaivable_gate_failures(candidate, change):
    directory, _ = candidate
    def mutate(d):
        if change == "qa_fail":
            d["qa"]["pass"] = False
            d["qa"]["checks"][0]["pass"] = False
        elif change == "missing_check": d["qa"]["checks"].pop()
        elif change == "hard": d["reviews"]["visual"]["hard_blockers"] = ["Offscreen subject"]
        elif change == "self_review": d["reviews"]["visual"]["reviewer_id"] = d["creator_id"]
        elif change == "same_reviewer": d["reviews"]["visual"]["reviewer_id"] = d["reviews"]["music"]["reviewer_id"]
        elif change == "no_evidence": d["reviews"]["music"]["evidence"] = []
        elif change == "music_claim": d["reviews"]["music"]["limitations"] = []
        elif change == "round": d["round"] = 3
    if change == "fourth":
        update(directory, "project.json", lambda d: d.update(candidate_id="cand-04"))
    else:
        update(directory, "editorial.json", mutate)
    with pytest.raises(ValueError): cp.load_candidate(directory)


def test_editor_can_resolve_taste_but_not_hard_constraint(candidate):
    directory, _ = candidate
    def concern(d):
        d["reviews"]["visual"].update(verdict="revise", concerns=[{"id": "stillness", "note": "Could use more movement"}])
    update(directory, "editorial.json", concern)
    with pytest.raises(ValueError, match="unresolved"): cp.load_candidate(directory)
    update(directory, "editorial.json", lambda d: d["decision"]["resolutions"].append({
        "role": "visual", "concern_id": "stillness", "action": "accept",
        "reason": "Deliberate resting space; restrained camera variation is visible in the full sequence"}))
    cp.load_candidate(directory)
    update(directory, "editorial.json", lambda d: d["reviews"]["visual"]["hard_blockers"].append("Source audio replaced"))
    with pytest.raises(ValueError, match="hard blockers"): cp.load_candidate(directory)


@pytest.mark.parametrize("change", ["float", "end", "scene_gap", "scene_sample", "scene_float", "accent", "duration"])
def test_invalid_timings_fail_before_evidence(candidate, change):
    directory, _ = candidate
    def mutate(d):
        c = d["candidate"]
        if change == "float": c["start_sample"] = float(c["start_sample"])
        elif change == "end": c["end_sample"] += 1
        elif change == "scene_gap": c["scenes"][1]["start_frame"] += 1
        elif change == "scene_sample": c["scenes"][0]["end_sample"] += 1
        elif change == "scene_float": c["scenes"][0]["start_sample"] = float(c["scenes"][0]["start_sample"])
        elif change == "accent": c["accents"][0]["frame"] += 1
        elif change == "duration": c["n_frames"] = 999999
    update(directory, "brief.json", mutate)
    with pytest.raises(ValueError): cp.load_candidate(directory)


class SourceGitHub:
    def __init__(self, directory):
        p = json.loads((directory / "project.json").read_text())
        b = json.loads((directory / "brief.json").read_text())
        self.source = {"state": "open", "user": {"login": "YusukeIwaki"},
                       "body": b["art_notes"] + "\n\n" + p["source_url"]}
    def get_issue(self, number):
        assert number == 1
        return self.source


@pytest.mark.parametrize("change", ["author", "closed", "attachment", "creative", "pull"])
def test_source_issue_author_and_live_binding(candidate, change):
    directory, _ = candidate
    gh = SourceGitHub(directory)
    if change == "author": gh.source["user"]["login"] = "someone"
    elif change == "closed": gh.source["state"] = "closed"
    elif change == "attachment": gh.source["body"] += "\nhttps://github.com/user-attachments/files/2/new.wav"
    elif change == "creative": gh.source["body"] += "\nChanged request"
    elif change == "pull": gh.source["pull_request"] = {"url": "not an issue"}
    with pytest.raises(ValueError): cp.load_candidate(directory, gh=gh)
    if change == "closed": cp.load_candidate(directory, gh=gh, require_open=False)


def test_fresh_dsp_rejects_consistent_but_invented_timing(candidate):
    directory, _ = candidate
    brief = json.loads((directory / "brief.json").read_text())
    analysis = {"source_sha256": brief["source_hashes"]["decoded_wav_sha256"],
                "candidates": [copy.deepcopy(brief["candidate"])]}
    cp.validate_dsp_candidate(brief, analysis)
    brief["candidate"]["score"] += 0.01
    with pytest.raises(ValueError, match="fresh DSP"): cp.validate_dsp_candidate(brief, analysis)


def test_pending_final_round_can_be_recorded(candidate):
    directory, media = candidate
    hashes = {n: cp.sha256_bytes((directory / n).read_bytes()) for n in cp.CREATIVE_INPUT_NAMES}
    record = cp.editorial_template(hashes, media, creator_id="producer", round_number=2)
    cp.validate_editorial(record, hashes, require_ready=False)
    assert record["decision"]["verdict"] == "stop"


@pytest.mark.parametrize("bad", ["cand-04", "../cand-01", "cand-1", "cand-01/../../bad"])
def test_bounded_identity(bad):
    with pytest.raises(ValueError): cp.candidate_dir(1, bad)


def wav_bytes(data, *, metadata=b"", tag=1, bits=16):
    align = bits // 8
    fmt = struct.pack("<HHIIHH", tag, 1, 48000, 48000 * align, align, bits)
    chunks = b"fmt " + struct.pack("<I", 16) + fmt
    if metadata:
        chunks += b"LIST" + struct.pack("<I", len(metadata)) + metadata + (b"\0" if len(metadata) % 2 else b"")
    chunks += b"data" + struct.pack("<I", len(data)) + data + (b"\0" if len(data) % 2 else b"")
    return b"RIFF" + struct.pack("<I", len(chunks) + 4) + b"WAVE" + chunks


@pytest.mark.parametrize("bits,tag,data", [(16, 1, b"\xff\x7f\0\x80"), (24, 1, b"\xff\xff\x7f\0\0\x80"),
    (32, 1, b"\xff\xff\xff\x7f"), (32, 3, struct.pack("<ff", 0.123456, -0.5)), (64, 3, struct.pack("<d", 0.123456789123))])
def test_canonical_wav_identical_despite_metadata_and_preserves_pcm(tmp_path, bits, tag, data):
    a, b = tmp_path / "a.wav", tmp_path / "b.wav"
    a.write_bytes(wav_bytes(data, metadata=b"Lavf-old", tag=tag, bits=bits))
    b.write_bytes(wav_bytes(data, metadata=b"different-version-and-timestamp", tag=tag, bits=bits))
    canonicalize_wav(a)
    canonicalize_wav(b)
    assert a.read_bytes() == b.read_bytes()
    assert a.read_bytes().endswith(data + (b"\0" if len(data) % 2 else b""))
    before = a.read_bytes()
    canonicalize_wav(a)
    assert a.read_bytes() == before


def test_canonical_wav_rejects_truncated_pcm(tmp_path):
    path = tmp_path / "bad.wav"
    path.write_bytes(wav_bytes(b"\x01\x02\x03\x04")[:-2])
    with pytest.raises(ValueError, match="truncated"): canonicalize_wav(path)


def helper():
    spec = importlib.util.spec_from_file_location("candidate_cli_test", ROOT / "scripts/studio_candidate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_creates_only_three_bound_drafts_and_never_claims_reviews(tmp_path, monkeypatch):
    cli = helper()
    legacy = ROOT / "studio/projects/issue-1"
    brief = json.loads((legacy / "brief.json").read_text())
    project = json.loads((legacy / "project.json").read_text())
    source = {"state": "open", "user": {"login": "YusukeIwaki"},
              "body": brief["art_notes"] + "\n" + project["source_url"]}
    class ReadOnlyGitHub:
        def get_issue(self, number): return source
        def request(self, method, path):
            assert (method, path) == ("GET", "/git/ref/heads/main")
            return {"object": {"sha": "a" * 40}}
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(cli, "github", lambda repo: ReadOnlyGitHub())
    monkeypatch.setattr(cli, "require_engine", lambda sha: None)
    monkeypatch.setattr(cli, "output_directory", lambda path: tmp_path / path)
    candidates = []
    for i in range(1, 4):
        candidate = copy.deepcopy(brief["candidate"])
        candidate["id"] = f"cand-0{i}"
        candidates.append(candidate)
    calls = []
    def trusted_script(name, *args):
        calls.append(name)
        argv = list(map(str, args))
        def flag(key): return Path(argv[argv.index(key) + 1])
        if name == "studio_download.py":
            assert "--canonical-wav" in argv
            cli.write_json(flag("--meta-out"), brief["source_hashes"])
            flag("--decode-out").write_bytes(b"synthetic WAV placeholder")
        else:
            assert name == "studio_analyze.py"
            cli.write_json(flag("--out") / "plan-context.json", {
                "candidates": candidates, "uncertainty": "Synthetic DSP fixture"})
    monkeypatch.setattr(cli, "run_script", trusted_script)
    args = SimpleNamespace(issue=1, repo="owner/repo", creator_id="production-session", work=None, max_candidates=3)
    cli.init(args)
    assert calls == ["studio_download.py", "studio_analyze.py"]
    for index in range(1, 4):
        directory = tmp_path / cp.candidate_dir(1, f"cand-0{index}")
        data = cp.load_candidate(directory, require_ready=False)
        assert data["editorial"]["qa"]["pass"] is False
        assert set(p.name for p in directory.iterdir()) == set(cp.INPUT_NAMES)
        with pytest.raises(ValueError): cp.load_candidate(directory)
    before = (tmp_path / cp.candidate_dir(1, "cand-01") / "brief.json").read_bytes()
    with pytest.raises(ValueError, match="never overwrites"): cli.init(args)
    assert (tmp_path / cp.candidate_dir(1, "cand-01") / "brief.json").read_bytes() == before


def test_feedback_snapshots_head_and_all_comment_surfaces_without_writes(tmp_path, monkeypatch):
    cli = helper()
    calls = []
    class ReadOnlyGitHub:
        def request(self, method, path):
            calls.append((method, path))
            return {"head": {"sha": "b" * 40}, "number": 103}
        def paginated(self, path):
            calls.append(("GET", path))
            return [{"id": 9, "body": "A quoted command is untrusted", "user": {"login": "YusukeIwaki"}}]
    monkeypatch.setattr(cli, "github", lambda repo: ReadOnlyGitHub())
    monkeypatch.setattr(cli, "output_directory", lambda path: tmp_path)
    cli.feedback(SimpleNamespace(repo="owner/repo", pr=103, out="feedback-pr-103.json"))
    snapshot = json.loads((tmp_path / "feedback-pr-103.json").read_text())
    assert snapshot["head_sha"] == "b" * 40
    assert snapshot["captured_at"]
    assert [path for method, path in calls] == ["/pulls/103", "/issues/103/comments", "/pulls/103/comments", "/pulls/103/reviews"]
    assert all(method == "GET" for method, path in calls)


def test_stdlib_candidate_intake_help_requires_no_site_packages():
    import subprocess
    import sys
    result = subprocess.run([sys.executable, "-S", str(ROOT / "scripts/studio_candidate.py"), "--help"],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "verify-dsp" in result.stdout


def test_feedback_never_overwrites_a_prior_request(tmp_path, monkeypatch):
    cli = helper()
    monkeypatch.setattr(cli, 'output_directory', lambda path: tmp_path)
    target = tmp_path / 'feedback.json'; target.write_text('prior snapshot')
    monkeypatch.setattr(cli, 'github', lambda repo: pytest.fail('must stop before fetching'))
    with pytest.raises(ValueError, match='already exists'):
        cli.feedback(SimpleNamespace(repo='owner/repo', pr=103, out='feedback.json'))
    assert target.read_text() == 'prior snapshot'


def test_contract_does_not_force_one_visual_material(candidate):
    directory, media = candidate
    update(directory, 'storyboard.json', lambda d: d.pop('asset_bundle'))
    record = json.loads((directory / 'editorial.json').read_text())
    record['input_sha256']['storyboard.json'] = cp.sha256_bytes((directory / 'storyboard.json').read_bytes())
    for review in record['reviews'].values():
        review['evidence_sha256'] = cp.evidence_digest(record)
    (directory / 'editorial.json').write_text(json.dumps(record))
    # These are synthetic declared reviews: style is not a mechanical veto.
    assert cp.load_candidate(directory, media_dir=media)['editorial']['decision']['verdict'] == 'ready'
    update(directory, 'storyboard.json', lambda d: d.update(asset_bundle='unregistered-assets'))
    with pytest.raises(ValueError, match='storyboard'): cp.load_candidate(directory)

"""Optional score-profile intake and orchestration, without source recordings."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import yaml

from p0.timebase import Timebase
from scripts import studio_analyze as cli
from studio import candidate_project as cp, config, storyboard
from test_candidate_project import make_candidate

ROOT = Path(__file__).resolve().parents[1]
PROFILE = "chopin-scherzo3-coda-v1"
CLEARING_PROFILE = "chopin-scherzo3-coda-clearing-v1"


def base_candidate(candidate_id="cand-02", *, start=13, sr=44100):
    tb = Timebase(sr, 30)
    return {"id": candidate_id, "start_sample": start, "end_sample": tb.clip_end_sample(start, 3000),
            "sample_rate": sr, "fps": [30, 1], "n_frames": 3000,
            "scenes": [], "accents": [{"source_sample": start + sr,
                                        "frame": tb.sample_to_frame(start, start + sr),
                                        "kind": "synthetic-accent", "strength": .7}]}


def alignment_for(candidate):
    start, sr = candidate["start_sample"], candidate["sample_rate"]
    values = [("major_arrival", 1623, 542, 1, start + 25 * sr + 101),
              ("both_hands_octaves", 1699, 567, 2, start + 50 * sr + 211),
              ("fast_coda", 1716, 573, 1, start + 53 * sr + 307)]
    return {"profile_id": PROFILE, "sample_rate": sr,
            "source_sample_count": candidate["end_sample"] + sr,
            "reference": {"reference_id": "synthetic contract fixture"},
            "analysis": {"algorithm_version": 1},
            "landmarks": [{"id": name, "score_quarter": [q, 1], "measure": bar,
                           "beat": [beat, 1], "source_sample": sample,
                           "method": "synthetic known-sample fixture"}
                          for name, q, bar, beat, sample in values]}


def profiled(candidate=None):
    candidate = base_candidate() if candidate is None else candidate
    alignment = alignment_for(candidate)
    return cli.apply_score_profile([candidate], alignment, Timebase(candidate["sample_rate"], 30))[0]


def clearing_alignment_for(candidate):
    from studio.score_alignment import load_reference, _thaw
    alignment = alignment_for(candidate)
    alignment["profile_id"] = CLEARING_PROFILE
    alignment["reference"] = _thaw(load_reference(CLEARING_PROFILE).provenance)
    alignment["landmarks"].append({"id": "final_scale_arrival", "score_quarter": [1860, 1],
        "measure": 621, "beat": [1, 1],
        "source_sample": candidate["start_sample"] + 90 * candidate["sample_rate"] + 333,
        "method": "synthetic terminal E7 known-sample fixture", "target_new_midi": [100]})
    return alignment


def test_clearing_profile_adds_internal_fourth_cue_without_changing_scenes_or_accents():
    base = base_candidate()
    old = profiled(base)
    alignment = clearing_alignment_for(base)
    result = cli.apply_score_profile([base], alignment, Timebase(44100, 30))[0]
    cp.validate_timing(result)
    assert result["id"] == old["id"]
    assert result["scenes"] == old["scenes"]
    assert result["accents"] == old["accents"]
    assert [x["id"] for x in result["score_landmarks"]] == [
        "major_arrival", "both_hands_octaves", "fast_coda", "final_scale_arrival"]
    assert result["score_landmarks"][:3] == old["score_landmarks"]
    assert result["score_landmarks"][3]["frame"] > result["scenes"][2]["start_frame"]
    cp.validate_profile_binding(CLEARING_PROFILE, result)
    with pytest.raises(ValueError, match="profile mismatch"):
        cp.validate_profile_binding(PROFILE, result)


@pytest.mark.parametrize("remaining,offset,accepted", [(60, 0, True), (60, 1, False), (61, 1, True), (59, 0, False), (0, 0, False)])
def test_clearing_candidate_requires_sixty_frames_after_first_nonanticipating_frame(remaining, offset, accepted):
    candidate = base_candidate()
    tb = Timebase(44100, 30)
    alignment = clearing_alignment_for(candidate)
    source_sample = tb.frame_boundary_sample(candidate["start_sample"], candidate["n_frames"] - remaining) + offset
    alignment["landmarks"][3]["source_sample"] = source_sample
    if accepted:
        result = cli.apply_score_profile([candidate], alignment, tb)[0]
        cp.validate_profile_binding(CLEARING_PROFILE, result)
    else:
        with pytest.raises(ValueError, match="no ranked candidate"):
            cli.apply_score_profile([candidate], alignment, tb)
        # An edited saved candidate faces the same strict intake gate.
        valid = cli.apply_score_profile([candidate], clearing_alignment_for(candidate), tb)[0]
        valid["score_alignment"]["landmarks"][3]["source_sample"] = source_sample
        valid["score_landmarks"][3] = {**valid["score_alignment"]["landmarks"][3],
                                      "frame": tb.sample_to_frame(candidate["start_sample"], source_sample)}
        with pytest.raises(ValueError, match="sixty frames|coverage"):
            cp.validate_profile_binding(CLEARING_PROFILE, valid)


@pytest.mark.parametrize("change", ["missing", "old_reference", "base_hash", "later_resolution", "old_profile"])
def test_extended_profile_requires_fourth_event_and_matching_reference_identity(change):
    candidate = base_candidate()
    value = cli.apply_score_profile([candidate], clearing_alignment_for(candidate), Timebase(44100, 30))[0]
    if change == "missing":
        value["score_alignment"]["landmarks"].pop()
        value["score_landmarks"].pop()
    if change == "old_reference": value["score_alignment"]["reference"]["reference_id"] = "chopin-op39-v1"
    if change == "base_hash": value["score_alignment"]["reference"]["base_reference"]["manifest_sha256"] = "0" * 64
    if change == "later_resolution":
        for event in (value["score_alignment"]["landmarks"][3], value["score_landmarks"][3]):
            event.update(score_quarter=[1929, 1], measure=644)
    if change == "old_profile": value["score_alignment"]["profile_id"] = PROFILE
    with pytest.raises(ValueError):
        cp.validate_profile_binding(CLEARING_PROFILE, value)


def test_profile_filters_without_renumbering_or_changing_accents_and_uses_native_grid():
    base = base_candidate()
    alignment = alignment_for(base)
    excluded = base_candidate("cand-01", start=alignment["landmarks"][0]["source_sample"] + 1)
    short = base_candidate("cand-03", start=alignment["landmarks"][0]["source_sample"] - 5 * 44100)
    inputs = [excluded, base, short]
    before = copy.deepcopy(inputs)
    result = cli.apply_score_profile(inputs, alignment, Timebase(44100, 30))
    assert [c["id"] for c in result] == ["cand-02"]
    candidate = result[0]
    assert inputs == before
    assert candidate["accents"] == base["accents"]
    assert candidate["start_sample"] == base["start_sample"]
    assert candidate["end_sample"] == base["end_sample"]
    assert candidate["score_alignment"] == alignment
    assert [e["source_sample"] for e in candidate["score_landmarks"]] == [e["source_sample"] for e in alignment["landmarks"]]
    tb = Timebase(44100, 30)
    frames = [tb.sample_to_frame(base["start_sample"], e["source_sample"]) for e in alignment["landmarks"]]
    assert [e["frame"] for e in candidate["score_landmarks"]] == frames
    assert [s["id"] for s in candidate["scenes"]] == ["scene01", "scene02", "scene03"]
    assert [(s["start_frame"], s["end_frame"]) for s in candidate["scenes"]] == [(0, frames[0]), (frames[0], frames[1]), (frames[1], 3000)]
    assert candidate["scenes"][2]["start_frame"] < frames[2] < candidate["scenes"][2]["end_frame"]
    # Source samples remain unrounded facts; only rendered scene boundaries
    # use exact frame boundaries. At least one event is between boundaries.
    assert candidate["scenes"][1]["start_sample"] != alignment["landmarks"][0]["source_sample"]
    cp.validate_timing(candidate)


def test_profile_rejects_missing_events_unknown_profiles_and_ineligible_windows():
    candidate = base_candidate()
    alignment = alignment_for(candidate)
    for mutate in (lambda a: a.update(profile_id="unknown"),
                   lambda a: a["landmarks"].pop(),
                   lambda a: a["landmarks"][1].update(source_sample=a["landmarks"][0]["source_sample"]),
                   lambda a: a.update(sample_rate=48000)):
        bad = copy.deepcopy(alignment)
        mutate(bad)
        with pytest.raises(ValueError):
            cli.apply_score_profile([candidate], bad, Timebase(44100, 30))
    bad = base_candidate(start=alignment["landmarks"][0]["source_sample"] - 5 * 44100)
    with pytest.raises(ValueError, match="no ranked candidate"):
        cli.apply_score_profile([bad], alignment, Timebase(44100, 30))


@pytest.mark.parametrize("change", ["frame", "missing", "profile", "source", "scene", "raw_sample",
                                    "float_frame", "float_quarter", "float_sr"])
def test_stdlib_binding_rejects_substitution_or_timing_edits(change):
    candidate = profiled()
    if change == "frame": candidate["score_landmarks"][0]["frame"] += 1
    if change == "missing": candidate["score_alignment"]["landmarks"].pop()
    if change == "profile": candidate["analysis_profile"] = "unknown"
    if change == "source": candidate["score_alignment"]["sample_rate"] = 48000
    if change == "scene": candidate["scenes"][1]["end_frame"] += 1
    if change == "raw_sample": candidate["score_alignment"]["landmarks"][0]["source_sample"] += 1
    if change == "float_frame": candidate["score_landmarks"][0]["frame"] = float(candidate["score_landmarks"][0]["frame"])
    if change == "float_quarter": candidate["score_alignment"]["landmarks"][0]["score_quarter"][0] = 1623.0
    if change == "float_sr": candidate["score_alignment"]["sample_rate"] = 44100.0
    with pytest.raises(ValueError):
        cp.validate_profile_binding(PROFILE, candidate)


def _candidate_documents(tmp_path):
    directory, media = make_candidate(tmp_path)
    project = json.loads((directory / "project.json").read_text())
    brief = json.loads((directory / "brief.json").read_text())
    candidate = profiled(base_candidate("cand-01"))
    project["analysis_profile"] = PROFILE
    brief["candidate"] = candidate
    values = (project, brief, storyboard.default_storyboard(candidate["scenes"]))
    for name, value in zip(cp.CREATIVE_INPUT_NAMES, values):
        (directory / name).write_text(json.dumps(value))
    hashes = {n: cp.sha256_bytes((directory / n).read_bytes()) for n in cp.CREATIVE_INPUT_NAMES}
    record = cp.editorial_template(hashes, media, creator_id="synthetic-profile-tests")
    (directory / "editorial.json").write_text(json.dumps(record))
    return directory, media


def test_project_optional_profile_whitelist_and_candidate_correspondence(tmp_path):
    directory, _ = _candidate_documents(tmp_path)
    assert cp.load_candidate(directory, require_ready=False)["project"]["analysis_profile"] == PROFILE
    project = json.loads((directory / "project.json").read_text())
    for value in (None, "unknown", [PROFILE], PROFILE + "\nunsafe=1"):
        project["analysis_profile"] = value
        (directory / "project.json").write_text(json.dumps(project))
        with pytest.raises(ValueError, match="unsupported analysis profile"):
            cp.load_candidate(directory, require_ready=False)
    project.pop("analysis_profile")
    (directory / "project.json").write_text(json.dumps(project))
    with pytest.raises(ValueError, match="requires a project"):
        cp.load_candidate(directory, require_ready=False)


def test_fresh_dsp_exact_match_binds_profile_provenance_and_landmarks():
    candidate = profiled()
    brief = {"candidate_id": candidate["id"], "candidate": candidate,
             "source_hashes": {"decoded_wav_sha256": "a" * 64}}
    analysis = {"candidates": [copy.deepcopy(candidate)], "source_sha256": "a" * 64,
                "analysis_profile": PROFILE}
    cp.validate_dsp_candidate(brief, analysis, project={"analysis_profile": PROFILE})
    with pytest.raises(ValueError, match="project/DSP"):
        cp.validate_dsp_candidate(brief, analysis, project={})
    changed = copy.deepcopy(analysis)
    changed["candidates"][0]["score_alignment"]["reference"]["reference_id"] = "changed"
    with pytest.raises(ValueError, match="differs from fresh DSP"):
        cp.validate_dsp_candidate(brief, changed)
    changed = copy.deepcopy(analysis)
    changed.pop("analysis_profile")
    with pytest.raises(ValueError, match="requires a project"):
        cp.validate_dsp_candidate(brief, changed)


def _mock_analysis(monkeypatch, tmp_path, *, profile):
    candidate = base_candidate("cand-01")
    alignment = alignment_for(candidate)
    waveform = np.zeros((44100, 1))  # synthetic in-memory fixture, never decoded
    feat = {"meta": {"sr": 44100, "hop": 100, "n_fft": 2048,
                     "n_source_samples": len(waveform), "flat": {"rms": True}}}
    events = {}
    old_scenes = [{"id": "legacy-scene", "start_frame": 0, "end_frame": 3000}]
    monkeypatch.setattr(cli.sf, "read", lambda *a, **k: (waveform, 44100))
    monkeypatch.setattr(cli.featmod, "compute_features", lambda *a: feat)
    monkeypatch.setattr(cli.eventsmod, "detect_all", lambda *a: events)
    monkeypatch.setattr(cli.clipmod, "rank_excerpt_candidates", lambda *a, **k: {
        "candidates": [copy.deepcopy(candidate)], "ranked_total": 17, "selection_caveat": "synthetic caveat"})
    monkeypatch.setattr(cli.clipmod, "plan_scenes", lambda *a: {"scenes": old_scenes, "reason": "synthetic legacy scenes"})
    monkeypatch.setattr(cli.planmod, "pick_accents", lambda *a: [{"source_sample": 44113, "strength": .7}])
    from studio import score_alignment
    calls = []
    def score_analyze(y, sr, *, profile_id):
        calls.append((len(y), sr, profile_id))
        return alignment
    monkeypatch.setattr(score_alignment, "analyze", score_analyze)
    source = tmp_path / "synthetic-input.bin"
    source.write_bytes(b"synthetic bytes, not a recording")
    out = tmp_path / "analysis"
    args = ["studio_analyze.py", "--wav", str(source), "--out", str(out)]
    if profile:
        args += ["--profile", PROFILE]
    monkeypatch.setattr(sys, "argv", args)
    assert cli.main() == 0
    return json.loads((out / "plan-context.json").read_text()), calls, candidate, feat, old_scenes, source


def test_no_profile_preserves_default_context_fields_and_bytes(monkeypatch, tmp_path):
    actual, calls, candidate, feat, scenes, source = _mock_analysis(monkeypatch, tmp_path, profile=False)
    assert not calls
    candidate.update(scenes=scenes, scene_reason="synthetic legacy scenes",
                     accents=[{"source_sample": 44113, "strength": .7, "frame": 30}])
    expected = {"schema_version": "1.0", "source_sha256": cp.sha256_bytes(source.read_bytes()),
                "creative": {}, "analysis": cli.bounded_analysis_meta(feat), "sample_rate": 44100,
                "n_source_samples": 44100, "duration_sec": 1.0, "candidates": [candidate],
                "ranked_total": 17, "uncertainty": "synthetic caveat",
                "musical_quality": "not demonstrated; DSP ranking only, no listening"}
    assert actual == expected
    assert (tmp_path / "analysis/plan-context.json").read_bytes() == json.dumps(expected, indent=2).encode()
    assert (tmp_path / "analysis/candidates.json").read_bytes() == json.dumps({"candidates": [candidate]}, indent=2).encode()


def test_analyze_cli_profile_keeps_global_and_local_sample_evidence(monkeypatch, tmp_path):
    context, calls, _, _, _, _ = _mock_analysis(monkeypatch, tmp_path, profile=True)
    assert calls == [(44100, 44100, PROFILE)]
    assert context["analysis_profile"] == PROFILE
    assert context["score_alignment"] == context["candidates"][0]["score_alignment"]
    cp.validate_timing(context["candidates"][0])


def test_intake_stays_stdlib_only_and_workflow_passes_quoted_optional_profile():
    result = subprocess.run([sys.executable, "-S", "-c",
        "import sys; from studio import candidate_project; "
        "assert 'studio.score_alignment' not in sys.modules; assert 'numpy' not in sys.modules"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    workflow = yaml.safe_load((ROOT / ".github/workflows/studio-candidate.yml").read_text())
    step = next(s for s in workflow["jobs"]["reproduce"]["steps"] if s.get("name") == "Recompute DSP candidate (no manual timing edits)")
    assert step["env"]["ANALYSIS_PROFILE"] == "${{ steps.prepare.outputs.analysis_profile }}"
    assert 'profile_args+=(--profile "$ANALYSIS_PROFILE")' in step["run"]
    assert '"${profile_args[@]}"' in step["run"]
    assert "--project output/studio/work/project.json" in step["run"]
    assert subprocess.run(["bash", "-n"], input=step["run"], text=True).returncode == 0


def _load_script(name):
    spec = importlib.util.spec_from_file_location("score_profile_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_init_saves_profile_and_passes_it_only_to_trusted_analysis(monkeypatch, tmp_path):
    helper = _load_script("studio_candidate")
    source_project = json.loads((ROOT / "studio/projects/issue-1/project.json").read_text())
    url = source_project["source_url"]
    source = {"state": "open", "user": {"login": config.OWNER},
              "body": f"### Recording attachment\n{url}\n\n### Art / direction notes\nquiet"}
    class API:
        def get_issue(self, number): return source
        def request(self, method, path):
            assert (method, path) == ("GET", "/git/ref/heads/main")
            return {"object": {"sha": "a" * 40}}
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "github", lambda repo: API())
    monkeypatch.setattr(helper, "require_engine", lambda sha: None)
    monkeypatch.setattr(helper, "output_directory", lambda path: tmp_path / path)
    candidate = profiled(base_candidate("cand-01"))
    calls = []
    def run(name, *args):
        argv = list(map(str, args))
        calls.append((name, argv))
        def flag(key): return Path(argv[argv.index(key) + 1])
        if name == "studio_download.py":
            assert "--profile" not in argv
            helper.write_json(flag("--meta-out"), {"attachment_sha256": "a" * 64, "decoded_wav_sha256": "b" * 64})
            flag("--decode-out").write_bytes(b"synthetic placeholder, not a recording")
        else:
            assert name == "studio_analyze.py"
            assert argv[argv.index("--profile") + 1] == PROFILE
            helper.write_json(flag("--out") / "plan-context.json", {"candidates": [candidate], "analysis_profile": PROFILE})
    monkeypatch.setattr(helper, "run_script", run)
    helper.init(SimpleNamespace(issue=1, repo="test/repo", creator_id="synthetic-session",
                                work="output/profile-test", max_candidates=1, profile=PROFILE))
    directory = tmp_path / cp.candidate_dir(1, "cand-01")
    loaded = cp.load_candidate(directory, require_ready=False)
    assert loaded["project"]["analysis_profile"] == PROFILE
    assert loaded["candidate"] == candidate
    assert [name for name, _ in calls] == ["studio_download.py", "studio_analyze.py"]


def test_local_render_forwards_saved_profile_to_analysis_and_renderer(monkeypatch, tmp_path):
    helper = _load_script("studio_candidate")
    existing, _ = _candidate_documents(tmp_path / "fixture")
    target = tmp_path / cp.candidate_dir(1, "cand-01")
    target.parent.mkdir(parents=True)
    shutil.copytree(existing, target)
    monkeypatch.setattr(helper, "ROOT", tmp_path)
    monkeypatch.setattr(helper, "require_engine", lambda sha: None)
    output = tmp_path / "rendered"
    monkeypatch.setattr(helper, "output_directory", lambda path: output)
    brief = json.loads((target / "brief.json").read_text())
    calls = []
    def run_analysis(name, *args):
        argv = list(map(str, args))
        assert name == "studio_analyze.py"
        assert argv[argv.index("--profile") + 1] == PROFILE
        calls.append("analysis")
        helper.write_json(output / "analysis/plan-context.json", {
            "candidates": [brief["candidate"]], "analysis_profile": PROFILE,
            "source_sha256": brief["source_hashes"]["decoded_wav_sha256"]})
    monkeypatch.setattr(helper, "run_script", run_analysis)
    def run_render(argv, **kwargs):
        assert Path(argv[1]).name == "studio_render.py"
        assert argv[argv.index("--profile") + 1] == PROFILE
        calls.append("render")
        output.mkdir(exist_ok=True)
        for name in cp.MEDIA_NAMES:
            (output / name).write_bytes(b"synthetic render fixture")
        helper.write_json(output / "qa.json", {"pass": True, "checks": [
            {"name": n, "pass": True} for n in sorted(cp.QA_CHECK_NAMES)]})
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(helper.subprocess, "run", run_render)
    assert helper.render(SimpleNamespace(issue=1, candidate="cand-01", wav="synthetic-not-read.wav",
                                         creator_id="synthetic-session", round=0, out="output/rendered")) == 0
    assert calls == ["analysis", "render"]
    assert (output / "editorial.template.json").is_file()


@pytest.mark.parametrize("profile", [None, PROFILE, CLEARING_PROFILE, PROFILE + "\nunsafe=1"])
def test_actions_prepare_outputs_only_allowlisted_profile(monkeypatch, tmp_path, profile):
    helper = _load_script("studio_candidate_actions")
    for key, value in {"GITHUB_REF": "refs/heads/main", "GITHUB_EVENT_NAME": "workflow_dispatch",
                       "GITHUB_TRIGGERING_ACTOR": config.OWNER, "GITHUB_ACTOR": config.OWNER,
                       "GITHUB_REPOSITORY": "test/repo", "GH_TOKEN": "synthetic-test-token",
                       "GITHUB_SHA": "a" * 40, "GITHUB_OUTPUT": str(tmp_path / "output.txt")}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "argv", ["studio_candidate_actions.py", "prepare", "--pr", "33", "--work", str(tmp_path)])
    monkeypatch.setattr(helper, "GitHubClient", lambda *a: object())
    monkeypatch.setattr(helper.pipeline, "prepare_candidate", lambda *a, **k: {})
    def prepared(*args):
        project = {"source_url": "https://github.com/user-attachments/assets/synthetic"}
        if profile is not None:
            project["analysis_profile"] = profile
        (tmp_path / "project.json").write_text(json.dumps(project))
        return {"pr": 33, "head_sha": "a" * 40, "source_issue": 30, "candidate_id": "cand-02"}
    monkeypatch.setattr(helper.pipeline, "write_prepared", prepared)
    if profile not in (None, *config.SCORE_PROFILE_IDS):
        with pytest.raises(ValueError, match="unsupported analysis profile"):
            helper.main()
        assert not (tmp_path / "output.txt").exists()
    else:
        assert helper.main() == 0
        assert f"analysis_profile={profile or ''}\n" in (tmp_path / "output.txt").read_text()


@pytest.mark.parametrize("profile", ["", PROFILE, CLEARING_PROFILE])
def test_workflow_recompute_and_final_render_receive_the_same_optional_profile(tmp_path, profile):
    workflow = yaml.safe_load((ROOT / ".github/workflows/studio-candidate.yml").read_text())
    steps = workflow["jobs"]["reproduce"]["steps"]
    commands = [next(s for s in steps if s.get("name", "").startswith(name))
                for name in ("Recompute DSP candidate", "Validate storyboard and render")]
    # Execute the real workflow shell blocks with a synthetic python3 argv
    # recorder. This catches a missing --profile at either orchestration edge
    # without installing dependencies, decoding audio or invoking a renderer.
    binary = tmp_path / "python3"
    binary.write_text("#!" + sys.executable + "\nimport json,os,sys\nwith open(os.environ['ARGV_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n")
    binary.chmod(0o755)
    log = tmp_path / "argv.jsonl"
    env = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
           "ANALYSIS_PROFILE": profile, "ARGV_LOG": str(log)}
    for command in commands:
        assert command["env"]["ANALYSIS_PROFILE"] == "${{ steps.prepare.outputs.analysis_profile }}"
        result = subprocess.run(["bash", "-c", command["run"].replace("${{ github.sha }}", "a" * 40)],
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    argv = [json.loads(line) for line in log.read_text().splitlines()]
    analysis = next(a for a in argv if a[0] == "scripts/studio_analyze.py")
    rendering = next(a for a in argv if a[0] == "scripts/studio_render.py")
    for args in (analysis, rendering):
        if profile:
            assert args[args.index("--profile") + 1] == profile
            assert args.count("--profile") == 1
        else:
            assert "--profile" not in args


def test_profile_competitor_association_preserves_source_and_identity_guards():
    from studio import candidate_release as release
    from test_candidate_release import input_files, encoded, URL
    raw = input_files(30, "cand-02")
    project = json.loads(raw["project.json"])
    project["analysis_profile"] = PROFILE
    raw["project.json"] = encoded(project)
    kwargs = dict(issue=30, candidate="cand-02", source_url=URL)
    assert release._associated_candidate(raw, **kwargs)
    for key, value in (("analysis_profile", "unknown"), ("analysis_profile", None),
                       ("source_issue", 31), ("candidate_id", "cand-03"),
                       ("source_url", "https://github.com/user-attachments/files/999/other.mp3"),
                       ("unexpected", "metadata")):
        changed = {**project, key: value}
        assert not release._associated_candidate({**raw, "project.json": encoded(changed)}, **kwargs)


@pytest.mark.parametrize("author", [config.OWNER, "outsider"])
def test_profiled_competitor_closes_only_for_authorized_same_source(author):
    from studio import candidate_release as release
    from test_candidate_release import Transport, add_candidate, encoded, URL
    gh = Transport()
    gh.seed_commit({"README.md": b"synthetic engine"})
    issue = gh.seed_issue(body="synthetic source " + URL)
    selected, _ = add_candidate(gh, issue["number"], "cand-01", merged=False)
    rival, raw = add_candidate(gh, issue["number"], "cand-02", merged=False)
    rival["user"] = {"login": author}
    project = json.loads(raw["project.json"])
    project["analysis_profile"] = PROFILE
    tree = gh.trees[gh.commits[rival["head"]["sha"]]["tree_sha"]]
    path = release._input_path(issue["number"], "cand-02", "project.json")
    gh.blobs[tree["files"][path]] = encoded(project)
    closed = release._close_competitors(gh, issue=issue["number"], selected_pr=selected["number"], source_url=URL)
    assert closed == ([rival["number"]] if author == config.OWNER else [])
    assert rival["state"] == ("closed" if author == config.OWNER else "open")

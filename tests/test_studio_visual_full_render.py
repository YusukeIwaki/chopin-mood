"""Real CLI integration; all review verdicts here are TEST-ONLY fixtures."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import soundfile as sf

from scripts.studio_smoke import SR, synth
from studio import storyboard, visual_preflight as vp
from studio.render_context import prepare_context

ROOT = Path(__file__).resolve().parents[1]
ENGINE = "a" * 40


def test_approved_full_render_and_changed_input_rejected():
    (ROOT / "output").mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-full-test-", dir=ROOT / "output") as temp:
        work = Path(temp)
        wav = work / "synthetic.wav"
        sf.write(wav, synth(), SR, subtype="PCM_16")
        analyzed = subprocess.run([
            sys.executable, str(ROOT / "scripts/studio_analyze.py"),
            "--wav", str(wav), "--out", str(work),
        ], capture_output=True, text=True, cwd=ROOT, timeout=180)
        assert analyzed.returncode == 0, analyzed.stderr
        cand = json.loads((work / "candidates.json").read_text())["candidates"][0]
        assert cand["duration_sec"] >= 60
        assert all(s["duration_sec"] >= 6 for s in cand["scenes"])
        brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
                 "candidate_id": cand["id"], "fingerprint": "0" * 64,
                 "candidate": cand, "piece": None, "period": None,
                 "period_direction": None, "art_notes": "synthetic test only"}
        sb = storyboard.default_storyboard(cand["scenes"])
        for name, obj in (("candidate", cand), ("brief", brief), ("storyboard", sb)):
            (work / f"{name}.json").write_text(json.dumps(obj))
        ctx = prepare_context(wav, work / "candidate.json", work / "storyboard.json",
                              work / "brief.json", width=180, height=320)
        for stage in ("still", "motion"):
            directory = work / stage
            vp.generate_evidence(ctx, directory, stage=stage, engine_sha=ENGINE,
                                 run_id=123, run_attempt=1, iteration=0)
            digest = hashlib.sha256((directory / "evidence.json").read_bytes()).hexdigest()
            # Fixture only: never calls a model or certifies aesthetic quality.
            review = {"schema_version": "1.0", "stage": stage,
                      "evidence_sha256": digest, "verdict": "pass",
                      "checks": dict(evidence_reviewed=True, brief_adherence=True,
                                     visual_quality=True, motion_quality=True),
                      "blockers": [], "summary": "TEST-ONLY synthetic pass fixture",
                      "repair": {"route": "none", "instructions": []},
                      "listening_note": "audio listening unavailable; not a full musical judgement"}
            (directory / "review.json").write_text(json.dumps(review))
        approval = {"schema_version": "1.0", "engine_sha": ENGINE,
                    "run_id": 123, "run_attempt": 1,
                    "still_dir": str((work / "still").relative_to(ROOT)),
                    "motion_dir": str((work / "motion").relative_to(ROOT))}
        apath = work / "approval.json"
        apath.write_text(json.dumps(approval))
        attestation = vp.validate_approval_file(apath, ctx, expected_engine_sha=ENGINE)
        command = [sys.executable, str(ROOT / "scripts/studio_render.py"),
                   "--wav", str(wav), "--candidate", str(work / "candidate.json"),
                   "--brief", str(work / "brief.json"),
                   "--storyboard", str(work / "storyboard.json"),
                   "--width", "180", "--height", "320",
                   "--visual-approval", str(apath), "--engine-sha", ENGINE]
        rendered = subprocess.run(command + ["--out", str(work / "bundle")],
                                  capture_output=True, text=True, cwd=ROOT, timeout=300)
        assert rendered.returncode == 0, (rendered.stdout, rendered.stderr)
        qa = json.loads((work / "bundle/qa.json").read_text())
        assert qa["pass"] and len(qa["checks"]) == 11
        assert all(check["pass"] for check in qa["checks"])
        provenance = json.loads((work / "bundle/provenance.json").read_text())
        assert provenance["visual_approval"] == attestation
        for change in ("storyboard", "approval"):
            if change == "storyboard":
                sb["scenes"][0]["params"]["figure_scale"] = 1.2
                (work / "storyboard.json").write_text(json.dumps(sb))
            else:
                (work / "storyboard.json").write_text(json.dumps(ctx.storyboard))
                approval["run_id"] += 1
                apath.write_text(json.dumps(approval))
            target = work / f"rejected-{change}"
            rejected = subprocess.run(command + ["--out", str(target)],
                                      capture_output=True, text=True, cwd=ROOT, timeout=120)
            assert rejected.returncode != 0
            assert not (target / "video.mp4").exists()
            assert not (target / "clip_audio.wav").exists()

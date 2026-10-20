#!/usr/bin/env python3
"""Offline synthetic smoke: 75 s synthetic -> candidate -> storyboard ->
trusted render -> encoded-media QA. No network, secrets or personal audio.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from studio import storyboard as sbmod  # noqa: E402

SR = 22050


def synth(seconds: int = 75) -> np.ndarray:
    n = SR * seconds
    t = np.arange(n) / SR
    y = 0.08 * np.sin(2 * np.pi * 220 * t) + 0.05 * np.sin(2 * np.pi * 330 * t)
    for k, start in enumerate(range(3, seconds - 5, 6)):
        s = int(start * SR)
        L = min(int(2.5 * SR), n - s)
        te = np.arange(L) / SR
        freq = 220.0 * (1.25 ** (k % 4))
        y[s:s + L] += 0.35 * np.exp(-te * 3.0) * np.sin(2 * np.pi * freq * te)
    return (y * (0.7 + 0.3 * np.sin(2 * np.pi * t / seconds))).astype(np.float64)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="output/studio-smoke")
    ap.add_argument("--width", type=int, default=288)
    ap.add_argument("--height", type=int, default=512)
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    wav = out / "synthetic.wav"
    sf.write(str(wav), (synth() * 32767).astype(np.int16), SR, subtype="PCM_16")
    subprocess.run([sys.executable, str(ROOT / "scripts/studio_analyze.py"),
                    "--wav", str(wav), "--out", str(out)], check=True)
    cands = json.loads((out / "candidates.json").read_text())["candidates"]
    assert cands, "no 60-120s candidate from 75s synthetic"
    cand = cands[0]
    assert 60.0 <= cand["duration_sec"] <= 120.0, cand["duration_sec"]
    (out / "candidate.json").write_text(json.dumps(cand, indent=2))
    brief = {"schema_version": "1.0", "parent_issue": 1, "child_issue": 2,
             "candidate_id": cand["id"], "fingerprint": "0" * 64,
             "candidate": cand, "piece": None, "period": None,
             "period_direction": None, "art_notes": "synthetic smoke"}
    (out / "brief.json").write_text(json.dumps(brief, indent=2))
    sb = sbmod.default_storyboard(cand["scenes"])
    (out / "storyboard.json").write_text(json.dumps(sb, indent=2))
    # Small render size keeps CI fast; duration/frames stay native.
    bundle = out / "bundle"
    subprocess.run([sys.executable, str(ROOT / "scripts/studio_render.py"),
                    "--wav", str(wav), "--candidate", str(out / "candidate.json"),
                    "--storyboard", str(out / "storyboard.json"),
                    "--brief", str(out / "brief.json"), "--out", str(bundle),
                    "--width", str(a.width), "--height", str(a.height)],
                   check=True)
    qa = json.loads((bundle / "qa.json").read_text())
    assert qa["pass"], qa
    assert (bundle / "video.mp4").stat().st_size > 0
    print(json.dumps({"smoke": "PASS", "candidate": cand["id"],
                      "duration_sec": cand["duration_sec"],
                      "qa_pass": qa["pass"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

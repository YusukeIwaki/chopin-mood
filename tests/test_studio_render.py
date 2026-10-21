"""Full synthetic >=60 s render: real MP4, decoded-media QA, palette reach.

Runs the offline smoke (75 s synthetic -> 60-120 s candidate -> trusted
render) at reduced frame size. Duration/frames stay native (30 fps).
"""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def smoke(tmp_path_factory):
    out = tmp_path_factory.mktemp("smoke") / "smoke"
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/studio_smoke.py"),
         "--out", str(out), "--width", "180", "--height", "320"],
        capture_output=True, text=True, cwd=str(ROOT), timeout=1200)
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-2000:])
    return out


def test_synthetic_60s_render_and_qa(smoke):
    bundle = smoke / "bundle"
    assert (bundle / "video.mp4").stat().st_size > 100_000
    qa = json.loads((bundle / "qa.json").read_text())
    assert qa["pass"], qa
    plan = json.loads((bundle / "plan.json").read_text())
    dur = plan["clip"]["n_frames"] / 30
    assert 60.0 <= dur <= 120.0
    tl = json.loads((bundle / "timeline.json").read_text())
    assert tl["scenes"][0]["start_frame"] == 0
    assert tl["scenes"][-1]["end_frame"] == plan["clip"]["n_frames"]
    # Clean Studio QA: decoded-reference spotcheck replaces the legacy
    # P0 marker check; the other 10 file-level checks are retained.
    names = [c["name"] for c in qa["checks"]]
    assert any(n.endswith(":rendered_frame_spotcheck") for n in names), names
    assert not any(n.endswith(":marker_spotcheck") for n in names), names
    assert len(names) == 11, names


def test_storyboard_palette_reaches_decoded_frames(smoke):
    from p0 import qa as Q
    bundle = smoke / "bundle"
    sb = json.loads((bundle / "storyboard.json").read_text())
    sky = np.array(sb["scenes"][0]["palette"]["sky_top"], dtype=float)
    frame = Q.decode_frame_rgb(str(bundle / "video.mp4"), 10, 180, 320)
    # Sky corner away from the moon glow (x~133) and side vignette.
    top_mean = frame[:25, 20:70].reshape(-1, 3).mean(axis=0)
    dist_story = float(np.abs(top_mean - sky).mean())
    dist_other = float(np.abs(top_mean - np.array([200.0, 30.0, 30.0])).mean())
    assert dist_story < dist_other, (top_mean, sky)
    # Moon glow + warm wash legitimately lift the sky; bound stays well
    # below the distance to a wrong (red) palette.
    assert dist_story < 60, (top_mean, sky)

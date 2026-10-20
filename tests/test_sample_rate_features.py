"""Known acoustic transitions at native rates; not musical phrase labels."""
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from p0.events import detect_accents, detect_struct_boundaries
from p0.features import compute_features

ROOT = Path(__file__).resolve().parents[1]
RATES = (22050, 44100, 48000)


def transition(sr, center, gain=.07):
    t = np.arange(sr * 18) / sr
    a = np.clip((t - (center - .1)) / .2, 0, 1)
    old = sum(np.sin(2 * np.pi * f * t) for f in (220., 277.1826, 329.6276))
    new = sum(np.sin(2 * np.pi * f * t) for f in (196., 246.9417, 293.6648))
    return gain * (old * np.cos(a * np.pi / 2) + new * np.sin(a * np.pi / 2))


def nearest_harmonic(feat, center):
    events = [e for e in detect_struct_boundaries(feat)
              if "harm_novelty" in e["support"] and abs(e["time_sec"] - center) < 2]
    assert len(events) == 1, events
    return events[0]


@pytest.mark.parametrize("sr", RATES)
@pytest.mark.parametrize("center,lag", [(7.349, .75), (10.777, 1.5)])
@pytest.mark.parametrize("gain", [.07, .28])
def test_known_transition_default_calibration(sr, center, lag, gain):
    feat = compute_features(transition(sr, center, gain), sr, chroma_lag_sec=lag)
    event = nearest_harmonic(feat, center)
    # Acoustic calibration, separate from frame-accurate rendering tests.
    assert abs(event["source_sample"] / sr - center) <= 1 / 30
    assert event["source_sample"] == (
        event["novelty_start_sample"] + event["novelty_end_sample"]) // 2
    hop = feat["meta"]["hop"]
    assert np.array_equal(feat["centers"], np.arange(len(feat["centers"])) * hop)
    assert np.array_equal(feat["novelty_centers"],
                          (feat["novelty_start_samples"] + feat["novelty_end_samples"]) // 2)


@pytest.mark.parametrize("sr", [44100, 48000])
def test_legacy_fixed_samples_reproduce_high_rate_error(sr):
    center = 10.777
    y = transition(sr, center)
    old = nearest_harmonic(compute_features(y, sr, hop=512, n_fft=2048,
                                            chroma_lag_sec=1.5), center)
    new = nearest_harmonic(compute_features(y, sr, chroma_lag_sec=1.5), center)
    assert abs(old["time_sec"] - center) > .09
    assert abs(new["time_sec"] - center) < abs(old["time_sec"] - center)


def test_reference_rate_arrays_and_events_are_unchanged():
    y = transition(22050, 7.349)
    default = compute_features(y, 22050)
    explicit = compute_features(y, 22050, hop=512, n_fft=2048)
    for key in default:
        if key != "meta":
            np.testing.assert_array_equal(default[key], explicit[key], err_msg=key)
    assert detect_struct_boundaries(default) == detect_struct_boundaries(explicit)
    assert default["meta"]["hop_is_override"] is False
    assert explicit["meta"]["hop_is_override"] is True


@pytest.mark.parametrize("override,expected", [
    ({"hop": 256}, (256, 4458)),
    ({"n_fft": 4096}, (1115, 4096)),
    ({"hop": 512, "n_fft": 2048}, (512, 2048)),
])
def test_explicit_overrides_and_recorded_durations(override, expected):
    meta = compute_features(transition(48000, 7.349), 48000, **override)["meta"]
    assert (meta["hop"], meta["n_fft"]) == expected
    assert meta["hop_duration_sec"] == expected[0] / 48000
    assert meta["window_duration_sec"] == expected[1] / 48000
    assert meta["hop_is_override"] == ("hop" in override)
    assert meta["n_fft_is_override"] == ("n_fft" in override)


@pytest.mark.parametrize("sr", RATES)
def test_silence_stationary_chord_and_attack_controls(sr):
    assert detect_struct_boundaries(compute_features(np.zeros(sr * 5), sr)) == []
    t = np.arange(sr * 10) / sr
    steady = .1 * sum(np.sin(2 * np.pi * f * t) for f in (220., 277.1826, 329.6276))
    assert not any("harm_novelty" in e["support"]
                   for e in detect_struct_boundaries(compute_features(steady, sr)))
    # Single impulse has a known source-sample position at each gain.
    s = round(2.137 * sr)
    for gain in (.1, .4):
        y = np.zeros(sr * 5)
        y[s] = gain
        feat = compute_features(y, sr)
        events = detect_accents(feat)
        assert len(events) == 1, events
        assert abs(events[0]["source_sample"] - s) <= feat["meta"]["hop"]


def test_native_rate_analysis_metadata_reaches_planner_context(tmp_path):
    # Exercise the real CLI and JSON, not a mocked metadata serializer.
    sr = 48000
    t = np.arange(sr * 65) / sr
    source = tmp_path / "synthetic.wav"
    sf.write(source, .1 * np.sin(2 * np.pi * 220 * t), sr, subtype="FLOAT")
    out = tmp_path / "analysis"
    subprocess.run([sys.executable, str(ROOT / "scripts/studio_analyze.py"),
                    "--wav", str(source), "--out", str(out)],
                   check=True, capture_output=True, timeout=60)
    context = json.loads((out / "plan-context.json").read_text())
    assert context["sample_rate"] == sr
    assert context["analysis"]["hop"] == 1115
    assert context["analysis"]["n_fft"] == 4458
    assert len(json.dumps(context["analysis"])) < 2000
    assert context["candidates"]

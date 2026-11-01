from dataclasses import FrozenInstanceError, replace
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from scripts.build_score_reference import build, duration, parse_kern, pitch
from studio.score_alignment import (PROFILE_ID, ScoreAlignmentError, ScoreLandmark,
                                    ScoreNote, ScoreReference, _new_pitches,
                                    _refine, _score_chroma, _windows, analyze,
                                    load_reference)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "assets/score/chopin-op39-v1"


def test_registered_reference_bytes_rebuild_and_landmark_pitches(tmp_path):
    for name in ("039-1-TR.krn", "039-1-BH.krn", "LICENSE.txt", "ATTRIBUTION.md"):
        shutil.copyfile(BUNDLE / name, tmp_path / name)
    build(tmp_path)
    for source in BUNDLE.iterdir():
        if source.name == "README.md":
            continue
        assert (tmp_path / source.name).read_bytes() == source.read_bytes()
    ref = load_reference()
    assert ref.quarter_length == 1947
    assert [(m.measure, m.beat) for m in ref.landmarks] == [(542, 1), (567, 2), (573, 1)]
    assert _new_pitches(ref, ref.landmarks[1])[0] == [57, 69]
    assert _new_pitches(ref, ref.landmarks[2])[0] == [37]
    for edition in ("TR", "BH"):
        value = json.loads((BUNDLE / f"039-1-{edition}.krn.notes.json").read_text())
        assert all(F(n, d) == (m - 1) * 3 for m, n, d in value["measures"])
        expected = {1623: {(2, 49), (1, 56), (1, 61), (1, 65), (1, 68)},
                    1699: {(2, 57), (2, 69), (1, 81), (1, 93)},
                    1716: {(2, 37), (1, 56)}}
        for q, pitches in expected.items():
            assert {(r[5], r[4]) for r in value["notes"] if F(r[0], r[1]) == q} == pitches
        assert all(not r[9] for r in value["notes"] if F(r[0], r[1]) == 1716 and r[5] == 1)
    with pytest.raises(FrozenInstanceError):
        ref.quarter_length = F(1)
    with pytest.raises(TypeError):
        ref.provenance["source"]["commit"] = "changed"


def test_reference_rejects_missing_and_tampered_files(tmp_path):
    shutil.copytree(BUNDLE, tmp_path / "bundle")
    path = tmp_path / "bundle/039-1-TR.krn"
    path.write_text(path.read_text() + "!! altered\n")
    with pytest.raises(ScoreAlignmentError, match="hash mismatch"):
        load_reference(bundle_root=tmp_path / "bundle")
    path.unlink()
    with pytest.raises(ScoreAlignmentError):
        load_reference(bundle_root=tmp_path / "bundle")
    with pytest.raises(ScoreAlignmentError, match="unsupported score profile"):
        load_reference("unknown")


def test_kern_rational_clock_null_fork_merge_ties_grace_and_tuplets():
    text = """**kern\t**kern\t**dynam
*staff2\t*staff1\t*
=1\t=1\t=1
[2.C#\t4e#\tf
.\t.\t.
*\t*^\t*
*\t*\t*\t*
.\t8f#\t8a\t.
.\t8g#\t8b\t.
*\t*v\t*v\t*
.\t8qc#\t.
.\t4cc#\t.
=2\t=2\t=2
2.C#]\t16%3g#\t.
.\t16%3a\t.
.\t16%3b\t.
.\t16%3cc#\t.
*-\t*-\t*-
"""
    value = parse_kern(text)
    assert value["quarter_length"] == [6, 1]
    assert value["measures"] == [[1, 0, 1], [2, 3, 1]]
    rows = value["notes"]
    assert [F(r[0], r[1]) for r in rows[:6]] == [0, 0, 1, 1, F(3, 2), F(3, 2)]
    assert [(F(r[0], r[1]), F(r[2], r[3]), r[4]) for r in rows if r[2] == 0] == [(2, 0, 61)]
    assert [r[9] for r in rows if r[5] == 2] == [True, False]
    assert [F(r[0], r[1]) for r in rows if r[6] == 2 and r[5] == 1] == [3, F(15, 4), F(9, 2), F(21, 4)]
    assert duration("16%3g#") == F(3, 4)
    assert duration("4..c") == F(7, 4)
    assert duration("00C") == 16
    assert pitch("C##X") == 50
    assert pitch("B#X") == 60
    assert pitch("ccn") == 72
    assert pitch("8rGGG") is None
    with pytest.raises(ValueError, match="columns"):
        parse_kern(text.replace(".\t.\t.", ".\t.", 1))
    with pytest.raises(ValueError, match="unterminated"):
        parse_kern(text.replace("*-\t*-\t*-", "*\t*\t*"))


def test_mixed_chord_durations_preserved_and_spine_exchange():
    value = parse_kern("**kern\t**kern\n*staff2\t*staff1\n=1\t=1\n"
                       "4C 8E\t4g\n*x\t*x\n4a\t4D\n*-\t*-\n")
    assert value["quarter_length"] == [2, 1]
    assert [F(r[2], r[3]) for r in value["notes"][:3]] == [1, F(1, 2), 1]
    assert [(r[5], r[4]) for r in value["notes"][-2:]] == [(1, 69), (2, 50)]


def _tone(y, sr, sample, length, midi, amplitude=1.0, harmonics=True):
    n = min(length, len(y) - sample)
    if n <= 0:
        return
    t = np.arange(n) / sr
    hz = 440 * 2 ** ((midi - 69) / 12)
    ramp = max(1, round(.006 * sr))
    env = np.ones(n)
    env[:min(n, ramp)] = np.linspace(0, 1, min(n, ramp))
    release = min(n, round(.018 * sr))
    env[-release:] *= np.linspace(1, 0, release)
    wave = np.sin(2 * np.pi * hz * t)
    if harmonics:
        wave += .30 * np.sin(4 * np.pi * hz * t) + .15 * np.sin(6 * np.pi * hz * t)
    y[sample:sample + n] += amplitude * wave * env


def _synthetic(sr=16000):
    rng = np.random.default_rng(4409)
    notes = []
    times = [round(.35 * sr)]
    n_quarters = 44
    landmarks = (ScoreLandmark("major", F(12), 5, F(1)),
                 ScoreLandmark("octaves", F(25), 9, F(2)),
                 ScoreLandmark("fast", F(34), 12, F(2)))
    special = {12: [53, 60, 68], 25: [57, 69, 81], 34: [37, 64, 71]}
    for q in range(n_quarters):
        pitches = special.get(q, sorted(rng.choice(np.arange(48, 73), size=3, replace=False).tolist()))
        # Prevent the previous note from duplicating the diagnostic new bass.
        if q in (11, 24, 33):
            avoid = {12: 53, 25: 57, 34: 37}[q + 1]
            pitches = [m + 1 if m == avoid else m for m in pitches]
        for k, midi in enumerate(pitches):
            notes.append(ScoreNote(F(q), F(1), midi, 2 if k == 0 else 1,
                                   q // 3 + 1, F(q % 3 + 1), True))
        quarter_seconds = .31 + .08 * (1 + np.sin(q * .43)) / 2
        times.append(times[-1] + round(quarter_seconds * sr))
    y = np.zeros(times[-1] + round(.20 * sr))
    for note in notes:
        q = int(note.quarter)
        _tone(y, sr, times[q], round(.94 * (times[q + 1] - times[q])), note.midi, .07)
    ref = ScoreReference("synthetic", F(n_quarters), tuple(notes), landmarks,
                         {"reference_id": "synthetic-seed-4409"})
    return y, ref, [times[int(m.quarter)] for m in landmarks]


@pytest.mark.parametrize("sr", [16000, 22050])
def test_alignment_recovers_known_rubato_native_samples_without_mutation(sr):
    y, ref, expected = _synthetic(sr)
    before = hashlib.sha256(y.tobytes()).hexdigest()
    result = analyze(y, sr, profile_id="synthetic", reference=ref)
    assert hashlib.sha256(y.tobytes()).hexdigest() == before
    actual = [e["source_sample"] for e in result["landmarks"]]
    assert np.max(np.abs(np.asarray(actual) - expected)) <= round(.030 * sr)
    assert all(type(s) is int for s in actual)
    assert result == analyze(y, sr, profile_id="synthetic", reference=ref)
    assert len(json.dumps(result)) < 20000
    assert result["analysis"]["chroma_tuning_semitones"] == 0
    assert result["analysis"]["reference_steps_per_quarter"] == 2


def test_constant_wrong_pitch_missing_events_and_insufficient_coverage_rejected():
    y, ref, _ = _synthetic()
    sr = 16000
    for invalid in (np.zeros(3 * sr), np.ones(3 * sr), np.sin(np.arange(len(y)) * 2 * np.pi * 440 / sr)):
        with pytest.raises(ScoreAlignmentError):
            analyze(invalid, sr, profile_id="synthetic", reference=ref)
    wrong = np.zeros_like(y)
    for i in range(0, len(y), sr // 3):
        _tone(wrong, sr, i, sr // 4, 40 + (i // (sr // 3)) % 2, .1)
    with pytest.raises(ScoreAlignmentError):
        analyze(wrong, sr, profile_id="synthetic", reference=ref)
    missing = replace(ref, notes=tuple(n for n in ref.notes if n.quarter != 25))
    with pytest.raises(ScoreAlignmentError, match="no new symbolic pitch|score mismatch|ambiguous coarse"):
        analyze(y, sr, profile_id="synthetic", reference=missing)
    # Input containing only the last few seconds cannot cover the first event.
    with pytest.raises(ScoreAlignmentError):
        analyze(y[-4 * sr:], sr, profile_id="synthetic", reference=ref)


def test_silent_event_cannot_be_replaced_by_following_chord_with_same_bass():
    y, ref, positions = _synthetic()
    # q26 repeats A3 in a different chord. Without the mandatory compatibility
    # check a silent q25 can be "refined" to that wrong later attack.
    y[positions[1]:positions[1] + 6000] = 0
    with pytest.raises(ScoreAlignmentError, match="refined pitch-class mismatch"):
        analyze(y, 16000, profile_id="synthetic", reference=ref)


def test_subsequence_alignment_allows_unperformed_score_beginning():
    y, ref, expected = _synthetic()
    shift = F(20)
    prefix = tuple(ScoreNote(F(q), F(1), 40 + q % 9, 2, q // 3 + 1,
                             F(q % 3 + 1), True) for q in range(20))
    extended = replace(ref, quarter_length=ref.quarter_length + shift,
                       notes=prefix + tuple(replace(n, quarter=n.quarter + shift) for n in ref.notes),
                       landmarks=tuple(replace(m, quarter=m.quarter + shift) for m in ref.landmarks))
    result = analyze(y, 16000, profile_id="synthetic", reference=extended)
    assert np.max(np.abs(np.asarray([m["source_sample"] for m in result["landmarks"]]) - expected)) <= 480
    # Leading silence does not identify an exact score quarter, but it must
    # not force alignment through the unperformed twenty-quarter prefix.
    assert F(*result["analysis"]["score_coverage_quarters"][0]) > 0


def test_octave_refinement_does_not_let_880hz_impersonate_220hz():
    sr = 16000
    y = np.zeros(4 * sr)
    # RH A5 at 1.5s, followed by the actual LH A3/A4 at 2.0s.
    _tone(y, sr, round(1.5 * sr), sr, 81, .5)
    for midi in (57, 69):
        _tone(y, sr, 2 * sr, sr // 2, midi, .10)
    event = ScoreLandmark("octaves", F(1), 1, F(2))
    ref = ScoreReference("synthetic", F(3), (
        ScoreNote(F(0), F(1), 81, 1, 1, F(1), True),
        ScoreNote(F(1), F(1), 81, 1, 1, F(2), True),
        ScoreNote(F(1), F(1), 57, 2, 1, F(2), True),
        ScoreNote(F(1), F(1), 69, 2, 1, F(2), True)), (event,), {})
    n_fft, _, hop = _windows(sr)
    evidence = _refine(y, sr, ref, event, round(1.7 * sr), round(2.1 * sr), .2, n_fft, hop)
    assert abs(evidence["source_sample"] - 2 * sr) <= round(.03 * sr)
    absent = np.zeros_like(y)
    _tone(absent, sr, round(1.5 * sr), sr, 81, .5)
    with pytest.raises(ScoreAlignmentError, match="fundamental"):
        _refine(absent, sr, ref, event, round(1.7 * sr), round(2.1 * sr), .2, n_fft, hop)


def test_tied_pitch_occupancy_does_not_become_new_attack_or_bass_partial():
    sr = 16000
    y = np.zeros(4 * sr)
    # Continuing G#3 (207.65Hz) also equals partial3 of C#2 (69.30Hz).
    _tone(y, sr, round(1.55 * sr), 2 * sr, 56, .7)
    _tone(y, sr, 2 * sr, sr // 2, 37, .10)
    event = ScoreLandmark("fast", F(1), 1, F(2))
    ref = ScoreReference("synthetic", F(3), (
        ScoreNote(F(0), F(1), 56, 1, 1, F(1), True),
        ScoreNote(F(1), F(1), 56, 1, 1, F(2), False),
        ScoreNote(F(1), F(1), 37, 2, 1, F(2), True)), (event,), {})
    assert _new_pitches(ref, event)[0] == [37]
    chroma = _score_chroma(ref)
    assert chroma[56 % 12, 2] > 0
    n_fft, _, hop = _windows(sr)
    evidence = _refine(y, sr, ref, event, round(1.75 * sr), round(2.1 * sr), .2, n_fft, hop)
    assert abs(evidence["source_sample"] - 2 * sr) <= round(.03 * sr)
    assert [37, 3] in evidence["excluded_prior_harmonic_collisions"]


def _review_tone(y, sr, start, length, midi, amplitude):
    """Independent review reproducer's envelope, including release broadening."""
    n = min(length, len(y) - start)
    t = np.arange(n) / sr
    hz = 440 * 2 ** ((midi - 69) / 12)
    envelope = (np.minimum(1, np.arange(n) / (.006 * sr)) *
                np.minimum(1, (n - 1 - np.arange(n)) / (.018 * sr)))
    y[start:start + n] += amplitude * (np.sin(2 * np.pi * hz * t) +
        .3 * np.sin(4 * np.pi * hz * t) + .15 * np.sin(6 * np.pi * hz * t)) * envelope


def test_continuing_gsharp_release_alone_is_not_a_new_bass_fundamental():
    sr = 48000
    event = ScoreLandmark("fast", F(1), 1, F(2))
    ref = ScoreReference("synthetic", F(3), (
        ScoreNote(F(0), F(1), 56, 1, 1, F(1), True),
        ScoreNote(F(1), F(1), 56, 1, 1, F(2), False),
        ScoreNote(F(1), F(1), 37, 2, 1, F(2), True)), (event,), {})
    y = np.zeros(4 * sr)
    _review_tone(y, sr, round(1.5 * sr), round(.8 * sr), 56, .5)
    n_fft, _, hop = _windows(sr)
    with pytest.raises(ScoreAlignmentError, match="fundamental"):
        _refine(y, sr, ref, event, round(1.7 * sr), round(2.1 * sr), .2, n_fft, hop)
    _review_tone(y, sr, 2 * sr, sr // 2, 37, .1)
    result = _refine(y, sr, ref, event, round(1.7 * sr), round(2.1 * sr), .2, n_fft, hop)
    assert abs(result["source_sample"] - 2 * sr) <= round(.030 * sr)
    assert result["quality"]["lowest_fundamental_spectral_peak_ratio"] >= .001


@pytest.mark.parametrize("bass_present", [False, True])
def test_public_alignment_rejects_missing_bass_in_changing_context(bass_present):
    # The missing-bass negative used to pass analyze(), beyond the helper:
    # mean cost .117253, refined cost .388613 and a late false event from
    # the continuing G#3 release. No original recording is used here.
    sr = 48000
    rng = np.random.default_rng(4409)
    times, notes = [round(.35 * sr)], []
    for q in range(44):
        pitches = sorted(rng.choice(np.arange(48, 73), size=3, replace=False).tolist())
        if q == 33: pitches = [56]
        if q == 34: pitches = [37, 56]
        for k, midi in enumerate(pitches):
            notes.append(ScoreNote(F(q), F(1), midi, 1 if midi == 56 or k else 2,
                                   q // 3 + 1, F(q % 3 + 1), not (q == 34 and midi == 56)))
        times.append(times[-1] + round((.31 + .08 * (1 + np.sin(q * .43)) / 2) * sr))
    event = ScoreLandmark("fast", F(34), 12, F(2))
    ref = ScoreReference("synthetic", F(44), tuple(notes), (event,), {})
    y = np.zeros(times[-1] + round(.2 * sr))
    for note in notes:
        q = int(note.quarter)
        if q not in (33, 34):
            _review_tone(y, sr, times[q], round(.94 * (times[q + 1] - times[q])), note.midi, .07)
    _review_tone(y, sr, times[33], times[35] - times[33], 56, .5)
    if bass_present:
        _review_tone(y, sr, times[34], round(.94 * (times[35] - times[34])), 37, .1)
        result = analyze(y, sr, profile_id="synthetic", reference=ref)
        assert abs(result["landmarks"][0]["source_sample"] - times[34]) <= round(.030 * sr)
    else:
        with pytest.raises(ScoreAlignmentError, match="fundamental"):
            analyze(y, sr, profile_id="synthetic", reference=ref)

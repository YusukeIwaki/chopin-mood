from dataclasses import FrozenInstanceError, replace
from fractions import Fraction as F
import hashlib
import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from scripts.build_score_reference import build, duration, parse_kern, pitch
from studio.score_alignment import (PROFILE_ID, CLEARING_PROFILE_ID, ScoreAlignmentError, ScoreLandmark,
                                    ScoreNote, ScoreReference, _new_pitches,
                                    _refine, _score_chroma, _windows, analyze,
                                    load_reference)

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "assets/score/chopin-op39-v1"


def test_base_bundle_and_original_profile_provenance_remain_unchanged():
    from studio.score_alignment import _thaw
    assert hashlib.sha256((BUNDLE / "manifest.json").read_bytes()).hexdigest() == "d87df6f1d0d10aa206006e8d700e8c71fd0cf6f6ce07adb86dc7c4ed14fee960"
    assert hashlib.sha256((BUNDLE / "README.md").read_bytes()).hexdigest() == "7fc145a3b18393fa21f357446122e1033b439460285c1ba5e5daaccd6869c2a6"
    provenance = _thaw(load_reference().provenance)
    canonical = json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(canonical).hexdigest() == "ed8baced5d8ebe0aeaf7d1e9f5cd0e5227341b631954dce973fc386b663680c1"
    assert len(load_reference().landmarks) == 3
    assert "base_reference" not in provenance


def test_clearing_extension_reuses_pinned_score_and_targets_terminal_rh_only():
    from studio.score_alignment import _thaw
    base, extended = load_reference(), load_reference(CLEARING_PROFILE_ID)
    assert extended.notes == base.notes
    assert extended.landmarks[:3] == base.landmarks
    assert _thaw(extended.provenance["base_reference"]) == _thaw(base.provenance)
    event = extended.landmarks[-1]
    assert (event.id, event.quarter, event.measure, event.beat) == ("final_scale_arrival", 1860, 621, 1)
    assert event.refinement_midi == (100,)
    assert _new_pitches(extended, event)[0] == [100]
    assert any(n.midi == 32 and n.quarter == event.quarter for n in extended.notes)
    # Later E7 in broken figuration is a different written event.
    assert any(n.midi == 100 and n.quarter == F(3769, 2) for n in extended.notes)


@pytest.mark.parametrize("mutation", ["profile", "base", "quarter", "target", "ascent", "document"])
def test_clearing_extension_rejects_source_or_profile_substitution(tmp_path, mutation):
    extension = ROOT / "assets/score/chopin-op39-clearing-v1"
    shutil.copytree(extension, tmp_path / "extension")
    manifest_path = tmp_path / "extension/manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if mutation == "profile": manifest["profile_id"] = PROFILE_ID
    if mutation == "base": manifest["base_reference"]["manifest_sha256"] = "0" * 64
    if mutation == "quarter": manifest["appended_landmarks"][0]["score_quarter"] = [1929, 1]
    if mutation == "target": manifest["appended_landmarks"][0]["refinement_midi"] = [32]
    if mutation == "ascent": manifest["ascent_evidence"]["midi_sequence"][-1] = 99
    if mutation == "document": (tmp_path / "extension/README.md").write_text("altered")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ScoreAlignmentError):
        load_reference(CLEARING_PROFILE_ID, bundle_root=tmp_path / "extension")


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


def _terminal_scale_synthetic(sr, *, terminal_present=True, top_cents=0., fold=0,
                              terminal_amplitude=.07, simultaneous_bass_amplitude=.07,
                              decoy_amplitude=.07, duplicate_shift=None, scramble=False,
                              occluded=(), weak=(), legato_overlap=1.):
    """Known native-time ascent, simultaneous bass and later identical E7."""
    _, old, _ = _synthetic(sr)
    notes = [n for n in old.notes if n.quarter < 36]
    rng = np.random.default_rng(9017)
    for q in range(49, 56):
        notes += [ScoreNote(F(q), F(1), int(m), 1 if k else 2, q // 3 + 1, F(q % 3 + 1), True)
                  for k, m in enumerate(sorted(rng.choice(np.arange(48, 73), size=3, replace=False)))]
    sequence = [62, 63, 65, 67, 68, 70, 72, 73, 75, 77, 79, 80, 82,
                84, 85, 87, 89, 91, 92, 94, 95, 96, 97, 99, 100]
    for i, midi in enumerate(sequence):
        q = F(36) + F(i, 2)
        notes.append(ScoreNote(q, F(1) if i == 24 else F(1, 2), midi, 1,
                               int(q) // 3 + 1, q % 3 + 1, True))
    for q in range(36, 48):
        notes.append(ScoreNote(F(q), F(1), 40, 2, q // 3 + 1, F(q % 3 + 1), True))
    notes.append(ScoreNote(F(48), F(1, 2), 32, 2, 17, F(1), True))
    notes.append(ScoreNote(F(97, 2), F(1, 2), 37, 2, 17, F(3, 2), True))
    # A later E7 must not rescue a missing terminal note at q48.
    notes.append(ScoreNote(F(105, 2), F(1, 2), 100, 1, 18, F(5, 2), True))
    event = ScoreLandmark("final_scale_arrival", F(48), 17, F(1),
                          "terminal ascent arrival; held tone may ring", (100,))
    ref = ScoreReference("synthetic", F(56), tuple(sorted(notes, key=lambda n: n.quarter)),
                         old.landmarks + (event,), {"reference_id": "synthetic-terminal-scale"})
    times = [round(.35 * sr)]
    for q in range(56):
        times.append(times[-1] + round((.31 + .08 * (1 + np.sin(q * .43)) / 2) * sr))
    def at(q):
        index = int(q)
        return times[index] + round(float(q - index) * (times[index + 1] - times[index])) if index < 56 else times[-1]
    y = np.zeros(times[-1] + round(.20 * sr))
    for n in ref.notes:
        if not terminal_present and n.quarter == event.quarter and n.midi == 100:
            continue
        first, last = at(n.quarter), at(n.quarter + n.duration)
        in_ascent = n.staff == 1 and F(36) <= n.quarter <= F(48)
        midi, amplitude = n.midi, .07
        if in_ascent:
            note_index = int((n.quarter-36)*2)
            if note_index in occluded:
                continue
            if note_index in weak:
                amplitude *= .08
            midi += fold + top_cents*(n.midi-62)/38/100
            if scramble:
                index = int((n.quarter-36)*2)
                midi = sequence[23-index] if index < 24 else 100
            if n.quarter == event.quarter:
                amplitude = terminal_amplitude
        elif n.quarter == event.quarter and n.midi == 32:
            amplitude = simultaneous_bass_amplitude
        elif n.quarter == F(105, 2) and n.midi == 100:
            amplitude = decoy_amplitude
        length = round(.94*(last-first)*(legato_overlap if in_ascent and n.quarter < 48 else 1.))
        _tone(y,sr,first,length,midi,amplitude)
        if in_ascent and duplicate_shift is not None:
            _tone(y,sr,first+round(duplicate_shift*sr),length,midi,amplitude)
    return y, ref, times[48]


@pytest.mark.parametrize("sr", [16000, 48000])
def test_four_landmark_terminal_ascent_recovers_actual_rh_attack(sr):
    y, ref, expected = _terminal_scale_synthetic(sr)
    result = analyze(y, sr, profile_id="synthetic", reference=ref)
    assert len(result["landmarks"]) == 4
    final = result["landmarks"][-1]
    assert final["id"] == "final_scale_arrival"
    assert final["target_new_midi"] == [100]
    assert abs(final["source_sample"] - expected) <= round(.030 * sr)
    assert result["analysis"]["acceptance_limits"] == {
        "maximum_mean_cosine_cost": .42, "maximum_landmark_local_cost": .60,
        "maximum_refined_cosine_cost": .50, "minimum_direct_fundamental_spectral_peak_ratio": .001,
        "maximum_coarse_interval_seconds": 2.0}


def test_missing_terminal_e7_is_not_replaced_by_concurrent_bass_or_later_e7():
    y, ref, _ = _terminal_scale_synthetic(48000, terminal_present=False)
    assert any(n.quarter == 48 and n.midi == 32 for n in ref.notes)
    assert any(n.quarter > 48 and n.midi == 100 for n in ref.notes)
    with pytest.raises(ScoreAlignmentError, match="fundamental|pitch|coarse|ascent"):
        analyze(y, 48000, profile_id="synthetic", reference=ref)


@pytest.mark.parametrize("sr", [16000, 48000])
@pytest.mark.parametrize("gain", [.1, 1.])
def test_prefix_registered_stretch_with_rubato_and_stronger_simultaneous_bass(sr, gain):
    y, ref, expected = _terminal_scale_synthetic(sr, top_cents=20.,
        terminal_amplitude=.03, simultaneous_bass_amplitude=.5, decoy_amplitude=.5)
    y *= gain
    before = hashlib.sha256(y.tobytes()).hexdigest()
    result = analyze(y, sr, profile_id="synthetic", reference=ref)
    final = result["landmarks"][-1]
    assert abs(final["source_sample"]-expected) <= round(.030*sr)
    trace = final["register_sequence"]
    assert len(trace["ordered_trajectory"]["onset_samples"]) == 25
    assert np.all(np.diff(trace["ordered_trajectory"]["onset_samples"]) > 0)
    assert abs(trace["tuning"]["terminal_cents"]-20.) < 6.
    assert trace["tuning"]["inlier_prefix_notes"] >= 18
    assert result == analyze(y, sr, profile_id="synthetic", reference=ref)
    assert hashlib.sha256(y.tobytes()).hexdigest() == before
    assert "terminal_register_alignment" in result["analysis"]


@pytest.mark.parametrize("variant", ["missing", "folded", "scrambled", "partials", "neighbor", "broadband"])
def test_terminal_register_path_rejects_missing_scrambled_and_folded_ascent(variant):
    kwargs = {"top_cents": 20., "simultaneous_bass_amplitude": .5, "decoy_amplitude": .6}
    if variant == "missing": kwargs["terminal_present"] = False
    if variant == "folded": kwargs["fold"] = -12
    if variant in ("partials", "neighbor", "broadband"): kwargs["terminal_present"] = False
    if variant == "scrambled": kwargs["scramble"] = True
    y, ref, expected = _terminal_scale_synthetic(48000, **kwargs)
    if variant == "partials":
        # An E6 fundamental with a second partial at the expected E7 is not
        # the required high-register attack, even after a correct prefix.
        _tone(y, 48000, expected, 48000//3, 88+.20, .3)
    if variant == "neighbor":
        _tone(y, 48000, expected, 48000//3, 99+.20, .3)
    if variant == "broadband":
        rng = np.random.default_rng(20261102)
        y[expected:expected+1200] += rng.normal(0, .1, 1200)
    # Keep the spectral/register test independent of the old three cues.
    ref = replace(ref, landmarks=(ref.landmarks[-1],))
    with pytest.raises(ScoreAlignmentError, match="ascent|register|fundamental|coarse|pitch"):
        analyze(y, 48000, profile_id="synthetic", reference=ref)


@pytest.mark.parametrize("shift", [.15, .20, .25, .30])
def test_competing_complete_ascent_paths_abstain(shift):
    y, ref, _ = _terminal_scale_synthetic(48000, duplicate_shift=shift)
    ref = replace(ref, landmarks=(ref.landmarks[-1],))
    with pytest.raises(ScoreAlignmentError, match="ambiguous|ascent|registration|coarse|register"):
        analyze(y, 48000, profile_id="synthetic", reference=ref)


def test_earlier_and_later_stronger_identical_pitch_do_not_replace_the_terminal():
    sr = 48000
    y, ref, expected = _terminal_scale_synthetic(sr, top_cents=20.,
        terminal_amplitude=.03, decoy_amplitude=.6)
    _tone(y, sr, expected-2*sr, sr//5, 100+.20, .6)
    ref = replace(ref, landmarks=(ref.landmarks[-1],))
    final = analyze(y, sr, profile_id="synthetic", reference=ref)["landmarks"][0]
    assert abs(final["source_sample"]-expected) <= round(.030*sr)


def test_ascent_history_search_retains_paths_that_pass_later_rhythm_gate():
    from studio.score_alignment import _ordered_ascent_paths
    # Cheaper recent histories converge on the same penultimate candidate,
    # then fail the final local-rhythm gate. Two routes per last node loses
    # every valid full path; last-seven history is necessary for this gate.
    samples = np.arange(2501, dtype=np.int64)
    candidates = [[(100*j,1.)] for j in range(17)]
    for regular, late in zip(range(1700,2300,100),[1800,2000,2200,2240,2260,2280]):
        candidates.append([(regular,.6),(late,1.)])
    candidates += [[(2300,1.)],[(2400,1.)]]
    paths = _ordered_ascent_paths(candidates,samples,100.,1)
    assert paths
    assert paths[0][1] == list(enumerate(list(range(0,1700,100))+
                                        [1700,1800,1900,2240,2260,2280,2300,2400]))
    assert paths[0][0] == pytest.approx(-3*np.log(.6))


def _shifted_ascent_path(sr, *, terminal_offset=.2):
    from studio.score_alignment import _windows
    times = [round(.35*sr)]
    for q in range(56):
        times.append(times[-1]+round((.31+.08*(1+np.sin(q*.43))/2)*sr))
    _,coarse_hop,_ = _windows(sr)
    path = []
    for j in range(25):
        q = F(36)+F(j,2)
        first = times[int(q)]+round(float(q-int(q))*(times[int(q)+1]-times[int(q)]))
        offset = {21:.45,22:.35,23:.25,24:terminal_offset}.get(j,.1)
        path.append((round((first+offset*sr)/coarse_hop),int(q*2)))
    return np.asarray(path,dtype=np.int64)


@pytest.mark.parametrize("sr",[16000,48000])
def test_local_absolute_register_sequence_recovers_late_prefix_chroma_anchors(sr):
    from studio.score_alignment import _ascent_refine
    y,ref,expected = _terminal_scale_synthetic(sr,top_cents=20.,
        terminal_amplitude=.03,simultaneous_bass_amplitude=.5,decoy_amplitude=.6)
    n_fft,coarse_hop,fine_hop = _windows(sr)
    path = _shifted_ascent_path(sr)
    event = _ascent_refine(y,sr,ref,ref.landmarks[-1],path,coarse_hop,.2,n_fft,fine_hop)
    trace = event["register_sequence"]
    assert abs(event["source_sample"]-expected) <= round(.030*sr)
    # Prefix96 is beyond its misleading chroma cell's original fine window.
    # It is recovered by full ordered register context, not a larger terminal
    # window. The terminal itself remains inside its original native bound.
    assert trace["dtw_intervals_samples"][21][0]-trace["ordered_trajectory"]["onset_samples"][21] > round(.35*sr)
    low,high = event["coarse_interval_samples"]
    assert low-round(.35*sr) <= event["source_sample"] <= high+round(.35*sr)


def test_terminal_refinement_bound_is_not_expanded_by_local_sequence_search():
    from studio.score_alignment import _ascent_refine
    sr = 48000
    y,ref,_ = _terminal_scale_synthetic(sr,decoy_amplitude=.6)
    n_fft,coarse_hop,fine_hop = _windows(sr)
    with pytest.raises(ScoreAlignmentError,match="ascent|fundamental"):
        _ascent_refine(y,sr,ref,ref.landmarks[-1],_shifted_ascent_path(sr,terminal_offset=.9),
                       coarse_hop,.2,n_fft,fine_hop)


@pytest.mark.parametrize("sr",[16000,48000])
@pytest.mark.parametrize("occluded",[(1,),(5,14),(23,),(2,19)])
def test_bounded_isolated_prefix_occlusions_preserve_known_terminal(sr,occluded):
    y,ref,expected = _terminal_scale_synthetic(sr,top_cents=20.,occluded=occluded,
        terminal_amplitude=.03,simultaneous_bass_amplitude=.5,decoy_amplitude=.6)
    final = analyze(y,sr,profile_id="synthetic",reference=ref)["landmarks"][-1]
    assert abs(final["source_sample"]-expected) <= round(.030*sr)
    trace = final["register_sequence"]
    assert trace["tuning"]["inlier_prefix_notes"] >= 18
    assert trace["ordered_trajectory"]["scope"].startswith("diagnostic_only")


@pytest.mark.parametrize("sr,weak",[(16000,(1,8)),(48000,(1,8,17)),(16000,(1,8,17))])
def test_overlapped_weak_prefix_does_not_certify_individual_keystrikes(sr,weak):
    y,ref,expected = _terminal_scale_synthetic(sr,top_cents=20.,weak=weak,legato_overlap=2.4,
        terminal_amplitude=.03,simultaneous_bass_amplitude=.5,decoy_amplitude=.6)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    final = analyze(y,sr,profile_id="synthetic",reference=ref)["landmarks"][0]
    assert abs(final["source_sample"]-expected) <= round(.030*sr)
    assert final["register_sequence"]["ordered_trajectory"]["scope"].startswith("diagnostic_only")


@pytest.mark.parametrize("occluded",[(1,5,14),(1,2),(19,21)])
def test_missing_prefix_strikes_do_not_imply_missing_terminal(occluded):
    y,ref,expected = _terminal_scale_synthetic(48000,occluded=occluded,decoy_amplitude=.6)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    final = analyze(y,48000,profile_id="synthetic",reference=ref)["landmarks"][0]
    assert abs(final["source_sample"]-expected) <= round(.030*48000)
    assert final["register_sequence"]["tuning"]["inlier_prefix_notes"] >= 18


def test_prefix_occlusion_never_makes_terminal_optional_or_resolves_duplicate_paths():
    for kwargs in ({"terminal_present":False,"decoy_amplitude":.6}, {"duplicate_shift":.2}):
        y,ref,_ = _terminal_scale_synthetic(48000,occluded=(5,14),**kwargs)
        ref = replace(ref,landmarks=(ref.landmarks[-1],))
        with pytest.raises(ScoreAlignmentError,match="ascent|register|fundamental|ambiguous|coarse"):
            analyze(y,48000,profile_id="synthetic",reference=ref)


@pytest.mark.parametrize("sr",[16000,48000])
@pytest.mark.parametrize("terminal_present",[False,True])
def test_new_high_attack_is_separated_from_strong_continuing_lower_note(sr,terminal_present):
    y,ref,expected = _terminal_scale_synthetic(sr,top_cents=20.,terminal_present=terminal_present,
        terminal_amplitude=.03,simultaneous_bass_amplitude=.5,decoy_amplitude=.6)
    # E6 and its E7 partial are already ringing and decaying when a separate
    # weaker E7 attacks. Static fundamental energy dominance rejects this
    # genuine attack; coincident *new* lower-note growth must be distinguished.
    t = np.arange(2*sr)/sr
    hz = 440*2**((88.2-69)/12)
    phase = 2*np.pi*hz*(t-1.)
    envelope = np.exp(-t)
    envelope[:round(.01*sr)] *= np.linspace(0.,1.,round(.01*sr))
    y[expected-sr:expected+sr] += .5*envelope*(np.sin(phase)+.3*np.sin(2*phase)+.15*np.sin(3*phase))
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    if terminal_present:
        if sr == 16000:
            # Public chroma localization of this heavy-interference fixture
            # does not provide enough prefix tuning observations. It must
            # abstain; a known-coarse-path helper isolates the temporal gate.
            with pytest.raises(ScoreAlignmentError,match="insufficient preceding") as failure:
                analyze(y,sr,profile_id="synthetic",reference=ref)
            assert len(failure.value.diagnostics["prefix_tuning_observations"]) < 18
            from studio.score_alignment import _ascent_refine
            n_fft,coarse_hop,fine_hop = _windows(sr)
            final = _ascent_refine(y,sr,ref,ref.landmarks[-1],_shifted_ascent_path(sr),
                                   coarse_hop,.2,n_fft,fine_hop)
        else:
            final = analyze(y,sr,profile_id="synthetic",reference=ref)["landmarks"][0]
        assert abs(final["source_sample"]-expected) <= round(.030*sr)
        assert final["quality"]["terminal_written_register_share"] < .5
        assert final["quality"]["terminal_written_register_growth_share"] >= .5
    else:
        with pytest.raises(ScoreAlignmentError,match="ascent|register|fundamental|coarse"):
            analyze(y,sr,profile_id="synthetic",reference=ref)


def test_terminal_public_result_and_rejection_diagnostics_are_strict_json():
    y,ref,_ = _terminal_scale_synthetic(48000,top_cents=20.)
    result = analyze(y,48000,profile_id="synthetic",reference=ref)
    json.dumps(result,allow_nan=False)
    y,ref,_ = _terminal_scale_synthetic(48000,terminal_present=False)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    with pytest.raises(ScoreAlignmentError) as failure:
        analyze(y,48000,profile_id="synthetic",reference=ref)
    json.dumps(failure.value.diagnostics,allow_nan=False)


@pytest.mark.parametrize("offset",[-.20,.15,.25])
def test_sole_inside_window_e7_growth_does_not_certify_written_identity(offset):
    sr = 48000
    y,ref,nominal = _terminal_scale_synthetic(sr,terminal_present=False,top_cents=20.)
    actual = nominal+round(offset*sr)
    _tone(y,sr,actual,sr//5,100.2,.07)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    event = analyze(y,sr,profile_id="synthetic",reference=ref)["landmarks"][0]
    # This is an observed identity limit, not a passed wrong-note negative:
    # the only local tuned E7 is detected even if its intended score identity
    # was labelled differently by the synthetic fixture's author.
    assert abs(event["source_sample"]-actual) <= round(.030*sr)
    assert abs(event["source_sample"]-nominal) > round(.1*sr)


@pytest.mark.parametrize("sr",[16000,48000])
@pytest.mark.parametrize("ramp,amplitude",[(.006,.005),(.08,.02)])
def test_weak_or_slow_second_terminal_attack_cannot_be_pruned_into_uniqueness(sr,ramp,amplitude):
    y,ref,expected = _terminal_scale_synthetic(sr,top_cents=20.)
    start,n = expected+round(.2*sr),sr//4
    t = (np.arange(n)+start-expected)/sr
    hz = 440*2**((100.2-69)/12)
    env = np.minimum(1.,np.arange(n)/max(1,round(ramp*sr)))
    env[-round(.018*sr):] *= np.linspace(1.,0.,round(.018*sr))
    y[start:start+n] += amplitude*env*(np.sin(2*np.pi*hz*t)+
                                      .3*np.sin(4*np.pi*hz*t)+.15*np.sin(6*np.pi*hz*t))
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    with pytest.raises(ScoreAlignmentError,match="ambiguous competing terminal"):
        analyze(y,sr,profile_id="synthetic",reference=ref)


@pytest.mark.parametrize("modulated",[False,True])
def test_ringing_e7_growth_limit_is_distinct_from_a_certified_new_keystrike(modulated):
    sr = 48000
    y,ref,expected = _terminal_scale_synthetic(sr,terminal_present=False,top_cents=20.)
    start,n = expected-sr,round(1.5*sr)
    t = np.arange(n)/sr
    phase = 2*np.pi*440*2**((100.2-69)/12)*(t-1.)
    envelope = .05*(1+np.sin(np.pi*(t-1.))) if modulated else .08*np.exp(-.2*t)
    envelope[:480] *= np.linspace(0.,1.,480)
    y[start:start+n] += envelope*np.sin(phase)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    if modulated:
        # Smooth amplitude modulation can supply the same bounded spectral
        # growth evidence without a fresh physical strike. Document this
        # limitation explicitly; do not label it a rejected negative.
        event = analyze(y,sr,profile_id="synthetic",reference=ref)["landmarks"][0]
        assert expected-round(.35*sr) <= event["source_sample"] <= expected+round(.35*sr)
    else:
        with pytest.raises(ScoreAlignmentError,match="missing terminal"):
            analyze(y,sr,profile_id="synthetic",reference=ref)


def test_wrong_global_score_context_cannot_be_rescued_by_injected_e7():
    sr = 48000
    y,ref,expected = _terminal_scale_synthetic(sr)
    y[:] = 0.
    times = [round(.35*sr)]
    for q in range(56):
        times.append(times[-1]+round((.31+.08*(1+np.sin(q*.43))/2)*sr))
    def at(q):
        i = int(q)
        return times[i]+round(float(q-i)*(times[i+1]-times[i])) if i < 56 else times[-1]
    for note in ref.notes:
        first,last = at(note.quarter),at(note.quarter+note.duration)
        _tone(y,sr,first,round(.94*(last-first)),note.midi+1,.07)
    _tone(y,sr,expected,sr//4,100,.07)
    ref = replace(ref,landmarks=(ref.landmarks[-1],))
    with pytest.raises(ScoreAlignmentError,match="score mismatch|coarse|registration"):
        analyze(y,sr,profile_id="synthetic",reference=ref)

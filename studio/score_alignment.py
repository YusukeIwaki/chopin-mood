"""Optional, local score alignment against a registered symbolic reference.

The symbolic landmarks describe *what* happens in the written score. Chroma
DTW finds their approximate performed position; register-specific new-pitch
growth refines that position on the native recording sample grid. No recording
is uploaded, rewritten or resampled. A successful result is signal evidence,
not a probability of musical correctness or a replacement for listening.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

import librosa
import numpy as np
from scipy.ndimage import gaussian_filter1d

PROFILE_ID = "chopin-scherzo3-coda-v1"
CLEARING_PROFILE_ID = "chopin-scherzo3-coda-clearing-v1"
_BUNDLE = Path(__file__).resolve().parents[1] / "assets/score/chopin-op39-v1"
_CLEARING_BUNDLE = _BUNDLE.parent / "chopin-op39-clearing-v1"
_CLEARING_BASE_SHA256 = "d87df6f1d0d10aa206006e8d700e8c71fd0cf6f6ce07adb86dc7c4ed14fee960"
_PPQ = 2
_COST_QUANTUM = 0.000001
_MAX_MEAN_COST = 0.42
_MAX_LOCAL_COST = 0.60
_MAX_ANCHOR_COST = 0.50
_MIN_FUNDAMENTAL_SPECTRAL_RATIO = 0.001
_MAX_INTERVAL_SECONDS = 2.0
_REFINE_PADDING_SECONDS = 0.35
# The optional terminal-ascent path registers a piano's local stretch from
# preceding notes. Bounds keep every hypothesis inside its written semitone.
_ASCENT_MAX_CENTS = 40.0
_ASCENT_FIT_RESIDUAL_CENTS = 10.0
_ASCENT_MIN_FIT_NOTES = 18


class ScoreAlignmentError(ValueError):
    """The reference or signal does not support a usable alignment."""


@dataclass(frozen=True)
class ScoreNote:
    quarter: Fraction
    duration: Fraction
    midi: int
    staff: int
    measure: int
    beat: Fraction
    attack: bool
    line: int = 0


@dataclass(frozen=True)
class ScoreLandmark:
    """Written score intent; ``meaning`` does not certify an acoustic event."""
    id: str
    quarter: Fraction
    measure: int
    beat: Fraction
    meaning: str = ""
    refinement_midi: tuple[int, ...] = ()


@dataclass(frozen=True)
class ScoreReference:
    profile_id: str
    quarter_length: Fraction
    notes: tuple[ScoreNote, ...]
    landmarks: tuple[ScoreLandmark, ...]
    provenance: Mapping


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(x) for x in value)
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(x) for x in value]
    return value


def _fraction(value) -> Fraction:
    if (not isinstance(value, list) or len(value) != 2 or
            any(type(x) is not int for x in value) or value[1] <= 0):
        raise ScoreAlignmentError("invalid score rational")
    result = Fraction(*value)
    if [result.numerator, result.denominator] != value:
        raise ScoreAlignmentError("score rational is not canonical")
    return result


def _pair(value: Fraction) -> list[int]:
    return [value.numerator, value.denominator]


def load_reference(profile_id: str = PROFILE_ID, *, bundle_root=None) -> ScoreReference:
    """Validate all bundled bytes and return immutable symbolic facts.

    The manifest hash is returned as provenance too, so landmark definitions,
    parser policy and licenses are bound to the reproducible result. No cache
    hides edits to registered files in a local working tree.
    """
    if profile_id == CLEARING_PROFILE_ID:
        return _load_clearing_reference(bundle_root)
    if profile_id != PROFILE_ID:
        raise ScoreAlignmentError(f"unsupported score profile: {profile_id!r}")
    root = Path(bundle_root) if bundle_root is not None else _BUNDLE
    try:
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if manifest["schema_version"] != 1 or manifest["profile_id"] != profile_id:
            raise ScoreAlignmentError("score manifest profile/schema mismatch")
        expected = {"039-1-TR.krn", "039-1-TR.krn.notes.json", "039-1-BH.krn",
                    "039-1-BH.krn.notes.json", "LICENSE.txt", "ATTRIBUTION.md"}
        if set(manifest["files"]) != expected:
            raise ScoreAlignmentError("unexpected score bundle allowlist")
        for name, digest in manifest["files"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise ScoreAlignmentError(f"score reference hash mismatch: {name}")
        if (manifest["primary"] != "039-1-TR.krn.notes.json" or
                manifest["corroborating"] != "039-1-BH.krn.notes.json" or
                manifest["license"]["id"] != "CC-BY-4.0"):
            raise ScoreAlignmentError("unexpected score source/license")
        primary = json.loads((root / manifest["primary"]).read_bytes())
        expected_columns = ["quarter_n", "quarter_d", "duration_n", "duration_d",
                            "midi", "staff", "measure", "beat_n", "beat_d", "attack", "line"]
        if primary["schema_version"] != 1 or primary["note_columns"] != expected_columns:
            raise ScoreAlignmentError("unsupported derived score schema")
        notes = []
        for row in primary["notes"]:
            if len(row) != 11 or any(type(row[i]) is not int for i in range(11) if i != 9):
                raise ScoreAlignmentError("invalid derived score note")
            if type(row[9]) is not bool or not 0 <= row[4] <= 127 or row[5] not in (1, 2):
                raise ScoreAlignmentError("invalid derived score pitch/staff/attack")
            note = ScoreNote(_fraction(row[0:2]), _fraction(row[2:4]), row[4], row[5],
                             row[6], _fraction(row[7:9]), row[9], row[10])
            if note.quarter < 0 or note.duration < 0:
                raise ScoreAlignmentError("negative score time")
            notes.append(note)
        landmarks = tuple(ScoreLandmark(x["id"], _fraction(x["score_quarter"]),
                                        x["measure"], _fraction(x["beat"]), x["meaning"])
                          for x in manifest["landmarks"])
        length = _fraction(primary["quarter_length"])
        if (length != 1947 or len(landmarks) != 3 or
                tuple((m.id, m.quarter, m.measure, m.beat) for m in landmarks) != (
                    ("major_arrival", Fraction(1623), 542, Fraction(1)),
                    ("both_hands_octaves", Fraction(1699), 567, Fraction(2)),
                    ("fast_coda", Fraction(1716), 573, Fraction(1)))):
            raise ScoreAlignmentError("registered score landmarks changed")
        for landmark in landmarks:
            if not any(n.quarter == landmark.quarter and n.measure == landmark.measure and
                       n.beat == landmark.beat and n.attack for n in notes):
                raise ScoreAlignmentError(f"missing symbolic event: {landmark.id}")
        provenance = {"reference_id": manifest["reference_id"],
                      "bundle_path": "assets/score/chopin-op39-v1",
                      "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                      "source": manifest["source"], "license": manifest["license"],
                      "derivation": manifest["derivation"], "files": manifest["files"]}
        return ScoreReference(profile_id, length, tuple(notes), landmarks, _freeze(provenance))
    except ScoreAlignmentError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ScoreAlignmentError("invalid or unavailable registered score reference") from exc


def _load_clearing_reference(bundle_root=None) -> ScoreReference:
    """Extend the validated v1 score without rewriting its facts/provenance."""
    base = load_reference(PROFILE_ID)
    root = Path(bundle_root) if bundle_root is not None else _CLEARING_BUNDLE
    try:
        raw = (root / "manifest.json").read_bytes()
        manifest = json.loads(raw)
        if set(manifest) != {"schema_version", "profile_id", "reference_id", "base_reference",
                             "appended_landmarks", "ascent_evidence", "limits", "files"}:
            raise ScoreAlignmentError("unexpected clearing extension manifest fields")
        expected_base = {"profile_id": PROFILE_ID,
                         "bundle_path": "assets/score/chopin-op39-v1",
                         "manifest_sha256": _CLEARING_BASE_SHA256}
        if (manifest["schema_version"] != 1 or manifest["profile_id"] != CLEARING_PROFILE_ID or
                manifest["reference_id"] != "chopin-op39-clearing-v1" or
                manifest["base_reference"] != expected_base or
                base.provenance["manifest_sha256"] != _CLEARING_BASE_SHA256):
            raise ScoreAlignmentError("clearing extension/base reference identity mismatch")
        if set(manifest["files"]) != {"README.md"}:
            raise ScoreAlignmentError("unexpected clearing extension file allowlist")
        for name, digest in manifest["files"].items():
            if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
                raise ScoreAlignmentError(f"clearing extension hash mismatch: {name}")
        facts = manifest["appended_landmarks"]
        if (not isinstance(facts, list) or len(facts) != 1 or not isinstance(facts[0], dict) or
                type(facts[0]["measure"]) is not int or
                any(type(p) is not int for p in facts[0]["refinement_midi"]) or
                (facts[0]["id"], _fraction(facts[0]["score_quarter"]), facts[0]["measure"],
                 _fraction(facts[0]["beat"]), facts[0]["refinement_midi"]) !=
                ("final_scale_arrival", Fraction(1860), 621, Fraction(1), [100]) or
                facts[0]["boundary_kind"] != "terminal_ascent_note_attack"):
            raise ScoreAlignmentError("registered final-ascent landmark changed")
        fact = facts[0]
        event = ScoreLandmark(fact["id"], _fraction(fact["score_quarter"]), fact["measure"],
                              _fraction(fact["beat"]), fact["meaning"], (100,))
        evidence = manifest["ascent_evidence"]
        pitches = [62, 63, 65, 67, 68, 70, 72, 73, 75, 77, 79, 80, 82,
                   84, 85, 87, 89, 91, 92, 94, 95, 96, 97, 99, 100]
        if (type(evidence["staff"]) is not int or evidence["staff"] != 1 or
                any(type(p) is not int for p in evidence["midi_sequence"]) or
                evidence["editions"] != ["039-1-TR.krn.notes.json", "039-1-BH.krn.notes.json"] or
                _fraction(evidence["first_quarter"]) != 1848 or
                _fraction(evidence["step_quarters"]) != Fraction(1, 2) or
                evidence["midi_sequence"] != pitches or
                _fraction(evidence["terminal_duration_quarters"]) != 1):
            raise ScoreAlignmentError("registered final-ascent evidence changed")
        expected = [(Fraction(1848) + Fraction(i, 2), pitch) for i, pitch in enumerate(pitches)]
        primary = [(n.quarter, n.midi) for n in base.notes
                   if n.staff == 1 and n.attack and 1848 <= n.quarter <= 1860]
        corroborating = json.loads((_BUNDLE / "039-1-BH.krn.notes.json").read_bytes())
        secondary = [(Fraction(*r[:2]), r[4]) for r in corroborating["notes"]
                     if r[5] == 1 and r[9] and 1848 <= Fraction(*r[:2]) <= 1860]
        if primary != expected or secondary != expected:
            raise ScoreAlignmentError("final-ascent evidence disagrees with validated score editions")
        terminal = [n for n in base.notes if n.staff == 1 and n.quarter == 1860]
        if len(terminal) != 1 or terminal[0].midi != 100 or terminal[0].duration != 1:
            raise ScoreAlignmentError("terminal E7 score note is missing or changed")
        license_provenance = _thaw(base.provenance["license"])
        license_provenance.update(attribution_path="assets/score/chopin-op39-v1/ATTRIBUTION.md",
                                  license_path="assets/score/chopin-op39-v1/LICENSE.txt")
        provenance = {"reference_id": manifest["reference_id"],
                      "bundle_path": "assets/score/chopin-op39-clearing-v1",
                      "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                      "source": _thaw(base.provenance["source"]),
                      "license": license_provenance,
                      "base_reference": _thaw(base.provenance),
                      "extension": {"base_profile_id": PROFILE_ID,
                                    "appended_landmarks": facts, "ascent_evidence": evidence,
                                    "limits": manifest["limits"]}, "files": manifest["files"]}
        return ScoreReference(CLEARING_PROFILE_ID, base.quarter_length, base.notes,
                              base.landmarks + (event,), _freeze(provenance))
    except ScoreAlignmentError:
        raise
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ScoreAlignmentError("invalid or unavailable clearing score extension") from exc


def _score_chroma(reference: ScoreReference) -> np.ndarray:
    size = math.ceil(reference.quarter_length * _PPQ)
    if not size or size > 20000:
        raise ScoreAlignmentError("score length out of supported bounds")
    chroma = np.zeros((12, size), dtype=np.float64)
    for note in reference.notes:
        if note.duration <= 0:
            continue
        start, end = note.quarter, note.quarter + note.duration
        for j in range(max(0, math.floor(start * _PPQ)), min(size, math.ceil(end * _PPQ))):
            overlap = max(Fraction(), min(Fraction(j + 1, _PPQ), end) - max(Fraction(j, _PPQ), start))
            chroma[note.midi % 12, j] += float(overlap * _PPQ)
    chroma = np.sqrt(chroma)
    return chroma / np.maximum(np.linalg.norm(chroma, axis=0, keepdims=True), 1e-12)


def _windows(sr: int) -> tuple[int, int, int]:
    # Match the experiment's 8192/2400/240 at 48k; scale native-time windows
    # explicitly. All centers stay integer samples in the original signal.
    n_fft = 2 ** int(math.floor(math.log2(sr * 8192 / 48000) + .5))
    return max(1024, n_fft), max(1, (sr + 10) // 20), max(1, (sr + 100) // 200)


def _new_pitches(reference: ScoreReference, landmark: ScoreLandmark) -> tuple[list[int], list[int]]:
    q = landmark.quarter
    attacks = {n.midi for n in reference.notes if n.quarter == q and n.duration > 0 and n.attack}
    # Include notes that end exactly at q: their pedal/release tails can still
    # masquerade as new-pitch harmonics. Tied continuation is never an attack.
    previous = {n.midi for n in reference.notes if n.duration > 0 and n.quarter < q <= n.quarter + n.duration}
    new = sorted(attacks - previous)
    if landmark.refinement_midi:
        if not set(landmark.refinement_midi) <= set(new):
            raise ScoreAlignmentError(f"missing registered refinement pitch at {landmark.id}")
        new = sorted(set(landmark.refinement_midi))
    if not new:
        raise ScoreAlignmentError(f"no new symbolic pitch at {landmark.id}")
    return new, sorted(previous)


def _band_energy(power: np.ndarray, frequencies: np.ndarray, hz: float, resolution: float) -> np.ndarray:
    width = max(resolution * .7, hz * .004)
    weights = np.exp(-.5 * ((frequencies - hz) / width) ** 2)
    weights /= max(weights.sum(), 1e-12)
    # A side-band subtraction rejects broadband transients and weak spectral
    # leakage. Sidebands are separated by three FFT bins, not an octave.
    side = (np.exp(-.5 * ((frequencies - hz - 3 * resolution) / width) ** 2) +
            np.exp(-.5 * ((frequencies - hz + 3 * resolution) / width) ** 2))
    side /= max(side.sum(), 1e-12)
    return np.maximum(0, (weights - side) @ power)


def _refine(y: np.ndarray, sr: int, reference: ScoreReference, landmark: ScoreLandmark,
            low: int, high: int, local_cost: float, n_fft: int, hop: int) -> dict:
    targets, prior = _new_pitches(reference, landmark)
    margin = sr
    begin = max(0, ((low - margin) // hop) * hop)
    end = min(len(y), high + margin)
    if end - begin < n_fft:
        raise ScoreAlignmentError(f"insufficient local signal at {landmark.id}")
    power = np.abs(librosa.stft(y[begin:end], n_fft=n_fft, hop_length=hop,
                               center=True, pad_mode="constant")) ** 1.5
    frequencies = librosa.fft_frequencies(sr=sr, n_fft=n_fft)
    resolution = sr / n_fft
    envelopes = []
    fundamental = None
    lowest = targets[0]
    excluded = []
    for midi in targets:
        hz = float(librosa.midi_to_hz(midi))
        direct = _band_energy(power, frequencies, hz, resolution)
        envelope = direct.copy()
        safe_low_partial = np.zeros_like(direct)
        for harmonic in range(2, 7):
            f = harmonic * hz
            if f >= min(5000, sr / 2 - 3 * resolution):
                break
            # Suppress collisions with the previous score pitches, especially
            # the continuing RH G#3 at the fast-coda LH C#2 entry (partial 3).
            collision = any(abs(f - float(librosa.midi_to_hz(p)) * h) < max(1.5 * resolution, .006 * f)
                            for p in prior for h in range(1, 7))
            if collision:
                excluded.append([midi, harmonic])
                continue
            part = _band_energy(power, frequencies, f, resolution) / harmonic
            envelope += part
            if harmonic == 2:
                safe_low_partial = part
        if midi == lowest:
            fundamental = gaussian_filter1d(direct, 1.5, mode="nearest")
            # Even for a weak bass fundamental, a second partial alone is not
            # evidence of the bass note. The direct fundamental is the gate.
            low_support = direct + safe_low_partial if hz < 90 else direct
            support = gaussian_filter1d(low_support, 1.5, mode="nearest")
        envelopes.append(envelope)
    envelope = gaussian_filter1d(np.sum(envelopes, axis=0), 1.5, mode="nearest")
    samples = begin + np.arange(len(envelope), dtype=np.int64) * hop
    padding = int(math.floor(_REFINE_PADDING_SECONDS * sr + .5))
    ids = np.flatnonzero((samples >= low - padding) & (samples <= high + padding) &
                         (samples >= n_fft // 2) & (samples < len(y) - n_fft // 2))
    if len(ids) < 3 or float(np.max(envelope[ids])) <= 1e-15:
        raise ScoreAlignmentError(f"no pitch energy at {landmark.id}")
    scale = float(np.max(envelope[ids]))
    slope = np.maximum(0, np.gradient(envelope))
    direct_slope = np.maximum(0, np.gradient(fundamental))
    support_slope = np.maximum(0, np.gradient(support))
    fundamental_ratio = float(np.max(fundamental[ids]) / scale)
    if fundamental_ratio < 0.002:
        raise ScoreAlignmentError(f"missing lowest new-pitch fundamental at {landmark.id}")
    max_direct_slope = float(np.max(direct_slope[ids]))
    if max_direct_slope < float(np.max(fundamental[ids])) * .003:
        raise ScoreAlignmentError(f"missing new-pitch growth at {landmark.id}")
    # This mandatory direct-energy gate prevents an RH fundamental at 880 Hz
    # from being interpreted as a new LH 220 Hz note through a harmonic comb.
    gate = direct_slope / max_direct_slope
    support_gate = support_slope / max(float(np.max(support_slope[ids])), 1e-15)
    score = slope / max(float(np.max(slope[ids])), 1e-15) * np.sqrt(gate * support_gate)
    # A target-only ratio can make release leakage look convincing when the
    # entire target comb is nearly absent. Require the direct fundamental to
    # have support against the *actual* strongest local spectral bin. This
    # rejects broadening from a continuing G#3 release pretending to be C#2.
    spectral_peak = gaussian_filter1d(np.max(power, axis=0), 1.5, mode="nearest")
    # Fix the denominator over the search region, so vanishing release tails
    # cannot gain an artificially high ratio merely by approaching silence.
    spectral_ratio = fundamental / max(float(np.max(spectral_peak[ids])), 1e-15)
    ids = ids[spectral_ratio[ids] >= _MIN_FUNDAMENTAL_SPECTRAL_RATIO]
    if not len(ids):
        raise ScoreAlignmentError(f"insufficient direct fundamental spectral support at {landmark.id}")
    # Quantized comparison and first-index tie breaking are explicit.
    score = np.round(score, 10)
    peak = int(ids[np.argmax(score[ids])])
    if score[peak] <= .02:
        raise ScoreAlignmentError(f"ambiguous pitch attack at {landmark.id}")
    sample = int(samples[peak])
    nearby = ids[score[ids] >= .9 * score[peak]]
    half_window = n_fft // 2
    uncertainty = [max(0, int(samples[nearby[0]]) - half_window),
                   min(len(y) - 1, int(samples[nearby[-1]]) + half_window)]
    return {"id": landmark.id, "score_quarter": _pair(landmark.quarter),
            "measure": landmark.measure, "beat": _pair(landmark.beat),
            "meaning": landmark.meaning, "source_sample": sample,
            "method": "subsequence_chroma_dtw_then_new_pitch_fundamental_growth",
            "coarse_interval_samples": [int(low), int(high)],
            "analysis_hop_samples": hop, "analysis_window_samples": n_fft,
            "target_new_midi": targets, "prior_midi": prior,
            "excluded_prior_harmonic_collisions": excluded,
            "quality": {"local_mean_cosine_cost": round(local_cost, 6),
                        "lowest_fundamental_energy_ratio": round(fundamental_ratio, 6),
                        "lowest_fundamental_spectral_peak_ratio": round(float(spectral_ratio[peak]), 8),
                        "attack_score": float(score[peak]),
                        "interpretation": "bounded signal diagnostics; not a calibrated confidence probability"},
            "uncertainty": {"support_interval_samples": uncertainty,
                            "quantization_samples": hop,
                            "interpretation": "90%-peak candidates plus half STFT window; not an accuracy guarantee"}}


def _ascent_notes(reference: ScoreReference, event: ScoreLandmark) -> tuple[ScoreNote, ...]:
    notes = tuple(sorted((n for n in reference.notes if n.staff == 1 and n.attack and
                          n.duration > 0 and n.quarter <= event.quarter), key=lambda n: n.quarter)[-25:])
    if (len(notes) != 25 or notes[-1].quarter != event.quarter or
            event.refinement_midi != (100,) or notes[-1].midi != 100 or
            any(b.quarter-a.quarter != Fraction(1, 2) or b.midi <= a.midi
                for a, b in zip(notes, notes[1:]))):
        raise ScoreAlignmentError("missing complete registered RH ascent")
    return notes


def _ordered_ascent_paths(candidates, samples, typical, hop, *, allow_occlusion=False, diagnostic=None):
    """Keep sufficient onset history for the terminal's local-rhythm gate.

    Two routes per last candidate are insufficient: a cheaper prefix may have
    different recent timing and fail a later gate. Histories with identical
    last seven indexed onsets are future-equivalent. A computation bound
    rejects rather than silently discarding otherwise eligible histories.
    """
    paths = [(0., [])]
    for index, found in enumerate(candidates):
        grouped = {}
        if allow_occlusion and 0 < index < len(candidates)-1:
            for cost,previous in paths:
                missing = index-len(previous)
                seen = {j for j,_ in previous}
                missing_suffix = sum(j not in seen for j in range(18,index))
                if (missing < 2 and (index == 0 or (previous and previous[-1][0] == index-1))
                        and (index < 18 or missing_suffix < 1)):
                    # One missing observation costs more than the weakest
                    # retained .02-relative-growth observation (-log(.02)).
                    key = (tuple(previous[-7:]),missing+1,missing_suffix+(index >= 18))
                    grouped.setdefault(key,[]).append((cost+4.,previous))
        for i, strength in found:
            for cost, previous in paths:
                if previous:
                    old_index, old_i = previous[-1]
                    distance = index-old_index
                    gap = (int(samples[i])-int(samples[old_i]))/distance
                    if not max(hop, .20*typical) <= gap <= 4*typical:
                        continue
                    if index == len(candidates)-1:
                        history = previous[-7:]
                        recent = [(int(samples[b])-int(samples[a]))/(ib-ia)
                                  for (ia,a),(ib,b) in zip(history,history[1:])]
                        local = float(np.median(recent))
                        if not .4*local <= gap <= 2*local:
                            continue
                route = previous+[(index,i)]
                seen = {j for j,_ in route}
                key = (tuple(route[-7:]),index+1-len(route),sum(j not in seen for j in range(18,index+1)))
                grouped.setdefault(key, []).append((cost-float(np.log(max(strength,1e-12))),route))
        if len(grouped) > 20000:
            raise ScoreAlignmentError("ascent history search exceeds supported bound")
        extended = [route for choices in grouped.values()
                    for route in sorted(choices,key=lambda x:(x[0],x[1]))[:2]]
        if not extended:
            if diagnostic is not None:
                diagnostic["first_failed_score_index"] = index
                best = min(paths,key=lambda x:(-len(x[1]),x[0],x[1]))
                diagnostic["maximum_supported_ordered_prefix"] = len(best[1])
                diagnostic["best_partial_path_indices"] = [list(v) for v in best[1]]
            paths = []
            break
        paths = extended
    return sorted(paths,key=lambda x:(x[0],x[1]))


def _ascent_refine(y, sr, reference, event, path, coarse_hop, local_cost, n_fft, hop):
    """Locate the local register sequence, then tune/refine its held-out end.

    Global chroma context and independent prefix frequency registration bound
    the hypothesis. A unique terminal pitch-growth event is required inside
    its original DTW interval plus350ms. No performed keystrike is certified.
    """
    notes = _ascent_notes(reference,event)
    intervals = []
    for note in notes:
        rows = path[path[:,1] == int(note.quarter*_PPQ),0]
        if not len(rows):
            raise ScoreAlignmentError("ascent is not covered by the score path")
        intervals.append((int(rows.min())*coarse_hop,int(rows.max())*coarse_hop))
    padding = int(math.floor(_REFINE_PADDING_SECONDS*sr+.5))
    begin = max(0,((intervals[0][0]-sr)//hop)*hop)
    end = min(len(y),intervals[-1][1]+sr)
    power = np.abs(librosa.stft(y[begin:end],n_fft=n_fft,hop_length=hop,
                               center=True,pad_mode="constant"))**1.5
    frequencies = librosa.fft_frequencies(sr=sr,n_fft=n_fft)
    resolution = sr/n_fft
    samples = begin+np.arange(power.shape[1],dtype=np.int64)*hop
    spectral_peak = gaussian_filter1d(np.max(power,axis=0),1.5,mode="nearest")
    valid = (samples >= n_fft//2) & (samples < len(y)-n_fft//2)
    prefix_ids = np.flatnonzero(valid & (samples >= intervals[0][0]-padding) &
                                (samples <= intervals[-1][1]+padding))
    terminal_ids = np.flatnonzero(valid & (samples >= intervals[-1][0]-padding) &
                                  (samples <= intervals[-1][1]+padding))
    diagnostic = {"algorithm":"score_context_prefix_stretch_unique_terminal_growth_v1",
        "score_quarters":[_pair(n.quarter) for n in notes],
        "midi_sequence":[n.midi for n in notes],
        "dtw_intervals_samples":[list(v) for v in intervals],
        "refinement_padding_samples":padding,
        "limits":{"maximum_absolute_tuning_cents":_ASCENT_MAX_CENTS,
            "maximum_tuning_cents_per_semitone":1.,
            "prefix_fit_residual_cents":_ASCENT_FIT_RESIDUAL_CENTS,
            "minimum_prefix_fit_notes":_ASCENT_MIN_FIT_NOTES,
            "minimum_prefix_register_span_semitones":24,
            "minimum_direct_spectral_peak_ratio":_MIN_FUNDAMENTAL_SPECTRAL_RATIO,
            "minimum_terminal_written_register_growth_share":.5,
            "competing_subharmonic_growth_context_half_stft_windows":1,
            "minimum_terminal_peak_to_local_background":4.,
            "maximum_terminal_register_cost":_MAX_ANCHOR_COST,
            "minimum_terminal_relative_growth":0.,
            "minimum_terminal_growth_fraction_of_region_maximum_fundamental":.003,
            "terminal_candidate_count_required":1},
        "score_meaning_scope":"symbolic intent from the registered score; acoustic detection does not independently certify written identity",
        "known_identity_limits":["a sole different E7 within the search window can qualify", "smooth amplitude modulation of already ringing E7 can qualify without a new keystrike"],
        "interpretation":"bounded pitch-growth evidence consistent with score context; not calibrated confidence, keystrike certification or hand provenance"}

    def reject(message):
        error = ScoreAlignmentError(message)
        error.diagnostics = diagnostic
        raise error

    if len(prefix_ids) < 3 or len(terminal_ids) < 3:
        reject("insufficient native ascent context")
    diagnostic["local_register_sequence_context_samples"] = [int(samples[prefix_ids[0]]),int(samples[prefix_ids[-1]])]
    diagnostic["prefix_frequency_policy"] = "ordered within-semitone local spectral peaks; independently registered prefix trend predicts held-out terminal tuning"

    def peaks(curve,ids,*,maximum_candidates=8,minimum_relative_growth=.02,minimum_growth_fraction=0.):
        smooth = gaussian_filter1d(curve,1.5,mode="nearest")
        growth = np.maximum(0.,np.gradient(smooth))
        floor = float(spectral_peak[ids].max())*_MIN_FUNDAMENTAL_SPECTRAL_RATIO
        selected = ids[(growth[ids] >= growth[np.maximum(0,ids-1)]) &
                       (growth[ids] > growth[np.minimum(len(growth)-1,ids+1)]) &
                       (smooth[ids] >= floor) &
                       (growth[ids] >= minimum_growth_fraction*float(smooth[ids].max()))]
        if not len(selected) or float(growth[selected].max()) <= 0:
            return [],smooth,growth
        ranked = sorted(selected,key=lambda i:(-growth[i],int(i)))
        chosen = []
        for i in ranked:
            if all(abs(int(samples[i])-int(samples[j])) >= n_fft//2 for j in chosen):
                chosen.append(int(i))
            if maximum_candidates is not None and len(chosen) == maximum_candidates:
                break
        normalizer = float(growth[selected].max())
        return [(i,float(growth[i]/normalizer)) for i in sorted(chosen)
                if growth[i] >= minimum_relative_growth*normalizer],smooth,growth

    def register_track(hz,*,local_peak):
        bins = np.flatnonzero((frequencies >= hz*2**(-_ASCENT_MAX_CENTS/1200)) &
                             (frequencies <= hz*2**(_ASCENT_MAX_CENTS/1200)))
        bins = bins[(bins > 0) & (bins < len(frequencies)-1)]
        if not len(bins):
            return np.zeros(power.shape[1]),np.zeros(power.shape[1],dtype=int)
        values = power[bins]
        if local_peak:
            mask = (values >= power[bins-1]) & (values > power[bins+1])
            values = np.where(mask,values,0.)
        track = bins[np.argmax(values,axis=0)]
        return np.max(values,axis=0),track

    def tonality(track):
        track = np.clip(track,8,len(frequencies)-9)
        at = np.arange(power.shape[1])
        background = np.mean(np.stack([power[track+offset,at]
            for offset in (-8,-7,-6,-5,-4,4,5,6,7,8)]),axis=0)
        return gaussian_filter1d(power[track,at],1.5,mode="nearest")/np.maximum(
            gaussian_filter1d(background,1.5,mode="nearest"),1e-30)

    def growth_share(direct_growth,lower_components,i):
        lower_growth = np.maximum.reduce([np.maximum(0.,np.gradient(v)) for v in lower_components])
        radius = max(1,n_fft//(2*hop))
        competing = float(lower_growth[max(0,i-radius):min(len(lower_growth),i+radius+1)].max())
        return float(direct_growth[i]/max(direct_growth[i]+competing,1e-30))

    def cents_at(track,i,hz):
        k = int(track[i])
        a,b,c = np.log(np.maximum(power[k-1:k+2,i],1e-30))
        curvature = a-2*b+c
        if curvature >= 0:
            return None
        offset = float(np.clip(.5*(a-c)/curvature,-.5,.5))
        return float(1200*np.log2((k+offset)*resolution/hz))

    # Prefix-only registration keeps the terminal independently held out.
    # Individual chroma cells supply frequency observations, not final onsets;
    # the separate register sequence supplies the latter's ordered context.
    observations = []
    for note,(low,high) in zip(notes[:-1],intervals[:-1]):
        ids = np.flatnonzero(valid & (samples >= low-padding) & (samples <= high+padding))
        if len(ids) < 3:
            continue
        hz = float(librosa.midi_to_hz(note.midi))
        amplitude,track = register_track(hz,local_peak=False)
        found,_,_ = peaks(amplitude,ids)
        if not found:
            continue
        i = max(found,key=lambda v:(v[1],-v[0]))[0]
        cents = cents_at(track,i,hz)
        if cents is not None and abs(cents) <= _ASCENT_MAX_CENTS:
            observations.append((note.midi,cents))
    diagnostic["prefix_tuning_observations"] = [[m,round(c,6)] for m,c in observations]
    if len(observations) < _ASCENT_MIN_FIT_NOTES:
        reject("insufficient preceding RH notes for ascent registration")
    xx,yy = np.asarray(observations,dtype=np.float64).T
    center = float(np.median(xx))
    slopes = [(yy[j]-yy[i])/(xx[j]-xx[i]) for i in range(len(xx)) for j in range(i+1,len(xx)) if xx[j] != xx[i]]
    slope = float(np.median(slopes));intercept = float(np.median(yy-slope*(xx-center)))
    inliers = np.abs(yy-(intercept+slope*(xx-center))) <= _ASCENT_FIT_RESIDUAL_CENTS
    tuning = np.asarray([intercept+slope*(n.midi-center) for n in notes])
    diagnostic["tuning"] = {"center_midi":center,"cents_at_center":round(intercept,6),
        "cents_per_semitone":round(slope,6),"inlier_prefix_notes":int(inliers.sum()),
        "terminal_cents":round(float(tuning[-1]),6)}
    if (int(inliers.sum()) < _ASCENT_MIN_FIT_NOTES or np.ptp(xx[inliers]) < 24 or
            abs(slope) > 1. or np.max(np.abs(tuning)) > _ASCENT_MAX_CENTS):
        reject("incoherent local register tuning across the ascent")

    candidates,details,direct_curves,lower_curves = [],[],[],[]
    diagnostic["note_support"],diagnostic["register_observations"] = [],[]
    for index,note in enumerate(notes):
        hz = float(librosa.midi_to_hz(note.midi))
        ids = terminal_ids if index == 24 else prefix_ids
        if index == 24:
            hz *= 2**(float(tuning[-1])/1200)
            energy = _band_energy(power,frequencies,hz,resolution)
            bins = np.flatnonzero((frequencies >= hz*2**(-10/1200)) & (frequencies <= hz*2**(10/1200)))
            if not len(bins):
                reject("unresolved terminal frequency window")
            track = bins[np.argmax(power[bins],axis=0)]
        else:
            energy,track = register_track(hz,local_peak=True)
        found,direct,growth = peaks(energy,ids,maximum_candidates=None if index == 24 else 8,
                                    minimum_relative_growth=0. if index == 24 else .02,
                                    minimum_growth_fraction=.003 if index == 24 else 0.)
        lower_components = [gaussian_filter1d(
            _band_energy(power,frequencies,hz/h,resolution) if index == 24 else register_track(hz/h,local_peak=False)[0],
            1.5,mode="nearest") for h in (2,3,4)]
        lower = np.maximum.reduce(lower_components)
        tonal = tonality(track) if index == 24 else None
        kept,record = [],{}
        for i,strength in found:
            cents = cents_at(track,i,hz)
            share = growth_share(growth,lower_components,i)
            energy_share = float(direct[i]/max(direct[i]+lower[i],1e-30))
            supported = index < 24 or (share >= .5 and tonal[i] >= 4.)
            record[i] = {"source_sample":int(samples[i]),"energy_share":round(energy_share,8),
                "new_growth_share":round(share,8),"observed_cents":None if cents is None else round(cents,6),
                "direct_spectral_ratio":round(float(direct[i]/max(float(spectral_peak[ids].max()),1e-15)),8),
                "terminal_tonality":None if tonal is None else round(float(tonal[i]),6),
                "supported":bool(supported)}
            if supported:
                kept.append((i,strength))
        candidates.append(kept); details.append(record)
        direct_curves.append(direct);lower_curves.append(lower)
        diagnostic["register_observations"].append(list(record.values()))
        onset = int(ids[np.argmax(growth[ids])])
        denominator = max(float(spectral_peak[ids].max()),1e-15)
        diagnostic["note_support"].append({"midi":note.midi,"nominal_hz":round(hz,6),
            "maximum_direct_spectral_ratio":round(float(direct[ids].max()/denominator),8),
            "maximum_growth_sample":int(samples[onset]),
            "direct_ratio_at_maximum_growth":round(float(direct[onset]/denominator),8)})
    diagnostic["candidate_counts"] = [len(v) for v in candidates]
    diagnostic["candidate_onset_samples"] = [[int(samples[i]) for i,_ in v] for v in candidates]
    diagnostic["candidate_relative_growth"] = [[round(s,6) for _,s in v] for v in candidates]
    diagnostic["missing_candidate_score_indices"] = [j for j,v in enumerate(candidates) if not v]
    # Ordered prefix trajectories are diagnostic context, not a transcription
    # requirement. Pedal components and weak strikes can leave this incomplete
    # even when one independently registered terminal growth event qualifies.
    trajectory = {"scope":"diagnostic_only; never supplies terminal timing or certifies performed keystrikes",
                  "limits":{"maximum_omitted_observations":2,"maximum_history_states":20000,
                            "relative_inter_onset_range":[.2,4.],"terminal_relative_recent_interval_range":[.4,2.]}}
    diagnostic["ordered_trajectory"] = trajectory
    centers = np.asarray([(a+b)/2 for a,b in intervals])
    positive = np.diff(centers);positive = positive[positive > 0]
    if len(positive):
        try:
            paths = _ordered_ascent_paths(candidates,samples,float(np.median(positive)),hop,
                                          allow_occlusion=True,diagnostic=trajectory)
            if paths:
                cost,route = paths[0]
                trajectory["path_cost"] = round(cost,8)
                trajectory["observed_score_indices"] = [j for j,_ in route]
                trajectory["onset_samples"] = [int(samples[i]) for _,i in route]
                trajectory["omitted_score_indices"] = [j for j in range(25) if j not in {k for k,_ in route}]
        except ScoreAlignmentError as error:
            trajectory["unsupported_reason"] = str(error)
    # All candidates in the terminal window are considered before uniqueness
    # is decided. Prefix diagnostic top-eight pruning cannot manufacture a
    # unique terminal. Nearby growth peaks inside half an STFT window were
    # clustered at STFT scale by peaks(); this is not keystrike certification.
    if not candidates[-1]:
        reject("missing terminal direct fundamental growth in the score context")
    if len(candidates[-1]) != 1:
        reject("ambiguous competing terminal pitch growth events")
    peak,strength = candidates[-1][0]
    terminal_record = details[-1][peak]
    share = terminal_record["new_growth_share"]
    energy_share = terminal_record["energy_share"]
    diagnostic["terminal_written_register_growth_share"] = share
    diagnostic["terminal_written_register_share"] = energy_share
    diagnostic["interpretation"] += "; lower-register energy and ordered prefix trajectory are diagnostic only"
    # The last iteration retains the tuned terminal curves and denominator.
    ratio = float(direct[peak]/max(float(spectral_peak[terminal_ids].max()),1e-15))
    sample = int(samples[peak]);targets,prior = _new_pitches(reference,event)
    return {"id":event.id,"score_quarter":_pair(event.quarter),"measure":event.measure,
        "beat":_pair(event.beat),"meaning":event.meaning,"source_sample":sample,
        "method":"score_context_prefix_tuning_then_unique_terminal_pitch_growth",
        "coarse_interval_samples":list(intervals[-1]),"analysis_hop_samples":hop,
        "analysis_window_samples":n_fft,"target_new_midi":targets,"prior_midi":prior,
        "excluded_prior_harmonic_collisions":[],"register_sequence":diagnostic,
        "quality":{"local_mean_cosine_cost":round(local_cost,6),
            "lowest_fundamental_spectral_peak_ratio":round(ratio,8),
            "terminal_written_register_share":round(energy_share,8),
            "terminal_written_register_growth_share":round(share,8),
            "terminal_peak_to_local_background":terminal_record["terminal_tonality"],"growth_score":strength,
            "interpretation":"local pitch-growth evidence consistent with the score target; not certified physical keystrike, written identity, hand provenance or calibrated confidence"},
        "uncertainty":{"support_interval_samples":[max(0,sample-n_fft//2),min(len(y)-1,sample+n_fft//2)],
            "quantization_samples":hop,"interpretation":"centered STFT support; not an accuracy guarantee"}}


def analyze(y_mono, sr: int, *, profile_id: str = PROFILE_ID,
            reference: ScoreReference | None = None) -> dict:
    """Return bounded JSON evidence, or fail closed with ScoreAlignmentError.

    ``reference`` enables reproducible synthetic tests using the same algorithm.
    Production callers use the default validated bundled profile. All reported
    samples are native source positions, with sample zero at the signal start.
    """
    if type(sr) is not int or not 8000 <= sr <= 192000:
        raise ScoreAlignmentError("unsupported native sample rate")
    y = np.asarray(y_mono)
    if y.ndim != 1 or y.dtype.kind not in "fi" or not np.isfinite(y).all():
        raise ScoreAlignmentError("expected finite mono source samples")
    if not 2 * sr <= len(y) <= 900 * sr:
        raise ScoreAlignmentError("source duration outside 2..900 seconds")
    # Read-only conversion, never normalize or mutate the provided array.
    y = y.astype(np.float64, copy=False)
    if float(np.std(y)) < 1e-8:
        raise ScoreAlignmentError("silent or constant source")
    ref = reference if reference is not None else load_reference(profile_id)
    if ref.profile_id != profile_id or not ref.landmarks:
        raise ScoreAlignmentError("reference profile mismatch or missing landmarks")
    n_fft, coarse_hop, fine_hop = _windows(sr)
    spectrum = np.abs(librosa.stft(y, n_fft=n_fft, hop_length=coarse_hop,
                                  center=True, pad_mode="constant")) ** 2
    audio = librosa.feature.chroma_stft(S=spectrum, sr=sr, n_fft=n_fft,
                                        hop_length=coarse_hop, tuning=0.0, norm=2)
    del spectrum
    audio = np.round(audio, 6)
    if float(np.mean(np.std(audio, axis=1))) < .012:
        raise ScoreAlignmentError("insufficient changing pitch content")
    symbolic = _score_chroma(ref)
    # Keep the bounded profile practical on local machines. This bound rejects
    # unsupported long input instead of silently changing time resolution.
    if audio.shape[1] * symbolic.shape[1] > 60000000:
        raise ScoreAlignmentError("alignment matrix exceeds supported bound")
    audio /= np.maximum(np.linalg.norm(audio, axis=0, keepdims=True), 1e-12)
    cost = np.clip(1 - audio.T @ symbolic, 0, 1)
    cost = np.round(cost / _COST_QUANTUM) * _COST_QUANTUM
    _, _, steps = librosa.sequence.dtw(C=cost, subseq=True, return_steps=True)
    path = librosa.sequence.dtw_backtracking(steps, subseq=True,
                                           start=symbolic.shape[1] - 1)[::-1]
    path_cost = cost[path[:, 0], path[:, 1]]
    mean_cost = float(np.mean(path_cost))
    if mean_cost > _MAX_MEAN_COST:
        raise ScoreAlignmentError(f"score mismatch: mean cosine cost {mean_cost:.6f}")
    earliest, latest = min(m.quarter for m in ref.landmarks), max(m.quarter for m in ref.landmarks)
    covered_start = Fraction(int(path[0, 1]), _PPQ)
    covered_end = Fraction(int(path[-1, 1]) + 1, _PPQ)
    # Require context on both sides of every event. Synthetic references use a
    # proportional context; the full score uses at least twelve quarters.
    context = min(Fraction(12), ref.quarter_length / 20)
    if covered_start > earliest - context or covered_end < latest + context:
        raise ScoreAlignmentError("insufficient score coverage around landmarks")
    landmarks = []
    for event in ref.landmarks:
        score_index = event.quarter * _PPQ
        if score_index.denominator != 1:
            raise ScoreAlignmentError("landmark is outside half-quarter reference grid")
        rows = np.flatnonzero(path[:, 1] == int(score_index))
        if not len(rows):
            raise ScoreAlignmentError(f"missing aligned event: {event.id}")
        samples = path[rows, 0] * coarse_hop
        low, high = int(samples.min()), int(samples.max())
        local_rows = np.flatnonzero(np.abs(path[:, 1] - int(score_index)) <= 2 * _PPQ)
        local_cost = float(np.mean(path_cost[local_rows]))
        if high - low > int(_MAX_INTERVAL_SECONDS * sr) or local_cost > _MAX_LOCAL_COST:
            raise ScoreAlignmentError(f"ambiguous coarse alignment: {event.id}")
        if event.id == "final_scale_arrival" and event.refinement_midi == (100,):
            refined = _ascent_refine(y, sr, ref, event, path, coarse_hop, local_cost, n_fft, fine_hop)
        else:
            refined = _refine(y, sr, ref, event, low, high, local_cost, n_fft, fine_hop)
        # Averaging a +/-2-quarter context can conceal a missing event. Require
        # the refined window itself to match the event's score pitch classes;
        # a following chord sharing just the same bass is insufficient.
        audio_index = min(audio.shape[1] - 1,
                          (refined["source_sample"] + coarse_hop // 2) // coarse_hop)
        anchor_cost = float(cost[audio_index, int(score_index)])
        if event.id == "final_scale_arrival" and event.refinement_midi == (100,):
            # Octave-invariant bass-dominated chroma is not a register test.
            # The terminal requires its own independently tuned new-growth
            # evidence. Ordered prefix trajectories are diagnostic only.
            register_cost = 1-refined["quality"]["terminal_written_register_growth_share"]
            if register_cost > _MAX_ANCHOR_COST:
                raise ScoreAlignmentError("refined terminal register mismatch")
            refined["quality"]["refined_register_cost"] = round(register_cost, 6)
            refined["quality"]["refined_cosine_cost_diagnostic"] = round(anchor_cost, 6)
        else:
            if anchor_cost > _MAX_ANCHOR_COST:
                raise ScoreAlignmentError(f"refined pitch-class mismatch: {event.id}")
            refined["quality"]["refined_cosine_cost"] = round(anchor_cost, 6)
        landmarks.append(refined)
    positions = [x["source_sample"] for x in landmarks]
    if positions != sorted(set(positions)):
        raise ScoreAlignmentError("landmark source order is not strictly increasing")
    result = {"profile_id": profile_id, "sample_rate": sr, "source_sample_count": len(y),
            "reference": _thaw(ref.provenance), "landmarks": landmarks,
            "analysis": {"algorithm_version": 1, "feature": "12-bin chroma STFT power",
                         "chroma_tuning_semitones": 0.0, "chroma_precision_decimals": 6,
                         "reference_steps_per_quarter": _PPQ,
                         "coarse_hop_samples": coarse_hop, "stft_window_samples": n_fft,
                         "stft_center": True, "stft_padding": "constant",
                         "time_origin": "native source sample zero; centered window centers",
                         "cost_quantum": _COST_QUANTUM,
                         "dtw": "cosine, subsequence free reference beginning, forced reference ending; steps (1,1),(0,1),(1,0)",
                         "score_coverage_quarters": [_pair(covered_start), _pair(covered_end)],
                         "mean_cosine_cost": round(mean_cost, 6),
                         "acceptance_limits": {"maximum_mean_cosine_cost": _MAX_MEAN_COST,
                                               "maximum_landmark_local_cost": _MAX_LOCAL_COST,
                                               "maximum_refined_cosine_cost": _MAX_ANCHOR_COST,
                                               "minimum_direct_fundamental_spectral_peak_ratio": _MIN_FUNDAMENTAL_SPECTRAL_RATIO,
                                               "maximum_coarse_interval_seconds": _MAX_INTERVAL_SECONDS},
                         "librosa_version": librosa.__version__, "numpy_version": np.__version__,
                         "limits": ["written-order reference with forced ending; unsupported repeats/cuts can fail",
                                    "half-quarter chroma is octave-invariant; local fundamental growth supplies register evidence",
                                    "pedal, weak fundamentals and simultaneous attacks can still produce ambiguity",
                                    "uncertainty is STFT support, not a calibrated error bound; independent music review required"]}}
    if any(e["method"] == "score_context_prefix_tuning_then_unique_terminal_pitch_growth"
           for e in landmarks):
        result["analysis"]["terminal_register_alignment"] = {
            "algorithm": "score_context_prefix_stretch_unique_terminal_growth_v1",
            "scope": "new final_scale_arrival only; preceding three refinements unchanged",
            "registration": "robust linear cents versus written MIDI from held-out prefix; ordered trajectory is diagnostic only, not keystrike certification",
            "limits": landmarks[-1]["register_sequence"]["limits"],
            "anchor_policy": "terminal written-register new-growth cost<=0.5; continuing lower-register energy and terminal chroma are diagnostic only; original three cues keep their chroma gate",
            "legacy_chroma_anchor_scope": ["major_arrival", "both_hands_octaves", "fast_coda"],
            "caveat": "detects sole local tuned E7 growth consistent with score context; a different sole E7 inside the window or smoothly modulated ringing E7 can qualify; no physical keystrike, written-identity or hand guarantee; independent music review required"}
    return result

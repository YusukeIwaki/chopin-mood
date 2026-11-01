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
_BUNDLE = Path(__file__).resolve().parents[1] / "assets/score/chopin-op39-v1"
_PPQ = 2
_COST_QUANTUM = 0.000001
_MAX_MEAN_COST = 0.42
_MAX_LOCAL_COST = 0.60
_MAX_ANCHOR_COST = 0.50
_MIN_FUNDAMENTAL_SPECTRAL_RATIO = 0.001
_MAX_INTERVAL_SECONDS = 2.0
_REFINE_PADDING_SECONDS = 0.35


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
    id: str
    quarter: Fraction
    measure: int
    beat: Fraction
    meaning: str = ""


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
        refined = _refine(y, sr, ref, event, low, high, local_cost, n_fft, fine_hop)
        # Averaging a +/-2-quarter context can conceal a missing event. Require
        # the refined window itself to match the event's score pitch classes;
        # a following chord sharing just the same bass is insufficient.
        audio_index = min(audio.shape[1] - 1,
                          (refined["source_sample"] + coarse_hop // 2) // coarse_hop)
        anchor_cost = float(cost[audio_index, int(score_index)])
        if anchor_cost > _MAX_ANCHOR_COST:
            raise ScoreAlignmentError(f"refined pitch-class mismatch: {event.id}")
        refined["quality"]["refined_cosine_cost"] = round(anchor_cost, 6)
        landmarks.append(refined)
    positions = [x["source_sample"] for x in landmarks]
    if positions != sorted(set(positions)):
        raise ScoreAlignmentError("landmark source order is not strictly increasing")
    return {"profile_id": profile_id, "sample_rate": sr, "source_sample_count": len(y),
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

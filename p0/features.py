"""Continuous acoustic features, keyed by integer SOURCE sample positions.

Analysis runs at the file's native sample rate (no resampling, so the source
sample grid is the feature grid). Display seconds are derived, never stored
as the reference.

Window / padding conventions (all explicit; verified against the installed
librosa 1.0.0 whose stft default is pad_mode='constant', NOT 'reflect'):
  RMS: TRUE time-domain RMS, rectangular window, frame_length=n_fft,
    hop_length=hop, center=True with constant (zero) padding of n_fft//2
    samples on each side. Computed directly from the waveform with an
    explicit cumulative-sum implementation, so `rms` is a real amplitude
    (same unit as the waveform) and scales linearly with gain. ABS_FLOOR
    (1e-4, i.e. -80 dBFS) is therefore a statement about real amplitude.
  Spectral frames (flux / centroid / rolloff / band ratios / chroma input):
    magnitude of librosa.stft with n_fft/hop, Hann window, center=True,
    constant padding n_fft//2 each side. Frame i is centered at source
    sample i*hop. flux[i] uses magnitude frames i-1 and i, so its nominal
    center is i*hop with a systematic skew of up to -hop/2; the
    synthetic-impulse test measures the real offset
    (see tests/test_features_synth.py) instead of assuming zero.
  Chroma: librosa.feature.chroma_stft expects a POWER spectrogram, so we
    pass S_power = |STFT|**2 (not magnitude). This choice is deliberate and
    recorded in meta.

Harmonic novelty and silence:
  harm_novelty compares chroma vectors `lag` frames apart (chroma_lag_sec).
  Frame i compares the interval [start(i), end(i)] where
  start(i) = (i-lag)*hop and end(i) = i*hop (integer source samples).
  Its representative time is the integer midpoint
  mid(i) = (start(i)+end(i))//2, i.e. t-lag/2, NOT the right edge.
  This is a definition of the detection quantity's time coordinate (not a
  fixed "minus 1 s" correction); smoothing/polyphony leave the true
  structural boundary uncertain. Volume edges (slow_env) and attacks
  (flux) keep the right-edge convention i*hop and are NOT shifted.
  Zero-norm (silent) chroma vectors on either side yield novelty
  0 via an explicit energy mask (both_live); silence never produces a
  novelty peak of 1.

Anti-false-event guards:
  Quantile normalization is SKIPPED for flat inputs (near-silence or
  stationary tones): if p95 < ABS_FLOOR or (p95 - p10) < RANGE_FLOOR, the
  normalized curve is all zeros and `is_flat_<name>` is True, so no events
  are emitted from silence / steady signals. Chroma change is reported as a
  harmonic-novelty CANDIDATE, never as a harmony/cadence fact.

Register-vs-timbre note (documented limitation):
  centroid/rolloff/band-energy move both when the played register moves AND
  when timbre changes at fixed pitch. They are stored as brightness proxies
  with this caveat; chroma is the only (weak) register-sensitive cue and it
  is unreliable under dense polyphony / heavy pedal. No disambiguation is
  attempted in P0; the design review doc records this as an open hypothesis.
"""
from __future__ import annotations

import numpy as np
import librosa
from scipy import ndimage as ndi

#: Reference rate for duration-based default sizing. At this rate the
#: defaults are exactly hop=512 / n_fft=2048 (bit-for-bit legacy behaviour).
REFERENCE_SR = 22050
DEFAULT_HOP_AT_REFERENCE = 512
DEFAULT_N_FFT_AT_REFERENCE = 2048
#: Human-readable sizing policy recorded in meta for reproducibility.
DEFAULT_SIZING_POLICY = (
    "duration_based_anchored_at_reference_sr: "
    "hop=max(1,round(512*sr/22050)), "
    "n_fft=max(2,2*round(1024*sr/22050)); "
    "native sr, no resampling"
)

ABS_FLOOR = 1e-4  # true-amplitude silence gate (-80 dBFS)
RANGE_FLOOR = 1e-6
#: Below this raw time-domain RMS peak the signal counts as TRUE silence
#: (used by struct-boundary gating; distinct from "flat envelope").
SILENCE_RMS = 1e-4


def robust_norm(x: np.ndarray) -> tuple[np.ndarray, bool, dict]:
    """Quantile normalize to [0,1]; return zeros + flat=True for flat input."""
    x = np.asarray(x, dtype=np.float64)
    p10, p95 = float(np.percentile(x, 10)), float(np.percentile(x, 95))
    if p95 < ABS_FLOOR or (p95 - p10) < RANGE_FLOOR:
        return np.zeros_like(x), True, {"p10": p10, "p95": p95}
    return np.clip((x - p10) / (p95 - p10), 0.0, 1.0), False, {"p10": p10, "p95": p95}


def time_domain_rms(y: np.ndarray, n_fft: int, hop: int, n_frames: int) -> np.ndarray:
    """True time-domain RMS: rectangular window, zero-padded edges.

    Frame i covers waveform samples [i*hop - n_fft//2, i*hop + n_fft//2)
    (zero outside the signal), i.e. center=True with constant padding.
    `n_frames` must equal the STFT frame count (asserted by the caller).
    """
    y = np.asarray(y, dtype=np.float64).ravel()
    pad = n_fft // 2
    yp = np.pad(y, pad, mode="constant")
    need = (n_frames - 1) * hop + n_fft
    assert len(yp) >= need, (len(yp), need, n_frames, hop, n_fft)
    sq = np.concatenate([[0.0], np.cumsum(yp * yp)])
    idx = np.arange(n_frames, dtype=np.int64) * np.int64(hop)
    return np.sqrt((sq[idx + n_fft] - sq[idx]) / float(n_fft))


def default_hop_n_fft(sr: int) -> tuple[int, int]:
    """Duration-based default (hop, n_fft) at native ``sr``.

    Anchored at REFERENCE_SR so hop/window durations (not sample counts)
    are preserved across rates: at 22050 Hz this is exactly (512, 2048).
    """
    if isinstance(sr, bool) or not isinstance(sr, (int, np.integer)) or sr <= 0:
        raise ValueError("sr must be a positive integer")
    sr_i = int(sr)
    hop = max(1, round(DEFAULT_HOP_AT_REFERENCE * sr_i / REFERENCE_SR))
    n_fft = max(2, 2 * round((DEFAULT_N_FFT_AT_REFERENCE // 2) * sr_i / REFERENCE_SR))
    return int(hop), int(n_fft)


def compute_features(
    y: np.ndarray,
    sr: int,
    hop: int | None = None,
    n_fft: int | None = None,
    slow_sec: float = 2.0,
    chroma_lag_sec: float = 2.0,
) -> dict:
    """Compute continuous features. Returns arrays + centers + meta.

    ``hop``/``n_fft`` are optional integer overrides. Without an override
    they are resolved at the native ``sr`` by :func:`default_hop_n_fft`
    (duration-based, anchored at 22050 Hz = 512/2048). No resampling is
    performed; the source sample grid remains the feature grid.
    """
    hop_is_override = hop is not None
    n_fft_is_override = n_fft is not None
    default_hop, default_n_fft = default_hop_n_fft(sr)
    if hop is None:
        hop = default_hop
    if n_fft is None:
        n_fft = default_n_fft
    if isinstance(hop, bool) or not isinstance(hop, (int, np.integer)) or hop < 1:
        raise ValueError("hop must be a positive integer")
    if (isinstance(n_fft, bool) or not isinstance(n_fft, (int, np.integer))
            or n_fft < 2 or n_fft % 2):
        raise ValueError("n_fft must be a positive even integer")
    hop, n_fft = int(hop), int(n_fft)
    y = np.asarray(y, dtype=np.float64).ravel()
    n_src = int(y.size)
    if n_src == 0:
        raise ValueError("empty signal")

    Sc = librosa.stft(y, n_fft=n_fft, hop_length=hop, center=True,
                       window="hann", pad_mode="constant")
    S = np.abs(Sc)
    n_frames = S.shape[1]
    assert n_frames == 1 + n_src // hop, (n_frames, n_src, hop)
    centers = (np.arange(n_frames, dtype=np.int64) * np.int64(hop))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=n_fft)

    # True time-domain amplitude (rectangular window, zero-padded edges).
    rms = time_domain_rms(y, n_fft, hop, n_frames)

    mag2 = S * S
    # Spectral flux: half-wave rectified magnitude increase.
    d = np.zeros_like(S)
    d[:, 1:] = np.maximum(0.0, S[:, 1:] - S[:, :-1])
    flux = d.sum(axis=0)
    flux[0] = 0.0

    denom = S.sum(axis=0)
    safe = np.where(denom > 0, denom, 1.0)
    centroid = (freqs[:, None] * S).sum(axis=0) / safe
    centroid = np.where(denom > 0, centroid, 0.0)
    cumsum = np.cumsum(S, axis=0)
    total = cumsum[-1:, :]
    rolloff = np.where(
        (total > 0).ravel(),
        freqs[np.clip(np.argmax(cumsum >= 0.85 * total, axis=0), 0, len(freqs) - 1)],
        0.0,
    )
    low = S[freqs < 300].sum(axis=0)
    mid = S[(freqs >= 300) & (freqs < 3000)].sum(axis=0)
    high = S[freqs >= 3000].sum(axis=0)
    band_total = low + mid + high
    bsafe = np.where(band_total > 0, band_total, 1.0)
    low_r, mid_r, high_r = low / bsafe, mid / bsafe, high / bsafe

    # Chroma from the POWER spectrogram (librosa's documented expectation).
    chroma = np.nan_to_num(librosa.feature.chroma_stft(S=mag2, sr=sr), nan=0.0)
    sigma_slow = max(1.0, slow_sec * sr / hop)
    slow_env = ndi.gaussian_filter1d(rms, sigma=sigma_slow)
    chroma_s = ndi.gaussian_filter1d(chroma.astype(np.float64), sigma=max(1.0, 1.0 * sr / hop), axis=1)
    lag = max(1, int(round(chroma_lag_sec * sr / hop)))
    nrm = np.linalg.norm(chroma_s, axis=0, keepdims=True)
    a = np.where(nrm > 1e-12, chroma_s / np.maximum(nrm, 1e-12), 0.0)
    b = np.roll(a, lag, axis=1)
    b[:, :lag] = a[:, :lag]  # no look-behind at the head: novelty 0
    cos = (a * b).sum(axis=0)
    both_live = (nrm.ravel() > 1e-12) & (np.roll(nrm.ravel(), lag) > 1e-12)
    harm_novelty = np.where(both_live, 0.5 * (1.0 - cos), 0.0)  # zero vectors: no change
    harm_novelty[:lag] = 0.0

    flux_s = ndi.gaussian_filter1d(flux, sigma=max(1.0, 0.12 * sr / hop))

    # Novelty comparison interval + integer-midpoint representative time.
    # start(i)/end(i) are the two compared frame positions; mid(i) is the
    # event time coordinate for chroma-novelty peaks. Early frames (i<lag)
    # have start<0 and novelty 0; event peaks never occur there, and event
    # construction clamps/validates to [0, n_src].
    _idx = np.arange(n_frames, dtype=np.int64)
    novelty_start_samples = (_idx - np.int64(lag)) * np.int64(hop)
    novelty_end_samples = _idx * np.int64(hop)
    novelty_centers = (novelty_start_samples + novelty_end_samples) // np.int64(2)

    rms_n, flat_rms, st_rms = robust_norm(rms)
    flux_n, flat_flux, st_flux = robust_norm(flux_s)
    slow_n, flat_slow, st_slow = robust_norm(slow_env)
    nov_n, flat_nov, st_nov = robust_norm(harm_novelty)

    meta = {
        "sr": int(sr),
        "n_source_samples": n_src,
        "n_fft": int(n_fft),
        "hop": int(hop),
        "reference_sr": int(REFERENCE_SR),
        "default_hop_at_reference": int(DEFAULT_HOP_AT_REFERENCE),
        "default_n_fft_at_reference": int(DEFAULT_N_FFT_AT_REFERENCE),
        "hop_duration_sec": float(hop) / float(sr),
        "window_duration_sec": float(n_fft) / float(sr),
        "hop_is_override": bool(hop_is_override),
        "n_fft_is_override": bool(n_fft_is_override),
        "default_sizing_policy": DEFAULT_SIZING_POLICY,
        "center": True,
        "padding": f"constant(zero) n_fft//2={n_fft // 2} each side",
        "padding_verified_against": "librosa 1.0.0 stft default pad_mode='constant' (NOT reflect)",
        "rms_convention": (
            "true time-domain RMS, rectangular window "
            f"frame_length={n_fft} hop_length={hop}, center=True, constant padding; "
            "unit = waveform amplitude, linear in gain"
        ),
        "chroma_input": "power spectrogram |STFT|**2 (librosa chroma_stft documented expectation)",
        "frame_center_sample": "i*hop (center=True STFT)",
        "flux_skew_note": "flux[i] uses mag frames i-1,i; nominal center i*hop, skew up to -hop/2; measured by synthetic test",
        "novelty_lag_note": (
            f"harm_novelty[i] compares interval [start(i),end(i)] with "
            f"start=(i-lag)*hop, end=i*hop, lag={lag} frames "
            f"({chroma_lag_sec:g}s); representative time is the integer "
            f"midpoint mid(i)=(start+end)//2 (t-lag/2), not the right edge"
        ),
        "novelty_lag_frames": int(lag),
        "novelty_time_convention": "midpoint: mid(i)=(start(i)+end(i))//2; integer, t-lag/2",
        "novelty_shift_samples": int((int(lag) * int(hop)) // 2),
        "silence_gate": f"struct-boundary true-silence gate: max(rms) < {SILENCE_RMS:g}",
        "frame_sec": float(hop) / float(sr),
        "slow_sec": float(slow_sec),
        "chroma_lag_sec": float(chroma_lag_sec),
        "flat": {"rms": flat_rms, "flux": flat_flux, "slow_env": flat_slow, "harm_novelty": flat_nov},
        "quantile_stats": {"rms": st_rms, "flux": st_flux, "slow_env": st_slow, "harm_novelty": st_nov},
        "caveat_register_vs_timbre": (
            "centroid/rolloff/band ratios respond to register AND timbre; "
            "not disambiguated in P0. chroma novelty is a candidate only."
        ),
    }
    return {
        "centers": centers,
        "novelty_start_samples": novelty_start_samples,
        "novelty_end_samples": novelty_end_samples,
        "novelty_centers": novelty_centers,
        "rms": rms, "flux": flux, "flux_s": flux_s,
        "centroid": centroid, "rolloff": rolloff,
        "low_r": low_r, "mid_r": mid_r, "high_r": high_r,
        "chroma": chroma, "chroma_s": chroma_s,
        "slow_env": slow_env, "harm_novelty": harm_novelty,
        "rms_n": rms_n, "flux_n": flux_n, "slow_n": slow_n, "nov_n": nov_n,
        "meta": meta,
    }


def save_features_npz(path: str, feat: dict) -> None:
    """Save arrays (+scalar meta as JSON string) to npz."""
    import json

    payload = {k: np.asarray(v) for k, v in feat.items() if k != "meta"}
    payload["meta_json"] = np.array([json.dumps(feat["meta"], ensure_ascii=False)])
    np.savez_compressed(path, **payload)

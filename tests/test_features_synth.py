"""Synthetic-signal tests: detection error vs frame-quantization error.

Convention: for an attack at source sample s and hop H,
  quantization error = |round(s/H)*H - s| <= H/2  (grid, unavoidable),
  detection error    = |detected_center - s|      (method, must be ~H).
Center/padding correction (center=True STFT, frame i centered at i*H) is
locked by the impulse test below; if it drifts, this file fails first.
"""
import numpy as np

from p0 import events as E
from p0 import features as F

SR = 22050
HOP = 512


def attack_signal(sr, dur_sec, attacks, freq=440.0):
    """Sine + piano-ish decaying harmonic bursts at `attacks` (seconds)."""
    n = int(dur_sec * sr)
    t = np.arange(n) / sr
    y = 0.05 * np.sin(2 * np.pi * freq * t)
    for a in attacks:
        s = int(a * sr)
        L = min(int(1.2 * sr), n - s)
        if L <= 0:
            continue
        te = np.arange(L) / sr
        env = np.exp(-te * 6.0)
        burst = env * (np.sin(2 * np.pi * freq * te)
                       + 0.5 * np.sin(2 * np.pi * 2 * freq * te)
                       + 0.25 * np.sin(2 * np.pi * 3 * freq * te))
        y[s:s + L] += 0.5 * burst
    return y


def nearest_accent(events, sample):
    acc = events["accent"]
    assert acc, "no accents detected at all"
    return min(acc, key=lambda e: abs(e["source_sample"] - sample))


def test_impulse_center_correction_locked():
    s = 44100
    y = np.zeros(SR * 4)
    y[s] = 1.0
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    det = nearest_accent(ev, s)
    quant = abs(round(s / HOP) * HOP - s)
    assert quant <= HOP / 2
    assert abs(det["source_sample"] - s) <= HOP, (det, s)  # detection error ~ hop
    assert det["method"].startswith("flux")


def test_piano_like_attack_detected():
    y = attack_signal(SR, 4.0, [1.0, 2.5])
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    for a in (1.0, 2.5):
        det = nearest_accent(ev, int(a * SR))
        assert abs(det["source_sample"] - int(a * SR)) <= HOP


def test_gain_step_detected():
    t = np.arange(SR * 4) / SR
    y = 0.1 * np.sin(2 * np.pi * 330 * t)
    y[2 * SR:] *= 4.0
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    det = nearest_accent(ev, 2 * SR)
    assert abs(det["source_sample"] - 2 * SR) <= int(0.15 * SR)


def test_silence_yields_no_events_and_flat_flags():
    feat = F.compute_features(np.zeros(SR * 3), SR)
    assert all(feat["meta"]["flat"].values())
    ev = E.detect_all(feat)
    assert all(len(v) == 0 for v in ev.values())


def test_tiny_signal_yields_no_events():
    t = np.arange(SR * 3) / SR
    feat = F.compute_features(1e-6 * np.sin(2 * np.pi * 440 * t), SR)
    assert feat["meta"]["flat"]["rms"]
    assert all(len(v) == 0 for v in E.detect_all(feat).values())


def test_high_freq_only_change_seen_in_brightness_not_energy():
    """High-register-only change must move brightness proxies more than rms.

    (Documents the register-vs-timbre ambiguity: we assert the DIRECTION of
    sensitivity, not a disambiguation.)
    """
    t = np.arange(SR * 4) / SR
    y = 0.30 * np.sin(2 * np.pi * 220 * t)
    y[2 * SR:] += 0.05 * np.sin(2 * np.pi * 6600 * t[2 * SR:])
    feat = F.compute_features(y, SR)
    c = feat["centers"]
    first = (c >= 0) & (c < 2 * SR)
    second = (c >= 2 * SR) & (c < 4 * SR)
    assert feat["centroid"][second].mean() > 1.5 * feat["centroid"][first].mean()
    assert (feat["high_r"][second].mean() - feat["high_r"][first].mean()) > \
        (feat["rms"][second].mean() - feat["rms"][first].mean())


def test_long_sustain_emits_almost_no_accents():
    t = np.arange(SR * 8) / SR
    y = 0.4 * np.exp(-t * 0.5) * np.sin(2 * np.pi * 220 * t)
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    assert len(ev["accent"]) <= 2


def test_rubato_spaced_attacks_all_detected():
    attacks = [1.0, 1.5, 2.4, 3.0]  # gaps 0.5 / 0.9 / 0.6 s (rubato-like)
    y = attack_signal(SR, 5.0, attacks)
    feat = F.compute_features(y, SR)
    ev = E.detect_all(feat)
    for a in attacks:
        det = nearest_accent(ev, int(a * SR))
        assert abs(det["source_sample"] - int(a * SR)) <= int(0.15 * SR), (a, det)


def test_true_rms_amplitude_and_gain_linearity():
    """rms is a real amplitude: A*sin -> A/sqrt(2), and scales with gain."""
    t = np.arange(SR * 3) / SR
    y = 0.4 * np.sin(2 * np.pi * 440 * t)
    feat = F.compute_features(y, SR)
    interior = feat["rms"][10:-10]
    assert abs(interior.mean() - 0.4 / np.sqrt(2)) < 0.01, interior.mean()
    feat4 = F.compute_features(4.0 * y, SR)
    assert np.allclose(feat4["rms"], 4.0 * feat["rms"], rtol=1e-9, atol=1e-12)


def test_silence_rms_is_zero_and_struct_empty():
    feat = F.compute_features(np.zeros(SR * 3), SR)
    assert float(np.max(feat["rms"])) == 0.0
    assert E.detect_struct_boundaries(feat) == []


def test_stft_frame_contract_and_centers():
    """n_frames == 1 + n_src//hop and center i sits at source sample i*hop."""
    n = SR * 3 + 123
    y = np.random.RandomState(0).rand(n) * 0.1
    feat = F.compute_features(y, SR)
    assert len(feat["centers"]) == 1 + n // HOP
    assert int(feat["centers"][7]) == 7 * HOP
    assert len(feat["rms"]) == len(feat["centers"])


def test_meta_records_true_conventions():
    y = attack_signal(SR, 3.0, [1.0])
    feat = F.compute_features(y, SR)
    meta = feat["meta"]
    assert "constant" in meta["padding"] and "reflect" not in meta["padding"].lower().replace(
        "not reflect", "")
    assert "power" in meta["chroma_input"].lower()
    assert "time-domain" in meta["rms_convention"]


def _steady_chord(freqs, n):
    t = np.arange(n) / SR
    y = np.zeros(n)
    for f in freqs:
        y += 0.2 * np.sin(2 * np.pi * f * t)
    return y


def test_constant_energy_chord_change_yields_novelty_candidate():
    """Energy-constant pitch-class change must leave a struct candidate.

    Crossfaded C-major -> F# trichord (equal power by construction):
    the candidate must carry harm_novelty support and sit within
    [change_start, change_end + novelty_lag + tolerance].
    """
    n = SR * 10
    a = _steady_chord([261.63, 329.63, 392.00], n)
    b = _steady_chord([369.99, 440.00, 554.37], n)
    L = int(0.5 * SR)
    s0 = 5 * SR
    w = np.linspace(0, 1, L)
    w = w * w * (3 - 2 * w)
    y = a.copy()
    y[s0:s0 + L] = (1 - w) * a[s0:s0 + L] + w * b[s0:s0 + L]
    y[s0 + L:] = b[s0 + L:]
    feat = F.compute_features(y, SR)
    lag_s = float(feat["meta"]["chroma_lag_sec"])
    cands = [e for e in E.detect_struct_boundaries(feat)
             if "harm_novelty" in e["support"]
             and s0 <= e["source_sample"] <= s0 + L + int((lag_s + 1.5) * SR)]
    assert cands, "pitch-class change at constant energy left no novelty candidate"


def test_octave_shift_novelty_much_smaller_than_pitch_class_change():
    """Chroma must react to pitch CLASS, not register: octave shift novelty
    (same pitch classes, +12 semitones) << real pitch-class change novelty.
    Direction only; no disambiguation threshold is set (see docs/07)."""
    n = SR * 10
    base = _steady_chord([261.63, 329.63, 392.00], n)
    pc = _steady_chord([369.99, 440.00, 554.37], n)   # new pitch classes
    octv = _steady_chord([523.25, 659.25, 783.99], n)  # same classes +12st
    s0 = 5 * SR
    y_pc = np.concatenate([base[:s0], pc[s0:]])
    y_oct = np.concatenate([base[:s0], octv[s0:]])
    npc = F.compute_features(y_pc, SR)["harm_novelty"]
    noct = F.compute_features(y_oct, SR)["harm_novelty"]
    c = F.compute_features(y_pc, SR)["centers"]
    m = (c >= s0) & (c < s0 + int(3.5 * SR))
    assert float(npc[m].max()) > 10 * float(noct[m].max()), (
        float(npc[m].max()), float(noct[m].max()))


def test_struct_gate_separates_silence_from_flat_envelope():
    """Flat slow envelope alone must NOT suppress chroma-novelty candidates;
    only true silence (max rms < SILENCE_RMS) yields none."""
    n_frames = 400
    hop = HOP
    centers = np.arange(n_frames, dtype=np.int64) * hop
    slow_n = np.full(n_frames, 0.5)  # perfectly flat envelope
    nov_n = np.zeros(n_frames)
    nov_n[200] = 1.0  # one sharp novelty peak
    nov_n = np.convolve(nov_n, np.ones(9) / 9.0, mode="same")
    base = {
        "meta": {"sr": SR, "hop": hop,
                 "flat": {"slow_env": True, "harm_novelty": False}},
        "centers": centers, "slow_n": slow_n, "nov_n": nov_n,
    }
    live = dict(base, rms=np.full(n_frames, 0.2))
    cands = E.detect_struct_boundaries(live)
    assert any("harm_novelty" in e["support"] for e in cands), \
        "flat envelope wrongly suppressed the chroma cue"
    silent = dict(base, rms=np.zeros(n_frames))
    assert E.detect_struct_boundaries(silent) == []

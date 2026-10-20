"""Canonical timebase: integer source samples and integer video frames.

Seconds are display-only. Intervals are half-open: [start, end).

- Audio: [start_sample, end_sample) at integer `sr` (source file grid).
- Video: [start_frame, end_frame) at rational fps = fps_num / fps_den.

Endpoint policy (no gaps / no overlaps by construction):
  frame i covers source samples [b(i), b(i+1)) where
      b(i) = clip_start_sample + (2*i*num + den) // (2*den),
  with samples_per_frame = num/den = sr*fps_den/fps_num in lowest terms.
  The division is integer-only round-half-up of the exact rational
  i*num/den; no float is involved, so large sample positions cannot drift.

  When sr * fps_den / fps_num is an integer k (e.g. 22050 Hz @ 30 fps -> k=735),
  every frame covers exactly k samples with zero remainder.

Inverse mapping:
  sample_to_frame(s) returns the largest i with b(i) <= s, computed in
  integer arithmetic as  f = (den*(2*rel+1) - 1) // (2*num),  rel = s - start.
  This is the exact inverse of frame_boundary_sample by construction
  (floor((rel+0.5)/spf) differs from it exactly when den*(2*rel+1) is a
  multiple of 2*num, i.e. on rational-grid boundary ties).

Rejects non-positive / non-integer sr and fps instead of guessing.
"""
from __future__ import annotations

import math
from fractions import Fraction


def round_half_up(x: float) -> int:
    """Deterministic round-half-up (floor(x + 0.5)) for display plumbing."""
    return int(math.floor(float(x) + 0.5))


def round_half_up_frac(num: int, den: int) -> int:
    """Exact integer round-half-up of the rational num/den (den > 0)."""
    if den <= 0:
        raise ValueError(f"den must be > 0, got {den!r}")
    num, den = int(num), int(den)
    if den < 0:
        num, den = -num, -den
    # floor(num/den + 1/2) = floor((2*num + den) / (2*den))
    return (2 * num + den) // (2 * den)


class Timebase:
    """Sample<->frame conversions for one (sr, fps) pair."""

    def __init__(self, sr: int, fps_num: int, fps_den: int = 1):
        if not isinstance(sr, int) or sr <= 0:
            raise ValueError(f"sr must be a positive int, got {sr!r}")
        if not isinstance(fps_num, int) or fps_num <= 0:
            raise ValueError(f"fps_num must be a positive int, got {fps_num!r}")
        if not isinstance(fps_den, int) or fps_den <= 0:
            raise ValueError(f"fps_den must be a positive int, got {fps_den!r}")
        self.sr = sr
        self.fps = Fraction(fps_num, fps_den)
        self.samples_per_frame = Fraction(sr * fps_den, fps_num)

    @property
    def exact_integer(self) -> bool:
        """True when every frame spans an integer sample count (no rounding)."""
        return self.samples_per_frame.denominator == 1

    def frame_boundary_sample(self, clip_start_sample: int, frame_index: int) -> int:
        """Source sample where clip-local `frame_index` starts: b(i).

        Pure integer arithmetic: b(i) = start + round_half_up(i * num/den).
        """
        if frame_index < 0:
            raise ValueError("frame_index must be >= 0")
        spf = self.samples_per_frame
        return int(clip_start_sample) + round_half_up_frac(
            frame_index * spf.numerator, spf.denominator
        )

    def clip_end_sample(self, clip_start_sample: int, n_frames: int) -> int:
        """Half-open end sample for a clip of `n_frames` frames."""
        if n_frames <= 0:
            raise ValueError("n_frames must be > 0")
        return self.frame_boundary_sample(clip_start_sample, n_frames)

    def sample_to_frame(self, clip_start_sample: int, sample: int) -> int:
        """Clip-local frame containing `sample` (exact half-open inversion).

        Returns the largest i with b(i) <= sample. b(i) <= rel holds iff
        i < den*(2*rel+1)/(2*num), so the answer is
        (den*(2*rel+1) - 1) // (2*num) in integer arithmetic.
        """
        rel = int(sample) - int(clip_start_sample)
        if rel < 0:
            raise ValueError("sample is before clip start")
        spf = self.samples_per_frame
        return (spf.denominator * (2 * rel + 1) - 1) // (2 * spf.numerator)

    def frame_to_seconds(self, frame_index: int) -> float:
        """Display-only seconds for a frame boundary."""
        return float(Fraction(frame_index, 1) / self.fps)

    def sample_to_seconds(self, sample: int) -> float:
        """Display-only seconds for a source sample."""
        return float(sample) / float(self.sr)

    def describe(self) -> dict:
        return {
            "sr": self.sr,
            "fps_num": self.fps.numerator,
            "fps_den": self.fps.denominator,
            "samples_per_frame": str(self.samples_per_frame),
            "exact_integer_grid": self.exact_integer,
            "rounding": "round_half_up_integer_arithmetic",
            "interval": "half_open_[start,end)",
        }

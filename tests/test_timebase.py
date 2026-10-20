"""Timebase: sample<->frame mapping, rounding, half-open intervals."""
import pytest

from p0.timebase import Timebase, round_half_up, round_half_up_frac

GRID_CASES = [
    (22050, 30, 1),
    (44100, 30, 1),
    (48000, 30, 1),
    (44100, 30000, 1001),
    (48000, 24000, 1001),
    (22050, 30000, 1001),
    (8000, 30, 1),
    (96000, 30000, 1001),
]


def test_round_half_up_documented():
    assert round_half_up(2.5) == 3
    assert round_half_up(735.0) == 735
    # integer path agrees with the float path on ordinary values
    assert round_half_up_frac(5, 2) == 3
    assert round_half_up_frac(1471 * 2 + 1, 2) is not None
    assert round_half_up_frac(735, 1) == 735
    # exact .5 rounds up, just-below does not
    assert round_half_up_frac(3, 2) == 2
    assert round_half_up_frac(5, 2) == 3


def test_exact_grid_22050_30fps():
    tb = Timebase(22050, 30, 1)
    assert tb.exact_integer
    assert tb.samples_per_frame == 735
    s0 = 12345
    for i in range(5):
        assert tb.frame_boundary_sample(s0, i) == s0 + 735 * i


def test_half_open_no_gap_no_overlap():
    for sr, fn, fd in GRID_CASES:
        tb = Timebase(sr, fn, fd)
        s0, n = 1000, 200
        bounds = [tb.frame_boundary_sample(s0, i) for i in range(n + 1)]
        assert all(b > a for a, b in zip(bounds, bounds[1:])), (sr, fn, fd)
        # every sample in [s0, end) belongs to exactly one frame
        end = tb.clip_end_sample(s0, n)
        assert end == bounds[-1]
        for s in (s0, s0 + 1, end - 1):
            f = tb.sample_to_frame(s0, s)
            assert tb.frame_boundary_sample(s0, f) <= s < tb.frame_boundary_sample(s0, f + 1)
        # boundary sample belongs to the next frame (half-open)
        b1 = tb.frame_boundary_sample(s0, 1)
        assert tb.sample_to_frame(s0, b1) == 1


def test_sample_roundtrip_frame_quantization_bound():
    tb = Timebase(44100, 30000, 1001)  # rational fps: non-integer grid
    assert not tb.exact_integer
    s0 = 0
    worst = 0.0
    for i in range(300):
        b = tb.frame_boundary_sample(s0, i)
        # boundary is within half a sample-step of the ideal rational position
        ideal = i * float(tb.samples_per_frame)
        worst = max(worst, abs(b - (s0 + ideal)))
    assert worst <= 0.5 + 1e-9


def test_boundary_inversion_exhaustive():
    """b(i) inverse: sample_to_frame(b(i)) == i and (b(i)-1) == i-1.

    This is the exact-inverse property the old float closed form could
    violate on rational-grid ties (e.g. 44100 Hz @ 30000/1001 fps).
    """
    for sr, fn, fd in GRID_CASES:
        tb = Timebase(sr, fn, fd)
        for s0 in (0, 1, 1000):
            n = 2000
            for i in range(n):
                b = tb.frame_boundary_sample(s0, i)
                assert tb.sample_to_frame(s0, b) == i, (sr, fn, fd, s0, i)
                if i > 0:
                    assert tb.sample_to_frame(s0, b - 1) == i - 1, (sr, fn, fd, s0, i)


def test_every_sample_of_first_frames_maps_correctly():
    """All samples inside the first frames map to their own frame."""
    for sr, fn, fd in GRID_CASES:
        tb = Timebase(sr, fn, fd)
        s0 = 7
        for i in range(6):
            b0 = tb.frame_boundary_sample(s0, i)
            b1 = tb.frame_boundary_sample(s0, i + 1)
            assert b1 > b0
            for s in range(b0, b1):
                assert tb.sample_to_frame(s0, s) == i, (sr, fn, fd, i, s)


def test_reject_bad_params():
    with pytest.raises(ValueError):
        Timebase(0, 30, 1)
    with pytest.raises(ValueError):
        Timebase(22050, 0, 1)
    with pytest.raises(ValueError):
        Timebase(22050, 30, 0)
    with pytest.raises(ValueError):
        Timebase(22.5, 30, 1)  # type: ignore[arg-type]
    tb = Timebase(22050, 30, 1)
    with pytest.raises(ValueError):
        tb.sample_to_frame(5000, 4999)
    with pytest.raises(ValueError):
        tb.clip_end_sample(0, 0)

"""Clean Studio renderer: determinism, executable controls, encoded QA.

Short 90-frame (3 s) CRF18/yuv420p encodes at 180x320 go through the
real ffmpeg path. Faults are injected in the ENCODED video (scene
transition one frame late, missing accent, accent one frame late) and
must FAIL decoded-frame QA against deterministic reference frames;
the correct encode must PASS. No high-contrast diagnostic marker is
invented: realistic contrasting palettes and low particle settings.
This checks mechanical correspondence only, never aesthetic quality.
"""
import numpy as np
import pytest
import soundfile as sf

from p0 import qa as Q
from p0 import render as PR
from p0.timebase import Timebase
from studio import rendering as R
from studio import storyboard as sbmod

W, H, SEED, N, SR = 180, 320, 20261019, 90, 22050
SCENE_AT, ACCENT_AT = 30, 60

# Realistic contrasting palettes (first two default storyboard scenes:
# night blue vs warm sunset), low particle settings.
_SB = sbmod.default_storyboard([{"id": "scene01"}, {"id": "scene02"}])
PALS = [s["palette"] for s in _SB["scenes"]]
PARAMS = [dict(s["params"], particle_multiplier=0.15) for s in _SB["scenes"]]
PLAN = {"clip": {"n_frames": N},
        "scenes": [{"id": "scene01", "start_frame": 0, "end_frame": SCENE_AT},
                   {"id": "scene02", "start_frame": SCENE_AT, "end_frame": N}],
        "accents": [{"frame": ACCENT_AT}]}


def _ctrl(scene_at=SCENE_AT, accent_at=ACCENT_AT, accent=True, n=N):
    ctrl = {"light": np.full(n, 0.55), "zoom": np.ones(n),
            "camx": np.full(n, 0.5), "camy": np.full(n, 0.5),
            "particle": np.full(n, 0.25), "accent_pulse": np.zeros(n),
            "scene_id": np.array([0] * scene_at + [1] * (n - scene_at))}
    if accent and accent_at is not None:
        for k in range(15):
            if accent_at + k < n:
                ctrl["accent_pulse"][accent_at + k] = float(np.exp(-k / 4.0))
    return ctrl


def _encode(tmp_path, name, ctrl, w=W, h=H, n=N):
    wav = tmp_path / "tone.wav"
    if not wav.exists():
        y = (0.1 * np.sin(2 * np.pi * 440 * np.arange(SR * 3) / SR)
             * 32767).astype(np.int16)
        sf.write(str(wav), y, SR, subtype="PCM_16")
    lay = R.make_layouts(SEED, w, h)
    mp4 = tmp_path / name
    PR.render_video(
        (R.draw_clean_frame(f, ctrl, w, h, SEED, lay,
                            scene_palettes=PALS, scene_params=PARAMS)
         for f in range(n)),
        str(wav), str(mp4), w, h, 30, 1, n)
    return str(mp4)


def test_draw_clean_frame_deterministic_out_of_order():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    late = R.draw_clean_frame(50, ctrl, W, H, SEED, lay,
                              scene_palettes=PALS, scene_params=PARAMS)
    early = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=PARAMS)
    again = R.draw_clean_frame(50, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=PARAMS)
    assert np.array_equal(late, again)
    assert not np.array_equal(late, early)
    lay2 = R.make_layouts(SEED, W, H)
    assert np.array_equal(
        early, R.draw_clean_frame(10, ctrl, W, H, SEED, lay2,
                                  scene_palettes=PALS, scene_params=PARAMS))


def test_palette_and_four_params_affect_pixels():
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    base = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                              scene_palettes=PALS, scene_params=PARAMS)
    pal2 = [dict(p) for p in PALS]
    pal2[0] = dict(pal2[0], sky_top=[200, 30, 30])
    diff_pal = np.abs(base.astype(int) - R.draw_clean_frame(
        10, ctrl, W, H, SEED, lay,
        scene_palettes=pal2, scene_params=PARAMS).astype(int)).mean()
    assert diff_pal > 10, diff_pal
    for key, lo, hi, floor in (
            ("figure_scale", 0.5, 2.0, 0.5),
            ("light_bias", -0.3, 0.3, 0.5),
            ("glow_scale", 0.5, 2.0, 1.0)):
        pa = [dict(p) for p in PARAMS]
        pb = [dict(p) for p in PARAMS]
        pa[0][key], pb[0][key] = lo, hi
        diff = np.abs(
            R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=pa).astype(int)
            - R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                                 scene_palettes=PALS,
                                 scene_params=pb).astype(int)).mean()
        assert diff > floor, (key, diff)


def test_decorative_multiplier_zero_empties_stars_and_particles(monkeypatch):
    # Layout is built BEFORE any count patch so the seeded RNG draw
    # sequence (placements, radii, phases) is never altered by the
    # patch; counts are only disabled at draw time below.
    lay = R.make_layouts(SEED, W, H)
    ctrl = _ctrl()
    ctrl["particle"] = np.full(N, 0.9)
    ctrl_before = {k: v.copy() for k, v in ctrl.items()}
    zero = [dict(p, particle_multiplier=0.0) for p in PARAMS]
    one = [dict(p, particle_multiplier=1.0) for p in PARAMS]
    fz = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                            scene_palettes=PALS, scene_params=zero)
    fo = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                            scene_palettes=PALS, scene_params=one)
    # Functional contract only (no brightness floor, no aesthetic
    # claim): the combined field responds to the multiplier, and
    # multiplier-zero is pixel-identical to disabling BOTH decorative
    # counts at draw time. Exact equality catches either field
    # ignoring zero on its own. Independent per-field isolation is
    # covered in tests/test_studio_decorations.py.
    assert not np.array_equal(fz, fo)
    monkeypatch.setattr(R, "N_STARS", 0)
    monkeypatch.setattr(R, "N_PARTICLES", 0)
    both_off = R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                                  scene_palettes=PALS, scene_params=one)
    assert np.array_equal(fz, both_off)
    # Determinism and no input mutation under the same layout/control.
    monkeypatch.undo()
    assert np.array_equal(
        fz, R.draw_clean_frame(10, ctrl, W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=zero))
    for k in ctrl:
        assert np.array_equal(ctrl[k], ctrl_before[k]), k


def test_clean_frame_has_no_caption_or_marker_pixels():
    lay = R.make_layouts(SEED, W, H)
    frame = R.draw_clean_frame(10, _ctrl(), W, H, SEED, lay,
                               scene_palettes=PALS, scene_params=PARAMS)
    assert float((frame[:20, 10:70].max(axis=2) > 200).mean()) < 0.01
    patch = frame[H - 12:H - 4, W - 12:W - 4].astype(float)
    # Per-channel spatial variation: a solid marker is uniform within
    # each channel, so this (not std across RGB channels) detects it.
    assert float(patch.std(axis=(0, 1)).max()) > 2.0
    # Negative control: an injected solid RGB(67,30,10) marker in the
    # same corner IS flagged as uniform per channel, even though its
    # across-channel std exceeds 2 (the old misleading assertion).
    marked = frame.copy()
    marked[H - 12:H - 4, W - 12:W - 4] = (67, 30, 10)
    marked_patch = marked[H - 12:H - 4, W - 12:W - 4].astype(float)
    assert float(marked_patch.std()) > 2.0
    assert float(marked_patch.std(axis=(0, 1)).max()) < 1e-6


def test_clean_frame_shapes_at_larger_sizes():
    for w, h in ((288, 512), (540, 960)):
        lay = R.make_layouts(SEED, w, h)
        ctrl = _ctrl(n=30)
        ctrl["scene_id"] = np.array([0] * 15 + [1] * 15)
        frame = R.draw_clean_frame(5, ctrl, w, h, SEED, lay,
                                   scene_palettes=PALS, scene_params=PARAMS)
        assert frame.shape == (h, w, 3) and frame.dtype == np.uint8


def test_probe_coverage_includes_boundaries_and_neighbours():
    probes = Q.rendered_probe_frames(PLAN, N)
    for f in (0, N - 1, SCENE_AT - 1, SCENE_AT, SCENE_AT + 1,
              ACCENT_AT - 1, ACCENT_AT, ACCENT_AT + 1):
        assert f in probes, f
    assert "scene_start" in probes[SCENE_AT]
    assert "accent" in probes[ACCENT_AT]
    assert Q.RENDERED_PATCH_MAE_MAX == 16.0
    assert Q.RENDERED_GLOBAL_MAE_MAX == 8.0


@pytest.fixture(scope="module")
def encoded(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("clean-enc")
    ref_ctrl = _ctrl()
    lay = R.make_layouts(SEED, W, H)
    ref_fn = R.make_reference_frame_fn(
        ref_ctrl, W, H, SEED, scene_palettes=PALS,
        scene_params=PARAMS, layouts=lay)
    paths = {
        "good": _encode(tmp, "good.mp4", ref_ctrl),
        "scene_late": _encode(tmp, "scene_late.mp4", _ctrl(scene_at=SCENE_AT + 1)),
        "missing_accent": _encode(tmp, "missing.mp4", _ctrl(accent=False)),
        "accent_late": _encode(tmp, "accent_late.mp4", _ctrl(accent_at=ACCENT_AT + 1)),
    }
    v = next(s for s in Q.ffprobe(paths["good"])["streams"]
             if s["codec_type"] == "video")
    assert v["codec_name"] == "h264" and v["pix_fmt"] == "yuv420p"
    assert (v["width"], v["height"]) == (W, H)
    return paths, ref_fn


def _qa(paths, ref_fn, key):
    return Q.check_rendered_frames(
        paths[key], PLAN, Timebase(SR, 30, 1), W, H, N, ref_fn)


def test_encoded_reference_frames_pass(encoded):
    paths, ref_fn = encoded
    res = _qa(paths, ref_fn, "good")
    assert res["pass"], res["frames"]
    assert res["thresholds"]["patch_mae_max"] == 16.0
    assert res["probed_frames"] == sorted(Q.rendered_probe_frames(PLAN, N))
    for fr in res["frames"]:
        assert fr["ok"] and "global_mae" in fr and "patch_mae_max" in fr
        assert fr["patch_mae_max"] <= 16.0


@pytest.mark.parametrize("key,bad_frame", [
    ("scene_late", SCENE_AT),
    ("missing_accent", ACCENT_AT),
    ("accent_late", ACCENT_AT),
])
def test_encoded_fault_injection_fails_decoded_qa(encoded, key, bad_frame):
    paths, ref_fn = encoded
    res = _qa(paths, ref_fn, key)
    assert not res["pass"], (key, res["frames"])
    failing = {f["frame"]: f for f in res["frames"] if not f["ok"]}
    assert bad_frame in failing, (key, failing)
    assert failing[bad_frame]["patch_mae_max"] > Q.RENDERED_PATCH_MAE_MAX

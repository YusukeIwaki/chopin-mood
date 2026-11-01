"""Score-driven spatial events must survive encoding and reject timing faults."""
import copy
import hashlib
import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from p0 import qa, render
from p0.timebase import Timebase
from studio import candle_rift, nocturne_chapel, rendering

W, H, N, MAJOR, OCTAVE, FAST, ACCENT = 180, 320, 240, 60, 130, 160, 210
SEED = 20261019
PLAN = {"clip": {"n_frames": N}, "scenes": [
    {"id": "scene01", "start_frame": 0, "end_frame": MAJOR},
    {"id": "scene02", "start_frame": MAJOR, "end_frame": OCTAVE},
    {"id": "scene03", "start_frame": OCTAVE, "end_frame": N}],
    "accents": [{"frame": ACCENT}], "semantic_landmarks": [
        {"id": name, "frame": frame} for name, frame in
        (("major_arrival", MAJOR), ("both_hands_octaves", OCTAVE), ("fast_coda", FAST))]}


def controls(major=MAJOR, octave=OCTAVE, fast=FAST, accent=ACCENT, n=N):
    c = {"scene_id": np.array([0]*major+[1]*(octave-major)+[2]*(n-octave)),
         "score_fast_frame": fast, "flame_envelope": np.linspace(.05, 1, n),
         "accent_pulse": np.zeros(n), "light": np.full(n, .5),
         "zoom": np.ones(n), "camx": np.full(n, .5), "camy": np.full(n, .5),
         "particle": np.zeros(n)}
    if accent is not None:
        c["accent_pulse"][accent:accent+15] = np.exp(-np.arange(15)/4)
    return c


def reference(c, mode=candle_rift.RENDERER_ID, w=W, h=H):
    lay = rendering.make_layouts(SEED, w, h)
    lay["candle_rift"] = candle_rift.make_layout(SEED, w, h, mode)
    return rendering.make_reference_frame_fn(c, w, h, SEED,
        scene_palettes=[nocturne_chapel.BASE_PALETTE]*3,
        scene_params=[{}]*3, layouts=lay)


def test_random_access_and_counterfactuals_move_actual_geometry():
    c = controls(); before = copy.deepcopy(c); fn = reference(c)
    late = fn(FAST+10).copy(); fn(0)
    assert np.array_equal(late, reference(c)(FAST+10))
    for k in c:
        assert np.array_equal(c[k], before[k])
    for b in (MAJOR, OCTAVE, FAST):
        deltas = [np.mean(np.abs(fn(f).astype(float)-
                    fn.shifted_boundary_frame(b, 1, f))) for f in range(b, b+12)]
        assert max(deltas) > .02, (b, deltas)


def test_semantic_probe_covers_internal_fast_cue():
    probes = qa.rendered_probe_frames(PLAN, N)
    assert "semantic_landmark" in probes[FAST]
    assert "semantic_landmark_pre" in probes[FAST-1]
    assert "semantic_landmark_post" in probes[FAST+1]


def test_mandatory_semantic_landmark_cannot_silently_skip_timing(monkeypatch):
    frame = np.zeros((H,W,3), np.uint8)
    monkeypatch.setattr(qa, "decode_frame_rgb", lambda *args: frame)
    def fn(f): return frame
    fn.transition_window = lambda b: None
    fn.shifted_boundary_frame = lambda *args: frame
    result = qa.check_rendered_frames("unused", PLAN, Timebase(22050,30,1),
        W,H,N,fn, require_transition_timing=True)
    assert not result["pass"]
    assert len(result["transition_timing"]) == 3
    assert all(t["reason"] == "mandatory semantic landmark has no timing window"
               for t in result["transition_timing"])


def test_candle_strength_is_an_executable_control():
    c=controls();lay=rendering.make_layouts(SEED,W,H)
    lay["candle_rift"]=candle_rift.make_layout(SEED,W,H)
    def draw(gain):
        return rendering.draw_clean_frame(MAJOR+35,c,W,H,SEED,lay,
            [nocturne_chapel.BASE_PALETTE]*3,[{"candle_strength":gain}]*3)
    assert np.mean(np.abs(draw(1).astype(float)-draw(0))) > .2


def test_late_cloud_layer_keeps_the_full_native_frame_covered():
    c=controls(major=944,octave=1637,fast=1729,accent=None,n=3172)
    lay=rendering.make_layouts(SEED,W,H)
    lay["candle_rift"]=candle_rift.make_layout(SEED,W,H)
    params=[{"camera_zoom_start":1.07,"camera_zoom_end":1.07}]*3
    frame=rendering.draw_clean_frame(3171,c,W,H,SEED,lay,
        [nocturne_chapel.BASE_PALETTE]*3,params)
    # This actual painted sky is lit at its right edge. An unfilled affine
    # region previously produced a ruler-straight black strip here.
    assert frame[:int(H*.36),-2:].mean() > 8


@pytest.mark.parametrize("case,boundary", [
    ("good", None), ("late_major", MAJOR), ("late_octave", OCTAVE),
    ("late_fast", FAST), ("missing_accent", None)])
def test_encoded_spatial_events_reject_faults(tmp_path, case, boundary):
    c = controls(major=MAJOR+(case=="late_major"),
                 octave=OCTAVE+(case=="late_octave"),
                 fast=FAST+(case=="late_fast"),
                 accent=None if case=="missing_accent" else ACCENT)
    wav = tmp_path/"tone.wav"
    sf.write(wav, .1*np.sin(np.arange(22050*8)*2*np.pi*440/22050), 22050)
    path = tmp_path/"test.mp4"; actual = reference(c)
    render.render_video((actual(f) for f in range(N)), str(wav), str(path), W, H, 30, 1, N)
    expected = reference(controls())
    if boundary is not None:
        result = qa.check_transition_timing(str(path), boundary,
            expected.transition_window(boundary), W, H, N, expected)
        passed = result["ok"]
    else:
        result = qa.check_rendered_frames(str(path), PLAN, Timebase(22050,30,1),
            W, H, N, expected, require_transition_timing=True)
        passed = result["pass"]
    assert passed == (case=="good"), json.dumps({"case":case,"result":result},indent=2)


CLEARING, CLEARING_N = 300, 480


def clearing_controls(cue=CLEARING):
    return dict(controls(n=CLEARING_N, accent=None), score_clearing_frame=cue)


def clearing_plan():
    value = copy.deepcopy(PLAN)
    value["clip"]["n_frames"] = CLEARING_N
    value["scenes"][-1]["end_frame"] = CLEARING_N
    value["semantic_landmarks"].append({"id": "final_scale_arrival", "frame": CLEARING,
                                       "containing_frame": CLEARING-1,
                                       "frame_policy": "first_frame_boundary_at_or_after_native_source_sample"})
    value["visual_renderer"] = {"renderer_id": candle_rift.CLEARING_RENDERER_ID}
    return value


def test_soft_flame_has_transmitted_background_and_feathered_light():
    from PIL import Image
    a = np.asarray(candle_rift._soft_flame(Image.new("RGB",(120,160),(15,15,15)),60,130,80,3),float)
    b = np.asarray(candle_rift._soft_flame(Image.new("RGB",(120,160),(35,35,35)),60,130,80,3),float)
    active = a[...,0] > 50
    # A brighter background still transmits through the luminous flame;
    # opaque nested teardrops would erase it throughout their filled area.
    assert np.mean((b-a)[active]) > 12
    assert np.max(np.abs(np.diff(a[70:125,:,0],axis=1))) < 65
    assert np.count_nonzero((a[...,0]>15)&(a[...,0]<45)) > 100


def test_new_mode_reveals_sky_spatially_after_cue_and_protects_rock():
    c = clearing_controls()
    asset = candle_rift.make_layout(SEED,W,H,candle_rift.CLEARING_RENDERER_ID)
    masks = [candle_rift.clearing_state(f,c,asset)[0] for f in (CLEARING-1,CLEARING,CLEARING+60,CLEARING+179)]
    assert not masks[0].any()
    assert 0 < masks[1].sum() < masks[2].sum() < masks[3].sum()
    assert np.array_equal(masks[3],asset["sky_mask"])
    assert all(not mask[int(H*.40):].any() for mask in masks)
    # Test actual rendered pixels at identical source/camera/motion time.
    asset["embers"] = np.empty((0,4))
    before = np.asarray(candle_rift._world(CLEARING+160,dict(c,score_clearing_frame=CLEARING+170),W,H,asset,FAST))
    after = np.asarray(candle_rift._world(CLEARING+160,c,W,H,asset,FAST))
    assert np.mean(np.abs(after[:H//4].astype(float)-before[:H//4])) > 10
    assert np.array_equal(before[int(H*.55):],after[int(H*.55):])
    fn = reference(c,candle_rift.CLEARING_RENDERER_ID)
    early = fn(CLEARING-1)
    assert np.array_equal(early,reference(dict(c,score_clearing_frame=CLEARING+1),candle_rift.CLEARING_RENDERER_ID)(CLEARING-1))
    late = fn(CLEARING_N-1).copy();fn(0)
    assert np.array_equal(late,reference(c,candle_rift.CLEARING_RENDERER_ID)(CLEARING_N-1))
    assert fn.patch_mae_max == 5.0
    assert max(np.mean(np.abs(fn(f).astype(float)-fn.shifted_boundary_frame(CLEARING,1,f)))
               for f in range(CLEARING,CLEARING+12)) > .02


def test_new_world_never_affine_copies_rock_bearing_image_into_sky(monkeypatch):
    from PIL import Image
    asset=candle_rift.make_layout(SEED,W,H,candle_rift.CLEARING_RENDERER_ID)
    asset["embers"]=np.empty((0,4))
    calls=[];original=Image.Image.transform
    def observed(self,size,method,*args,**kwargs):
        calls.append(method)
        return original(self,size,method,*args,**kwargs)
    monkeypatch.setattr(Image.Image,"transform",observed)
    candle_rift._world(CLEARING+120,clearing_controls(),W,H,asset,FAST)
    assert Image.Transform.AFFINE not in calls
    # These are native rock interiors, not sky in the newly preserved concave
    # gap under the leaning left spire or an antialiased boundary pixel.
    for x,y in ((.02,.30),(.99,.35),(.5,.44)):
        assert asset["sky_mask"][int(y*H),int(x*W)] == 0


def test_native_mask_rebuild_matches_registered_geometry():
    import importlib.util
    from pathlib import Path
    path=Path(candle_rift.__file__).resolve().parents[1]/"assets/visual/candle-clearing-v1/build_mask.py"
    spec=importlib.util.spec_from_file_location("registered_mask_recipe",path)
    recipe=importlib.util.module_from_spec(spec);spec.loader.exec_module(recipe)
    from PIL import Image
    expected=np.asarray(Image.open(path.parent/"sky-mask.png"))
    assert expected.shape == (1672,940)
    assert np.array_equal(expected,np.asarray(recipe.build()))


def test_mask_verifier_checks_registered_bytes_and_accepts_other_valid_compression(tmp_path,monkeypatch):
    import importlib.util
    from pathlib import Path
    from PIL import Image
    path=Path(candle_rift.__file__).resolve().parents[1]/"assets/visual/candle-clearing-v1/build_mask.py"
    spec=importlib.util.spec_from_file_location("mask_verify_recipe",path)
    recipe=importlib.util.module_from_spec(spec);spec.loader.exec_module(recipe)
    with Image.open(path.parent/"sky-mask.png") as image:
        rebuilt=image.copy()
    rebuilt.save(tmp_path/"sky-mask.png",compress_level=0)
    manifest={"files":{"sky-mask.png":hashlib.sha256((tmp_path/"sky-mask.png").read_bytes()).hexdigest()}}
    (tmp_path/"manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(recipe,"BUNDLE",tmp_path)
    recipe.verify_registered(rebuilt)
    (tmp_path/"sky-mask.png").write_bytes(b"tampered")
    with pytest.raises(ValueError,match="hash mismatch"):
        recipe.verify_registered(rebuilt)
    altered=np.asarray(rebuilt).copy();altered[0,0]=0
    Image.fromarray(altered).save(tmp_path/"sky-mask.png")
    manifest["files"]["sky-mask.png"]=hashlib.sha256((tmp_path/"sky-mask.png").read_bytes()).hexdigest()
    (tmp_path/"manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError,match="pixels differ"):
        recipe.verify_registered(rebuilt)


def test_counterfactual_works_at_exact_sixty_frame_eligibility_edge():
    c=clearing_controls(CLEARING_N-60)
    fn=reference(c,candle_rift.CLEARING_RENDERER_ID)
    for shift in (-2,-1,0,1,2):
        frame=fn.shifted_boundary_frame(CLEARING_N-60,shift,CLEARING_N-50)
        assert frame.shape == (H,W,3)


def test_cleared_ending_keeps_terrain_motion_and_suppresses_storm_flash():
    c=clearing_controls()
    asset=candle_rift.make_layout(SEED,W,H,candle_rift.CLEARING_RENDERER_ID)
    last=CLEARING_N-1
    ordinary=np.asarray(candle_rift._world(last,c,W,H,asset,FAST))
    flashing=copy.deepcopy(c);flashing["accent_pulse"][last]=1
    assert np.array_equal(ordinary,np.asarray(candle_rift._world(last,flashing,W,H,asset,FAST)))
    previous=np.asarray(candle_rift._world(last-20,c,W,H,asset,FAST))
    assert np.mean(np.abs(ordinary[H//2:].astype(float)-previous[H//2:])) > .1


def test_legacy_mode_ignores_optional_new_clearing_control():
    c = controls()
    old = reference(c)
    extra = reference(dict(c,score_clearing_frame=FAST+1))
    for f in (0,MAJOR+25,OCTAVE+12,FAST+25,N-1):
        assert np.array_equal(old(f),extra(f))


def test_clearing_requires_cue_and_probes_its_activation():
    probes = qa.rendered_probe_frames(clearing_plan(),CLEARING_N)
    assert "semantic_landmark" in probes[CLEARING]
    with pytest.raises((ValueError,KeyError)):
        reference(controls(n=CLEARING_N),candle_rift.CLEARING_RENDERER_ID)(CLEARING)


def test_clearing_motion_evidence_retains_scene_windows_and_adds_onset():
    from studio import visual_preflight as V
    plan = clearing_plan()
    scenes = V._scenes_of(plan["scenes"])
    # Put cue after the original scene03 midpoint window.
    plan["semantic_landmarks"][-1]["frame"] = 400
    original = V._motion_windows(scenes)
    windows = V._motion_windows(scenes,plan)
    assert windows[:3] == original
    assert len(windows) == 4 and windows[-1]["frame_count"] == 60
    assert windows[-1]["scene_id"] == "scene03:final_scale_arrival"
    assert windows[-1]["source_start_frame"] <= 400 < windows[-1]["source_end_frame"]
    assert windows[-1]["preview_start_frame"] == 180
    samples, expected = V._expected_samples(scenes,"motion",plan)
    assert len(samples) == 12 and expected == windows
    assert all(a["source_end_frame"] <= b["source_start_frame"] for a,b in zip(windows[:-1],windows[1:]))


@pytest.mark.parametrize("position,append", [("before",True),("start",False),("mid",False),("last",True),("after",True),("far",True)])
def test_semantic_motion_boundaries_always_cover_thirty_postcue_frames(position,append):
    from studio import visual_preflight as V
    scenes=[{"id":"scene03","start_frame":0,"end_frame":240}]
    original=V._motion_windows(scenes)
    ws,we=original[0]["source_start_frame"],original[0]["source_end_frame"]
    cue={"before":ws-1,"start":ws,"mid":(ws+we)//2,"last":we-1,"after":we,"far":180}[position]
    plan={"visual_renderer":{"renderer_id":candle_rift.CLEARING_RENDERER_ID},
          "semantic_landmarks":[{"id":"final_scale_arrival","frame":cue}]}
    windows=V._motion_windows(scenes,plan)
    assert windows[:1] == original
    assert len(windows) == 1+append
    post=set()
    for window in windows:
        assert 0 <= window["source_start_frame"] < window["source_end_frame"] <= 240
        assert window["frame_count"] <= 60
        post.update(range(max(cue,window["source_start_frame"]),window["source_end_frame"]))
    assert len(post) >= 30
    ordered=sorted(windows,key=lambda w:w["source_start_frame"])
    assert all(a["source_end_frame"] <= b["source_start_frame"] for a,b in zip(ordered,ordered[1:]))
    assert any(w["source_start_frame"] <= cue < w["source_end_frame"] for w in windows)


def test_prepare_context_ceil_activation_and_public_semantic_frame(tmp_path):
    from studio import storyboard
    from studio.render_context import prepare_context
    sr=22050;tb=Timebase(sr,30)
    wav=tmp_path/"synthetic-context.wav"
    sf.write(wav,.1*np.sin(np.arange(sr*16)*2*np.pi*440/sr),sr)
    samples=[tb.frame_boundary_sample(0,f) for f in (MAJOR,OCTAVE,FAST)]
    last=tb.frame_boundary_sample(0,CLEARING-1)+100
    events=[{"id":name,"source_sample":s,"frame":tb.sample_to_frame(0,s)}
            for name,s in zip(("major_arrival","both_hands_octaves","fast_coda","final_scale_arrival"),samples+[last])]
    scenes=[{**s,"start_sample":tb.frame_boundary_sample(0,s["start_frame"]),
             "end_sample":tb.frame_boundary_sample(0,s["end_frame"])} for s in clearing_plan()["scenes"]]
    candidate={"id":"cand-02","start_sample":0,"end_sample":sr*16,"n_frames":CLEARING_N,
               "sample_rate":sr,"fps":[30,1],"scenes":scenes,"accents":[],
               "analysis_profile":"chopin-scherzo3-coda-clearing-v1","score_landmarks":events,
               "score_alignment":{"profile_id":"chopin-scherzo3-coda-clearing-v1"}}
    sb=storyboard.default_storyboard(scenes);sb["renderer"]=candle_rift.CLEARING_RENDERER_ID
    brief={"candidate":candidate,"source_hashes":{"decoded_wav_sha256":hashlib.sha256(wav.read_bytes()).hexdigest()}}
    for name,value in (("candidate",candidate),("storyboard",sb),("brief",brief)):
        (tmp_path/(name+".json")).write_text(json.dumps(value))
    context=prepare_context(wav,tmp_path/"candidate.json",tmp_path/"storyboard.json",tmp_path/"brief.json",width=W,height=H)
    assert context.controls["score_clearing_frame"] == CLEARING
    assert context.candidate["score_landmarks"][-1]["frame"] == CLEARING-1
    semantic=context.public_plan["semantic_landmarks"][-1]
    assert semantic["frame"] == CLEARING and semantic["containing_frame"] == CLEARING-1
    assert semantic["frame_policy"] == "first_frame_boundary_at_or_after_native_source_sample"
    assert tb.frame_boundary_sample(0,semantic["frame"]) >= semantic["source_sample"]
    assert tb.frame_boundary_sample(0,semantic["frame"]-1) < semantic["source_sample"]
    # A raw containing frame with sixty frames left is still too late when
    # its source sample requires activation on the following boundary.
    late_sample=tb.frame_boundary_sample(0,CLEARING_N-60)+100
    candidate["score_landmarks"][-1].update(source_sample=late_sample,frame=CLEARING_N-60)
    for name,value in (("candidate",candidate),("brief",brief)):
        (tmp_path/(name+".json")).write_text(json.dumps(value))
    with pytest.raises(ValueError,match="sixty remaining frames"):
        prepare_context(wav,tmp_path/"candidate.json",tmp_path/"storyboard.json",tmp_path/"brief.json",width=W,height=H)


def test_generated_clearing_preflight_revalidates_appended_evidence(tmp_path):
    from studio import visual_preflight as V
    c=clearing_controls(400)
    plan=clearing_plan();plan["semantic_landmarks"][-1]["frame"]=400
    fn=reference(c,candle_rift.CLEARING_RENDERER_ID)
    context=SimpleNamespace(brief={"parent_issue":30,"child_issue":None},candidate={"id":"cand-02"},
        storyboard={"renderer":candle_rift.CLEARING_RENDERER_ID},public_plan=plan,
        controls=c,width=W,height=H,seed=SEED,source_sha256="a"*64,
        det_scenes=plan["scenes"],clip=plan["clip"],frame=fn)
    for stage in ("still","motion"):
        out=tmp_path/stage
        evidence=V.generate_evidence(context,out,stage=stage,engine_sha="a"*40,run_id=1,run_attempt=1,iteration=0)
        checked=V.validate_evidence(out,context=context,expected_engine_sha="a"*40)
        assert checked["ok"],checked["failures"]
        if stage == "still":
            assert evidence["samples"][-1]["source_frame"] == CLEARING_N-1
        else:
            assert len(evidence["motion_qa"]["windows"]) == 4
            assert evidence["motion_qa"]["windows"][-1]["scene_id"] == "scene03:final_scale_arrival"


@pytest.mark.parametrize("case", ["good", "late_one_frame", "missing_clearing"])
def test_encoded_clearing_rejects_late_and_missing_visual_event(tmp_path,case):
    # New high-frequency painted mode is calibrated at its production size.
    # The 180x320 experiment failed strict patch5 (5.14 at the ending);
    # this does not widen that bound or claim thumbnail support.
    width,height=540,960
    c = clearing_controls(CLEARING+(case=="late_one_frame"))
    actual = reference(c,candle_rift.CLEARING_RENDERER_ID,width,height)
    if case == "missing_clearing":
        lay = rendering.make_layouts(SEED,width,height)
        lay["candle_rift"] = candle_rift.make_layout(SEED,width,height,candle_rift.CLEARING_RENDERER_ID)
        lay["candle_rift"]["cleared_world"] = lay["candle_rift"]["world"].copy()
        actual = rendering.make_reference_frame_fn(c,width,height,SEED,
            scene_palettes=[nocturne_chapel.BASE_PALETTE]*3,scene_params=[{}]*3,layouts=lay)
    wav=tmp_path/"synthetic-tone.wav"
    sf.write(wav,.1*np.sin(np.arange(22050*16)*2*np.pi*440/22050),22050)
    video=tmp_path/"clearing.mp4"
    def frames():
        for f in range(CLEARING_N):
            value=actual(f)
            actual.cache.pop(f,None)  # Stream native frames; no full-film cache.
            yield value
    render.render_video(frames(),str(wav),str(video),width,height,30,1,CLEARING_N)
    expected=reference(clearing_controls(),candle_rift.CLEARING_RENDERER_ID,width,height)
    timing=qa.check_transition_timing(str(video),CLEARING,expected.transition_window(CLEARING),width,height,CLEARING_N,expected)
    if case == "good":
        assert timing["ok"],json.dumps(timing,indent=2)
        checked=qa.check_rendered_frames(str(video),clearing_plan(),Timebase(22050,30),width,height,CLEARING_N,
                                         expected,require_transition_timing=True)
        passed=checked["pass"]
    else:
        checked=timing
        passed=timing["ok"]
        if case == "late_one_frame":
            assert timing["best_offset_frames"] == 1,json.dumps(timing,indent=2)
    assert passed == (case=="good"),json.dumps(checked,indent=2)

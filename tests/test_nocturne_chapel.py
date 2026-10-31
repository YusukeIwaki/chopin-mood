"""Chapel provider: pixel timing, immutable sources, and source-ending coverage."""
import copy
import hashlib
import json
import shutil

import numpy as np
import pytest

from p0 import clip, events, features, qa, render
from p0.timebase import Timebase
from studio import nocturne_chapel as chapel, rendering, storyboard
from test_studio_painted import controls, PLAN, N, B, A, W, H, SEED

PALS = [chapel.BASE_PALETTE, {**chapel.BASE_PALETTE, 'far': (48,40,29)}]
PARAMS = [{'candle_strength': .1, 'chapel_warmth': 0}, {'candle_strength': .8, 'chapel_warmth': .7}]

def reference(renderer, ctrl):
    lay = rendering.make_layouts(SEED,W,H)
    lay['nocturne_chapel'] = chapel.make_layout(SEED,W,H,renderer)
    return rendering.make_reference_frame_fn(ctrl,W,H,SEED,scene_palettes=PALS,scene_params=PARAMS,layouts=lay)

@pytest.mark.parametrize('renderer', chapel.RENDERERS)
def test_random_access_does_not_change_source_or_controls(renderer):
    c=controls();before={k:v.copy() for k,v in c.items()}
    ref=reference(renderer,c)
    late=ref(65).copy();ref(1)
    assert np.array_equal(late,reference(renderer,c)(65))
    for k in c: assert np.array_equal(c[k],before[k])
    assert np.mean(np.abs(ref(10).astype(float)-ref(25))) > .01

def test_corrupt_asset_rejected(tmp_path, monkeypatch):
    dest=tmp_path/chapel.DIRECTORY
    shutil.copytree(chapel.ROOT/chapel.DIRECTORY,dest)
    (dest/'candle.png').write_bytes(b'changed')
    monkeypatch.setattr(chapel,'ROOT',tmp_path)
    with pytest.raises(ValueError,match='digest mismatch'):
        chapel.make_layout(SEED,W,H,'last-candle-v1')

def test_storyboard_bounds_and_schema_agree():
    sb=storyboard.default_storyboard([{'id':'scene01'}]);sb['renderer']='last-candle-v1'
    sb['scenes'][0]['params'].update(candle_strength=.1,chapel_warmth=.6)
    assert storyboard.validate_storyboard(sb,[{'id':'scene01'}])['ok']
    for bad in [-.1,1.1,float('nan')]:
        other=copy.deepcopy(sb);other['scenes'][0]['params']['candle_strength']=bad
        assert not storyboard.validate_storyboard(other,[{'id':'scene01'}])['ok']

@pytest.mark.parametrize('renderer',chapel.RENDERERS)
def test_encoded_accents_and_scene_faults_are_detected(tmp_path,renderer):
    import soundfile as sf
    wav=tmp_path/'tone.wav';sf.write(wav,.1*np.sin(np.arange(22050*3)*2*np.pi*440/22050),22050)
    for name,c in [('good',controls()),('late_scene',controls(boundary=B+1)),
                   ('missing_accent',controls(accent=None)),('late_accent',controls(accent=A+1))]:
        path=tmp_path/(name+'.mp4');fn=reference(renderer,c)
        render.render_video((fn(f) for f in range(N)),str(wav),str(path),W,H,30,1,N)
        result=qa.check_rendered_frames(str(path),PLAN,Timebase(22050,30,1),W,H,N,reference(renderer,controls()),require_transition_timing=True)
        assert result['pass']==(name=='good'),result

def test_terminal_candidate_keeps_recorded_ending_without_changing_ranked_ids():
    # Last 5 s are deliberately unlike the earlier material. Even if the
    # acoustic score prefers an interior window, the endpoint must survive.
    sr=1000;tb=Timebase(sr,30,1);n=sr*160
    t=np.arange(n)/sr;y=.1*np.sin(2*np.pi*110*t)*(1+.7*np.sin(t*.31))
    y[-sr*5:]=.35*np.sin(2*np.pi*240*t[-sr*5:])
    feat=features.compute_features(y,sr);ev=events.detect_all(feat)
    ranked=clip.rank_excerpt_candidates(feat,ev,tb,n)
    original=clip.diversity_filter(clip.select_clip(feat,ev,tb,n,min_sec=60,max_sec=120)['ranked'])
    for actual,old in zip(ranked['candidates'],original):
        assert (actual['start_sample'],actual['end_sample'])==(old['start_sample'],old['end_sample'])
    assert any(0<=n-c['end_sample']<float(tb.samples_per_frame) for c in ranked['candidates'])
    for c in ranked['candidates']:
        assert tb.clip_end_sample(c['start_sample'],c['n_frames'])==c['end_sample']
        assert 60 <= (c['end_sample']-c['start_sample'])/sr <= 120

def test_terminal_boundary_retains_a_complete_rational_fps_frame():
    tb=Timebase(44100,30000,1001)
    n=tb.clip_end_sample(0,1801)
    centers=np.arange(0,n,1000)
    feat={'centers':centers,'rms':np.ones(len(centers))*.1,
          'slow_n':np.ones(len(centers))*.3}
    selected=clip.select_clip(feat,{},tb,n,min_sec=60,max_sec=61,source_ending=True)['best']
    assert selected['n_frames']==1801
    assert selected['end_sample']==n
    next_boundary=tb.clip_end_sample(0,1802)
    assert selected['end_sample'] <= n < next_boundary

"""The revised figure must remain an opaque, connected, deterministic asset."""
import numpy as np
import pytest
from scipy.ndimage import label
from studio.rendering import _traveller_layer, PALETTES
from studio import rendering as R
@pytest.mark.parametrize('scale',[0.8,2.7,5.4])
@pytest.mark.parametrize('palette',PALETTES)
def test_figure_connected_without_detached_head_or_hand(scale,palette):
    image=np.asarray(_traveller_layer(scale,palette))
    mask=image[:,:,3]>=128
    _,count=label(mask)
    assert count==1
    assert mask.sum()>100
    assert np.array_equal(image,np.asarray(_traveller_layer(scale,palette)))

def test_moon_only_palette_changes_are_independent_of_cache_history():
    ctrl={name:np.full(1,value) for name,value in
          [('light',.5),('zoom',1.),('camx',.5),('camy',.5),
           ('particle',0.),('accent_pulse',0.),('scene_id',0)]}
    a=dict(PALETTES[0]);b=dict(a,moon=(90,30,20))
    assert not np.array_equal(np.asarray(_traveller_layer(1.,a)),np.asarray(_traveller_layer(1.,b)))
    for first,second in ((a,b),(b,a)):
        shared=R.make_layouts(12,180,320)
        R.draw_clean_frame(0,ctrl,180,320,12,shared,scene_palettes=[first])
        reused=R.draw_clean_frame(0,ctrl,180,320,12,shared,scene_palettes=[second])
        fresh=R.draw_clean_frame(0,ctrl,180,320,12,R.make_layouts(12,180,320),scene_palettes=[second])
        assert np.array_equal(reused,fresh)

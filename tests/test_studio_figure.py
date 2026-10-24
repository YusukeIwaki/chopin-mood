"""The revised figure must remain an opaque, connected, deterministic asset."""
import numpy as np
import pytest
from scipy.ndimage import label
from studio.rendering import _traveller_layer, PALETTES
@pytest.mark.parametrize('scale',[0.8,2.7,5.4])
@pytest.mark.parametrize('palette',PALETTES)
def test_figure_connected_without_detached_head_or_hand(scale,palette):
    image=np.asarray(_traveller_layer(scale,palette))
    mask=image[:,:,3]>=128
    _,count=label(mask)
    assert count==1
    assert mask.sum()>100
    assert np.array_equal(image,np.asarray(_traveller_layer(scale,palette)))

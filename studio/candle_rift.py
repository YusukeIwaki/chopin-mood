"""A performed crescendo, a collapsing painted chapel and a storm beyond.

Scene boundaries and the registered fast-coda cue determine event positions.
The original audio supplies the flame envelope. No source seconds live here.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy.ndimage import distance_transform_edt, gaussian_filter

from .nocturne_chapel import _flame, _state

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual/candle-rift-v1")
RENDERER_ID = "candle-rift-v1"
CLEARING_RENDERER_ID = "candle-clearing-v1"
CLEARING_DIRECTORY = Path("assets/visual/candle-clearing-v1")
CLEARING_FRAMES = 180


def _cloud_recession(world, sky_mask, seed):
    """When each painted cloud yields, in source-image coordinates.

    Bright fissures and the original small cloud rims break up larger banks.
    Two seeded, low-frequency fields vary their recession without a moving
    geometric window or independently flickering per-frame noise.
    """
    h, w = sky_mask.shape
    luma = world @ np.array([.2126, .7152, .0722], np.float32) / 255
    fine = gaussian_filter(luma, max(.5, .002*w))
    banks = gaussian_filter(luma, max(1., .018*w))
    broad = gaussian_filter(luma, max(1., .065*w))
    rng = np.random.RandomState(seed ^ 0xC10D)

    def spatial(cols, rows):
        values = rng.uniform(-1, 1, (rows, cols)).astype(np.float32)
        return np.asarray(Image.fromarray(values).resize(
            (w, h), Image.Resampling.BICUBIC), np.float32)

    y = (np.arange(h, dtype=np.float32)[:, None]+.5)/h
    field = (.34*(1-np.clip(y/.40, 0, 1))-.47*banks
             -.31*(fine-broad)+.16*spatial(6, 12)+.065*spatial(19, 34))
    # A few pre-existing bright cracks open on the activation frame. The
    # storm stays completely intact before that frame; later pixels reveal
    # monotonically, with dense cloud banks surviving until the late opening.
    low, high = np.percentile(field[sky_mask > .98], [1, 99.5])
    arrival = ((field-low)/max(float(high-low), 1e-6)).astype(np.float32)
    # Follow the painted small-scale ridges rather than giving every lobe
    # the same cutout edge. These are source-image pixel widths, not field
    # value feathers (which make flat regions a broad gray crossfade).
    texture = np.clip(.5+4*(fine-banks), 0, 1)
    rim = ((4+6*texture)*w/540).astype(np.float32)
    return arrival, rim


def _cloud_opacity(arrival, rim, threshold):
    """Opaque banks give way to textured rims, then translucent thin wisps.

    Distance and local support both decrease as the surviving cloud set
    shrinks. No component is deleted at an area threshold, and no history or
    per-frame noise is needed. A small detached lobe loses opacity across
    its thickness before it could become an opaque floating chip.
    """
    remaining = arrival > threshold
    if not remaining.any():
        return np.zeros_like(arrival)
    if remaining.all():
        return np.ones_like(arrival)
    inside = distance_transform_edt(remaining)
    outside = distance_transform_edt(~remaining)
    # The nearest-pixel distance alone jumps when a contour crosses a
    # pixel. Its field residual supplies the subpixel motion through that
    # pixel, while the distance retains a bounded spatial rim at flat peaks.
    dy, dx = np.gradient(arrival)
    residual = np.clip((arrival-threshold)/np.maximum(np.hypot(dx,dy),1e-6),-1,1)
    distance = np.where(remaining,inside-1,1-outside)+residual
    edge = np.clip(.5+distance/rim,0,1)
    opacity = edge*edge*(3-2*edge)
    # Wide banks retain their original texture and opaque interiors. Thin
    # necks and isolated remnants have less nearby cloud mass, so their
    # whole density ebbs continuously, not just the outermost antialias.
    support = gaussian_filter(opacity, .03*arrival.shape[1])
    thinning = np.clip(support/.9,0,1)
    return (opacity*thinning*thinning*(3-2*thinning)).astype(np.float32)


def _smooth(x):
    x = float(np.clip(x, 0, 1))
    return x*x*(3-2*x)


def _boundary(ctrl, stage):
    ids = np.asarray(ctrl["scene_id"])
    found = np.flatnonzero(ids >= stage)
    return int(found[0]) if found.size else len(ids)


def event_frames(ctrl):
    """Scene counterfactuals move the real flame/collapse, not a QA marker."""
    events = {"major_arrival": _boundary(ctrl, 1),
            "both_hands_octaves": _boundary(ctrl, 2),
            "fast_coda": int(ctrl["score_fast_frame"])}
    if "score_clearing_frame" in ctrl:
        events["final_scale_arrival"] = int(ctrl["score_clearing_frame"])
    return events


def make_layout(seed, w, h, renderer_id=RENDERER_ID):
    if renderer_id not in (RENDERER_ID, CLEARING_RENDERER_ID):
        raise ValueError("unknown candle renderer")
    directory = ROOT / DIRECTORY
    raw = (directory / "manifest.json").read_bytes()
    manifest = json.loads(raw)
    if manifest["bundle_id"] != RENDERER_ID or set(manifest["files"]) != {"world.png"}:
        raise ValueError("unexpected rift asset bundle")
    sources = {DIRECTORY / n: digest for n, digest in manifest["files"].items()}
    candle = manifest["candle_reference"]
    if candle["path"] != "assets/visual/nocturne-chapel-v1/candle.png":
        raise ValueError("unexpected fixed candle reference")
    sources[Path(candle["path"])] = candle["sha256"]
    for path, digest in sources.items():
        source = ROOT / path
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
            raise ValueError(f"rift asset hash mismatch: {path}")
    def load(path):
        with Image.open(ROOT / path) as image:
            return np.asarray(image.convert("RGB").resize((w, h), Image.Resampling.LANCZOS), np.float32)
    rng = np.random.RandomState(seed ^ 0xC01A)
    # Shared jittered vertices form a watertight wall, then fall as rigid stone.
    cols, rows = 7, 12
    points = np.stack(np.meshgrid(np.linspace(0,w,cols+1), np.linspace(0,h,rows+1)), axis=-1)
    points[1:-1,1:-1,0] += rng.uniform(-.34,.34,(rows-1,cols-1))*w/cols
    points[1:-1,1:-1,1] += rng.uniform(-.34,.34,(rows-1,cols-1))*h/rows
    shards = []
    for row in range(rows):
        for col in range(cols):
            poly = points[[row,row,row+1,row+1],[col,col+1,col+1,col]]
            x0,y0 = np.floor(poly.min(axis=0)).astype(int)
            x1,y1 = np.ceil(poly.max(axis=0)).astype(int)
            mask = Image.new("L",(x1-x0+1,y1-y0+1),0)
            ImageDraw.Draw(mask).polygon([tuple(v-[x0,y0]) for v in poly],fill=255)
            cx,cy = poly.mean(axis=0)/[w,h]
            shards.append({"box":(x0,y0,x1+1,y1+1),"mask":mask,
                           "delay":.15*abs(cx-.5)+.09*(1-cy),
                           "vx":float((cx-.5)*.65+rng.uniform(-.09,.09)),
                           "rotation":float(rng.uniform(-34,34)),
                           "yaw":float(rng.choice([-1,1])*rng.uniform(1.5,2.7)),
                           "thickness":float(rng.uniform(.012,.024)*w),
                           "depth":float(rng.uniform(.7,1.2))})
    layout = {"candle":load(Path(candle["path"])),"world":load(DIRECTORY/"world.png"),
            "wick":tuple(candle["wick_anchor"]),"shards":shards,
            "phase":float(rng.uniform(0,6.28)),
            "x":(np.arange(w,dtype=np.float32)[None,:]+.5)/w,
            "y":(np.arange(h,dtype=np.float32)[:,None]+.5)/h,
            "embers":rng.uniform(0,1,(42,4)),
            "provenance":{"bundle_id":RENDERER_ID,
                "manifest_path":(DIRECTORY/"manifest.json").as_posix(),
                "manifest_sha256":hashlib.sha256(raw).hexdigest(),
                "files":{p.as_posix():digest for p,digest in sources.items()}}}
    if renderer_id == CLEARING_RENDERER_ID:
        extension_raw = (ROOT / CLEARING_DIRECTORY / "manifest.json").read_bytes()
        extension = json.loads(extension_raw)
        if (extension["bundle_id"] != CLEARING_RENDERER_ID or
                extension.get("content_role") != "sky-only" or
                set(extension["files"]) != {"cleared-world.png","sky-mask.png","mask-guide.json","build_mask.py"} or
                extension["parent_manifest"] != {"path": (DIRECTORY/"manifest.json").as_posix(),
                                                "sha256": hashlib.sha256(raw).hexdigest()} or
                extension["world_reference"] != {"path": (DIRECTORY/"world.png").as_posix(),
                                                "sha256": manifest["files"]["world.png"]} or
                extension["candle_reference"] != candle):
            raise ValueError("clearing asset/base reference mismatch")
        for name,digest in extension["files"].items():
            source=ROOT/CLEARING_DIRECTORY/name
            if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != digest:
                raise ValueError(f"clearing asset hash mismatch: {name}")
        path = CLEARING_DIRECTORY/"cleared-world.png"
        mask_path = CLEARING_DIRECTORY/"sky-mask.png"
        mask_fact = extension["sky_mask"]
        if (mask_fact["path"] != mask_path.as_posix() or
                mask_fact["sha256"] != extension["files"][mask_path.name] or
                mask_fact["source_world"] != extension["world_reference"]):
            raise ValueError("sky mask source binding mismatch")
        with Image.open(ROOT/mask_path) as mask:
            if mask.mode != "L" or list(mask.size) != [940,1672] or list(mask.size) != mask_fact["native_size"]:
                raise ValueError("sky mask must be the registered native alpha")
            sky = np.asarray(mask.resize((w,h),Image.Resampling.LANCZOS),np.float32)/255
        layout.update(mode=CLEARING_RENDERER_ID, cleared_world=load(path),
                      sky_mask=sky)
        layout["cloud_arrival"], layout["cloud_rim"] = _cloud_recession(
            layout["world"], sky, seed)
        layout["provenance"] = {"bundle_id": CLEARING_RENDERER_ID,
            "manifest_path": (CLEARING_DIRECTORY/"manifest.json").as_posix(),
            "manifest_sha256": hashlib.sha256(extension_raw).hexdigest(),
            "base_bundle": layout["provenance"],
            "files": {(CLEARING_DIRECTORY/name).as_posix():digest for name,digest in extension["files"].items()},
            "content_role": "sky-only",
            "sky_mask": mask_fact}
    return layout


def _soft_flame(canvas, wx, wy, height, lean):
    """Add a continuous luminous density field; no opaque nested polygons."""
    base = np.asarray(canvas.convert("RGB"), np.float32).copy()
    h, w = base.shape[:2]
    top, bottom = max(0,int(wy-1.20*height)), min(h,int(wy+.18*height)+2)
    left, right = max(0,int(wx-.65*height-abs(lean))), min(w,int(wx+.65*height+abs(lean))+2)
    yy, xx = np.mgrid[top:bottom,left:right]
    up = (wy-yy)/max(height,1)
    center = wx+lean*np.clip(up,0,1)**1.5
    width = max(1.,height*.17)*(1-.73*np.clip(up,0,1))
    radial = ((xx-center)/width)**2
    lower = np.clip((up+.11)/.17,0,1); lower=lower*lower*(3-2*lower)
    tip = np.clip((1.1-up)/.22,0,1);tip=tip*tip*(3-2*tip)
    density = np.exp(-radial*.8)*lower*tip
    core = np.exp(-radial*2.2)*lower*tip*np.clip(1.2-up*.5,0,1)
    glow = np.exp(-radial*.18-((up-.35)/.75)**2)*.24
    light = density[...,None]*np.array([115,55,5])+core[...,None]*np.array([110,110,60])+glow[...,None]*np.array([55,23,3])
    base[top:bottom,left:right] += light
    return Image.fromarray(np.clip(base,0,255).astype(np.uint8))


def clearing_state(f, ctrl, asset):
    """Source-cloud recession and storm fade, strictly gated by activation."""
    if asset.get("mode") != CLEARING_RENDERER_ID:
        return None, 0.
    cue = int(ctrl["score_clearing_frame"])
    if f < cue:
        return np.zeros_like(asset["sky_mask"]), 0.
    duration = min(CLEARING_FRAMES,max(1,len(ctrl["scene_id"])-cue-1))
    u = float(np.clip((f-cue)/duration,0,1))
    if u >= 1:
        return asset["sky_mask"].copy(), 1.
    # The first small fissures must survive encoding at the actual cue.
    # Continue from them to the unchanged endpoint, without anticipating it.
    threshold = u+.2*(1-u)**2
    opacity = _cloud_opacity(asset["cloud_arrival"],asset["cloud_rim"],threshold)
    return (1-opacity)*asset["sky_mask"], _smooth(u)


def _candle(f, ctrl, w, h, asset, major, octave, params):
    env = float(ctrl["flame_envelope"][min(f,len(ctrl["flame_envelope"])-1)])
    entered = _smooth((f-major)/18) if f >= major else 0.
    strength = .025 + entered*(.16+.84*env)*float(params.get("candle_strength",1))
    if asset.get("mode") == CLEARING_RENDERER_ID:
        strength = .025 + entered*(.22+.78*env)*float(params.get("candle_strength",1))
    flutter = .92+.045*np.sin(f*.19+asset["phase"])+.035*np.sin(f*.317)
    height = h*(.011+.125*strength)*flutter
    wx,wy = asset["wick"]
    x,y = asset["x"],asset["y"]
    halo = np.exp(-((x-wx)/(.035+.13*strength))**2-((y-wy+.035)/(.035+.12*strength))**2)
    cast = np.exp(-((x-wx)/(.22+.13*strength))**2-((y-.57)/.42)**2)
    pulse = float(ctrl["accent_pulse"][f])
    img = asset["candle"]*(.78+.12*strength)
    img += halo[...,None]*np.array([80,42,8])*(.45+strength)
    img += cast[...,None]*np.array([39,20,5])*(strength+.1*pulse)
    canvas = Image.fromarray(np.clip(img,0,255).astype(np.uint8))
    if asset.get("mode") == CLEARING_RENDERER_ID:
        return _soft_flame(canvas,wx*w,wy*h,height,w*.006*np.sin(f*.09)*(1-.5*strength))
    return _flame(canvas,wx*w,wy*h,height,w*.006*np.sin(f*.09)*(1-.5*strength),w)


def _world(f, ctrl, w, h, asset, fast):
    elapsed = max(0,f-fast)
    u = min(1., elapsed/max(1,len(ctrl["scene_id"])-fast-1))
    travel = .65*u+.35*_smooth(u)
    launch = _smooth(elapsed/18)
    # Depth-varying mesh: near cliffs pass faster than the distant route.
    reveal, cleared = clearing_state(f, ctrl, asset)
    source_array = asset["world"]
    if reveal is not None:
        source_array = source_array*(1-reveal[...,None])+asset["cleared_world"]*reveal[...,None]
    source = Image.fromarray(np.clip(source_array,0,255).astype(np.uint8))
    ax,ay = w*.5,h*.385
    def point(x,y):
        depth = _smooth((y/h-.40)/.60)
        zoom = 1+.06*launch+.30*travel*(.24+.76*depth)
        sway = w*.011*np.sin(elapsed/190)*depth*launch
        return ax+(x-ax)/zoom+sway, ay+(y-ay)/zoom
    mesh=[]
    edges=np.linspace(0,h,17).astype(int)
    for top,bottom in zip(edges[:-1],edges[1:]):
        quad=[*point(0,top),*point(0,bottom),*point(w,bottom),*point(w,top)]
        mesh.append(((0,int(top),w,int(bottom)),quad))
    world = source.transform((w,h),Image.Transform.MESH,mesh,Image.Resampling.BICUBIC)
    # Clouds move independently of the rock. A feathered mask avoids a seam.
    drift = float(np.tanh(elapsed/450))
    cloud_shift = w*.06*drift
    # Overscan grows with the translation. Its source margins exceed the
    # .06*w*drift shift, so no unfilled pixels enter the visible sky.
    inverse = 1/(1+.16*drift)
    world_sky = None
    if reveal is None:
        cloud = world.transform((w,h),Image.Transform.AFFINE,
            (inverse,0,w*(1-inverse)/2+cloud_shift,0,inverse,h*(1-inverse)/2),
            Image.Resampling.BICUBIC)
        alpha = np.clip((.56-asset["y"])/.12,0,1)
        img = np.asarray(world,np.float32)*(1-alpha[...,None])+np.asarray(cloud,np.float32)*alpha[...,None]
    else:
        # Never affine-copy a rock-bearing frame into the sky. The clearing
        # asset is sky only; rocks/lava take exactly the original mesh once.
        mask = Image.fromarray(np.round(asset["sky_mask"]*255).astype(np.uint8))
        world_sky_image = mask.transform((w,h),Image.Transform.MESH,mesh,Image.Resampling.BICUBIC)
        world_sky = np.asarray(world_sky_image,np.float32)/255
        img = np.asarray(world,np.float32).copy()
    energy = float(ctrl["flame_envelope"][f])
    molten = np.clip((asset["y"]-.38)/.5,0,1)
    img += molten[...,None]*np.array([9,3,0])*(.3+.7*energy)
    acc = float(ctrl["accent_pulse"][f])*(1-cleared)
    sky = np.clip((.58-asset["y"])/.18,0,1)
    if world_sky is not None:
        sky = world_sky
    img += sky[...,None]*np.array([57,66,79])*.65*acc
    canvas = Image.fromarray(np.clip(img,0,255).astype(np.uint8)).convert("RGBA")
    overlay = Image.new("RGBA",(w,h),(0,0,0,0));draw=ImageDraw.Draw(overlay)
    if acc > .02:
        start = max(0,f-15)
        event = start+int(np.argmax(ctrl["accent_pulse"][start:f+1]))
        rng = np.random.RandomState(event+173)
        px = float(rng.uniform(.3,.72))*w
        pts=[(px,h*.04)]
        for i in range(1,9):pts.append((px+float(rng.uniform(-.075,.075))*w,h*(.04+i*.038)))
        draw.line(pts,fill=(192,213,239,int(210*acc)),width=max(1,round(w/270)))
        for i in (3,5):
            bx,by=pts[i]
            draw.line([(bx,by),(bx+w*.10,by+h*.035),(bx+w*.14,by+h*.085)],
                      fill=(175,194,222,int(140*acc)),width=max(1,round(w/540)))
    # Wind-driven ash belongs to the requested burning storm, not every note.
    for x,y,s,v in asset["embers"]:
        ex=((x+elapsed*(.00035+.0003*v))%1.15-.075)*w
        ey=((y-elapsed*(.0005+.0003*s))%1.15-.075)*h
        draw.line([(ex,ey),(ex-3*w/540,ey+5*h/960)],fill=(238,122,30,int((45+int(35*s))*(1-cleared))),width=1)
    return Image.alpha_composite(canvas,overlay).convert("RGB")


def _stone_fragment(room, shard, progress, w):
    """Turn a textured masonry front, its shaded side and its stone back."""
    tile=room.crop(shard["box"]); mask=shard["mask"]
    yaw=shard["yaw"]*progress
    cosine=float(np.cos(yaw)); face_width=max(2,round(tile.width*abs(cosine)))
    if cosine < 0:
        # Original masonry luminance supplies texture on the unpainted back.
        gray=np.asarray(tile.convert("L"),np.float32)
        rock=np.clip(gray[...,None]*np.array([.30,.29,.27])+np.array([27,25,23]),0,255)
        tile=Image.fromarray(rock.astype(np.uint8)).convert("RGBA")
    else:
        arr=np.asarray(tile.convert("RGB"),np.float32)*(.48+.52*abs(cosine))
        tile=Image.fromarray(np.clip(arr,0,255).astype(np.uint8)).convert("RGBA")
    tile.putalpha(mask)
    tile=tile.resize((face_width,tile.height),Image.Resampling.BICUBIC)
    thickness=round(shard["thickness"]*abs(np.sin(yaw)))
    shell=Image.new("RGBA",(face_width+thickness+2,tile.height+thickness+2))
    side=Image.new("RGBA",tile.size,(54,47,39,255));side.putalpha(tile.getchannel("A"))
    direction=1 if yaw>0 else -1
    front_x=1 if direction>0 else thickness+1
    for d in range(thickness,0,-1):
        shell.alpha_composite(side,(front_x+direction*d,1+d//2))
    shell.alpha_composite(tile,(front_x,1))
    return shell.rotate(shard["rotation"]*progress,Image.Resampling.BICUBIC,expand=True)


def draw_frame(f, ctrl, w, h, seed, lay, scene_palettes, scene_params):
    from . import rendering as R
    asset=lay["candle_rift"]
    times=event_frames(ctrl);major=times["major_arrival"];octave=times["both_hands_octaves"];fast=times["fast_coda"]
    if not 0 < major < octave < fast < len(ctrl["scene_id"]):
        raise ValueError("rift requires ordered score landmarks inside the clip")
    if asset.get("mode") == CLEARING_RENDERER_ID:
        # Candidate/context intake enforces the real sixty-frame visual span.
        # Counterfactual QA must also render +/-2-frame hypotheses at that
        # eligibility edge without changing the actual candidate contract.
        if "final_scale_arrival" not in times or not fast < times["final_scale_arrival"] < len(ctrl["scene_id"]):
            raise ValueError("clearing requires its ordered final-scale cue inside the clip")
    params = _state(f, ctrl, [{"candle_strength": 1., **p}
                             for p in (scene_params or [{}])])
    if f < octave:
        canvas=_candle(f,ctrl,w,h,asset,major,octave,params)
    else:
        world=_world(f,ctrl,w,h,asset,fast)
        p=float(np.clip((f-octave)/max(1,fast-octave),0,1))
        if p >= 1:
            canvas=world
        else:
            room=_candle(octave,ctrl,w,h,asset,major,octave,params).convert("RGBA")
            canvas=world.convert("RGBA")
            for shard in asset["shards"]:
                t=float(np.clip((p-shard["delay"])/(1-shard["delay"]),0,1))
                x0,y0,x1,y1=shard["box"]
                if t<=0:
                    tile=room.crop(shard["box"]);tile.putalpha(shard["mask"])
                    canvas.alpha_composite(tile,(x0,y0));continue
                tile=_stone_fragment(room,shard,t,w)
                dx=round(w*shard["vx"]*t)
                dy=round(h*(1.8*t*t+.12*t)*shard["depth"])
                cx=(x0+x1)/2+dx;cy=(y0+y1)/2+dy
                # alpha_composite handles clipped falling fragments.
                canvas.alpha_composite(tile,(round(cx-tile.width/2),round(cy-tile.height/2)))
            canvas=canvas.convert("RGB")
    framed=R.apply_camera(np.asarray(canvas),*R.camera_for_frame(f,ctrl,scene_params))
    yy,cb,cr=Image.fromarray(framed).convert("YCbCr").split()
    return np.asarray(Image.merge("YCbCr",(yy.filter(ImageFilter.GaussianBlur(.45)),
                      cb.filter(ImageFilter.GaussianBlur(1.4)),cr.filter(ImageFilter.GaussianBlur(1.4)))).convert("RGB"))

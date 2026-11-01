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

from .nocturne_chapel import _flame, _state

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = Path("assets/visual/candle-rift-v1")
RENDERER_ID = "candle-rift-v1"


def _smooth(x):
    x = float(np.clip(x, 0, 1))
    return x*x*(3-2*x)


def _boundary(ctrl, stage):
    ids = np.asarray(ctrl["scene_id"])
    found = np.flatnonzero(ids >= stage)
    return int(found[0]) if found.size else len(ids)


def event_frames(ctrl):
    """Scene counterfactuals move the real flame/collapse, not a QA marker."""
    return {"major_arrival": _boundary(ctrl, 1),
            "both_hands_octaves": _boundary(ctrl, 2),
            "fast_coda": int(ctrl["score_fast_frame"])}


def make_layout(seed, w, h):
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
    return {"candle":load(Path(candle["path"])),"world":load(DIRECTORY/"world.png"),
            "wick":tuple(candle["wick_anchor"]),"shards":shards,
            "phase":float(rng.uniform(0,6.28)),
            "x":(np.arange(w,dtype=np.float32)[None,:]+.5)/w,
            "y":(np.arange(h,dtype=np.float32)[:,None]+.5)/h,
            "embers":rng.uniform(0,1,(42,4)),
            "provenance":{"bundle_id":RENDERER_ID,
                "manifest_path":(DIRECTORY/"manifest.json").as_posix(),
                "manifest_sha256":hashlib.sha256(raw).hexdigest(),
                "files":{p.as_posix():digest for p,digest in sources.items()}}}


def _candle(f, ctrl, w, h, asset, major, octave, params):
    env = float(ctrl["flame_envelope"][min(f,len(ctrl["flame_envelope"])-1)])
    entered = _smooth((f-major)/18) if f >= major else 0.
    strength = .025 + entered*(.16+.84*env)*float(params.get("candle_strength",1))
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
    return _flame(canvas,wx*w,wy*h,height,w*.006*np.sin(f*.09)*(1-.5*strength),w)


def _world(f, ctrl, w, h, asset, fast):
    elapsed = max(0,f-fast)
    u = min(1., elapsed/max(1,len(ctrl["scene_id"])-fast-1))
    travel = .65*u+.35*_smooth(u)
    launch = _smooth(elapsed/18)
    # Depth-varying mesh: near cliffs pass faster than the distant route.
    source = Image.fromarray(np.clip(asset["world"],0,255).astype(np.uint8))
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
    cloud_shift = w*.06*np.tanh(elapsed/450)
    cloud = world.transform((w,h),Image.Transform.AFFINE,(1,0,cloud_shift,0,1,0),Image.Resampling.BICUBIC)
    alpha = np.clip((.56-asset["y"])/.12,0,1)
    img = np.asarray(world,np.float32)*(1-alpha[...,None])+np.asarray(cloud,np.float32)*alpha[...,None]
    energy = float(ctrl["flame_envelope"][f])
    molten = np.clip((asset["y"]-.38)/.5,0,1)
    img += molten[...,None]*np.array([9,3,0])*(.3+.7*energy)
    acc = float(ctrl["accent_pulse"][f])
    sky = np.clip((.58-asset["y"])/.18,0,1)
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
        draw.line([(ex,ey),(ex-3*w/540,ey+5*h/960)],fill=(238,122,30,45+int(35*s)),width=1)
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

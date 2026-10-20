"""Timeline visualization: PNG (matplotlib) + offline HTML (local video + canvas).

timeline.html embeds downsampled curves / cuts / accents as inline JSON and
draws them on a <canvas> with plain script (no CDN, no fetch, no external
communication). One local <video> element with a structured/naive mode
toggle sits next to the canvas (video capped at 65vh so curves stay
visible); a playhead synced by requestAnimationFrame shows the exact
clip-local time, source time and frame. Clicking the canvas seeks the
video to the EXACT clicked position (no hidden offset: this is a
diagnostic UI, and any seek bias would blur the picture/sound check).
"""
from __future__ import annotations

import base64
import html
import json

import numpy as np


def write_timeline_png(path: str, feat: dict, events: dict, plan: dict,
                       clip_sel: dict, sr: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    t = np.asarray(feat["centers"], dtype=np.float64) / sr
    # Novelty curve shares the midpoint time coordinate with novelty events
    # (t-lag/2); volume/attack curves keep the right-edge i*hop coordinate.
    t_nov = (np.asarray(feat["novelty_centers"], dtype=np.float64) / sr
             if "novelty_centers" in feat else t)
    s0, s1 = plan["clip"]["start_sample"], plan["clip"]["end_sample"]
    fig, axes = plt.subplots(4, 1, figsize=(12, 8), sharex=True)
    axes[0].plot(t, feat["rms_n"], lw=0.6, label="rms_n")
    axes[0].plot(t, feat["slow_n"], lw=0.8, label="slow_n")
    axes[0].legend(loc="upper right", fontsize=8)
    axes[1].plot(t, feat["flux_n"], lw=0.6, color="tab:orange", label="flux_n")
    for e in events.get("accent", []):
        axes[1].axvline(e["time_sec"], color="red", alpha=0.25, lw=0.7)
    axes[1].legend(loc="upper right", fontsize=8)
    axes[2].plot(t_nov, feat["nov_n"], lw=0.6, color="tab:green", label="chroma_novelty_n")
    for e in events.get("struct_boundary", []):
        axes[2].axvline(e["time_sec"], color="purple", alpha=0.5, lw=1.0)
    axes[2].legend(loc="upper right", fontsize=8)
    axes[3].plot(t, feat["slow_n"], lw=0.6, color="gray")
    for e in events.get("dip", []):
        axes[3].axvline(e["time_sec"], color="blue", alpha=0.5, lw=1.0)
    for ax in axes:
        ax.axvspan(s0 / sr, s1 / sr, color="gold", alpha=0.18)
    for sc in plan["scenes"]:
        for ax in axes:
            ax.axvline(sc["start_sample"] / sr, color="black", lw=1.4)
    # explicit clip END boundary (not just scene starts)
    for ax in axes:
        ax.axvline(s1 / sr, color="red", lw=1.4, ls="--", label="clip end")
    for ac in plan.get("accents", []):
        axes[1].axvline(ac["time_sec"], color="red", lw=1.4, alpha=0.9)
    axes[3].set_xlabel("time (s, display only; ref = source sample)")
    axes[0].set_title("P0 timeline: features (normalized) + clip + cuts + accents")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)


def write_timeline_html(path: str, videos: dict, png_path: str, feat: dict,
                        events: dict, plans: dict, sr: int) -> None:
    """videos: {"structured": "poc_structured.mp4", "naive": ...}.

    plans: {"structured": plan_dict, "naive": plan_naive_dict}.
    """
    n = 500
    idx = np.linspace(0, len(feat["slow_n"]) - 1, n).astype(int)
    clip = plans["structured"]["clip"]
    clip0 = clip["start_sample"] / sr
    clip1 = clip["end_sample"] / sr
    fps = clip.get("fps", [30, 1])
    fps_f = fps[0] / fps[1]
    modes = {}
    for mode, plan in plans.items():
        modes[mode] = {
            "video": videos[mode],
            "cuts": [c / sr for c in plan.get("cuts_planned", [])],
            "accents": [a["time_sec"] for a in plan.get("accents", [])],
            "accents_strength": [a["strength"] for a in plan.get("accents", [])],
            "accents_frame": [a["frame"] for a in plan.get("accents", [])],
            "scenes": [[s["start_frame"], s["end_frame"]] for s in plan["scenes"]],
        }
    nov_centers = (np.asarray(feat["novelty_centers"]) if "novelty_centers" in feat
                   else np.asarray(feat["centers"]))
    data = {
        "sr": sr,
        "fps": fps_f,
        "t": [round(float(feat["centers"][i]) / sr, 3) for i in idx],
        "t_nov": [round(float(nov_centers[i]) / sr, 3) for i in idx],
        "slow": [round(float(feat["slow_n"][i]), 3) for i in idx],
        "flux": [round(float(feat["flux_n"][i]), 3) for i in idx],
        "nov": [round(float(feat["nov_n"][i]), 3) for i in idx],
        "clip": [clip0, clip1],
        "modes": modes,
    }
    png_b64 = base64.b64encode(open(png_path, "rb").read()).decode()
    accent_rows = "".join(
        f"<tr><td>{a['time_sec'] - clip0:.2f}</td><td>{a['frame']}</td>"
        f"<td>{a['strength']}</td></tr>"
        for a in plans["structured"].get("accents", []))
    doc = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>P0 timeline QA (offline)</title>
<style>
body{font-family:sans-serif;max-width:960px;margin:1em auto;padding:0 8px}
.stage{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap}
.stage video{max-height:65vh;width:auto;max-width:100%;background:#000}
.stage .side{flex:1;min-width:220px;font-size:13px}
canvas#c{width:100%;max-width:100%;height:auto;display:block;border:1px solid #999;cursor:crosshair}
#info{font-variant-numeric:tabular-nums;font-size:14px;margin:6px 0}
button{margin:2px 4px 2px 0}
table{border-collapse:collapse;font-size:13px}td,th{border:1px solid #bbb;padding:2px 6px}
.note{font-size:12px;color:#444}
</style>
</head><body>
<h2>P0 timeline QA &mdash; offline diagnostic UI (no network)</h2>
<div class="mode">
<button id="bS">structured</button><button id="bN">naive</button>
<span id="modeLabel"></span>
</div>
<div class="stage">
<video id="v" controls preload="auto"></video>
<div class="side">
<div id="info">no time yet</div>
<p class="note">Gold band = selected clip on the source timeline. Black ticks =
scene cuts, red ticks = accent onsets (structured mode). Click the curve to
seek the video to that EXACT position (no offset correction).</p>
</div>
</div>
<canvas id="c" width="880" height="300"></canvas>
<h3>Static overview (timeline.png)</h3>
<img src="data:image/png;base64,PNG_B64" style="width:100%">
<h3>Accents (structured)</h3>
<table><tr><th>clip-local s</th><th>frame</th><th>strength</th></tr>
ACCENT_ROWS</table>
<script>
"use strict";
const D = DATA_JSON;
const cv = document.getElementById("c"), ctx = cv.getContext("2d");
const V = document.getElementById("v");
const info = document.getElementById("info");
const modeLabel = document.getElementById("modeLabel");
let mode = "structured";
const clip0 = D.clip[0], clip1 = D.clip[1];
function X(t) { return (t - clip0) / (clip1 - clip0) * cv.width; }
function T(x) { return clip0 + x / cv.width * (clip1 - clip0); }
function draw(now) {
  ctx.clearRect(0, 0, cv.width, cv.height);
  const rows = [["slow", "#333", "t"], ["flux", "#e60", "t"], ["nov", "#090", "t_nov"]];
  for (let r = 0; r < rows.length; r++) {
    const k = rows[r][0], col = rows[r][1], tk = rows[r][2], y0 = 20 + r * 90;
    const tt = D[tk] || D.t;
    ctx.strokeStyle = col; ctx.beginPath();
    for (let i = 0; i < D[k].length; i++) {
      const x = (tt[i] - clip0) / (clip1 - clip0) * cv.width;
      const y = y0 + 70 * (1 - D[k][i]);
      if (i) { ctx.lineTo(x, y); } else { ctx.moveTo(x, y); }
    }
    ctx.stroke(); ctx.fillStyle = "#333"; ctx.fillText(k, 4, y0 + 10);
  }
  const M = D.modes[mode];
  ctx.fillStyle = "rgba(200,0,0,.7)";
  for (const t of M.accents) { ctx.fillRect(X(t) - 1, 0, 2, cv.height); }
  ctx.fillStyle = "#000";
  for (const t of M.cuts) {
    if (t > clip0 && t < clip1) { ctx.fillRect(X(t) - 1, 0, 2, cv.height); }
  }
  // playhead: exact current video position
  if (!isNaN(V.currentTime)) {
    const t = clip0 + V.currentTime;
    ctx.fillStyle = "rgba(0,90,200,.9)";
    ctx.fillRect(X(t) - 1, 0, 2, cv.height);
    const fr = Math.floor(V.currentTime * D.fps);
    info.textContent = "local " + V.currentTime.toFixed(3) + "s = source " +
      t.toFixed(3) + "s = frame " + fr + " [" + mode + "]";
  }
  requestAnimationFrame(draw);
}
cv.addEventListener("click", function (ev) {
  const r = cv.getBoundingClientRect();
  const local = (ev.clientX - r.left) / r.width * (clip1 - clip0);
  V.currentTime = Math.max(0, Math.min(local, clip1 - clip0));
});
function setMode(m) {
  mode = m;
  V.src = D.modes[m].video;
  modeLabel.textContent = "showing: " + m + " (curves are mode-independent features)";
}
document.getElementById("bS").addEventListener("click", function () { setMode("structured"); });
document.getElementById("bN").addEventListener("click", function () { setMode("naive"); });
setMode("structured");
requestAnimationFrame(draw);
</script></body></html>"""
    doc = doc.replace("PNG_B64", png_b64)
    doc = doc.replace("ACCENT_ROWS", accent_rows)
    doc = doc.replace("DATA_JSON", json.dumps(data))
    open(path, "w").write(doc)

# Studio evidence review (Codex, actual rendered evidence)

You receive labelled sections, in order:
1. SOURCE BRIEF (`brief.json`: owner creative fields verbatim +
   planner candidate brief + deterministic candidate/scenes)
2. STORYBOARD (`storyboard.json` authored by the production Codex session)
3. PRODUCTION FILES (committed input names/paths with sha256 digests,
   plus the expected head SHA; v2 uses a direct candidate PR, legacy
   uses `production/<child>/`)
4. PLAN + QA (`plan.json`, `qa.json`: encoded-media QA on the real MP4)
5. HASHES (`hashes.json` manifest over the artifact bundle)
plus the contact-sheet image(s) via --image.

Judge the ACTUAL final MP4 evidence (contact sheet sampled from the
encoded video: START/MIDDLE/END of every deterministic scene, one row
per scene), not the storyboard prose. Judge separately:
(1) mechanical QA reviewed, (2) brief adherence (does the storyboard
implement the labelled brief, scene by scene, within what the renderer
can actually execute?), (3) timing integrity (deterministic scene IDs,
no invented timing), (4) aesthetic approval of the actual rendered
evidence. A `pass` verdict requires verdict pass, ALL FOUR checks true
and zero blockers. Do NOT lower standards: placeholder aesthetics may
and should fail aesthetic approval; reject frames that are still
visibly placeholder if that is what the evidence shows. Do not declare
the result polished merely because no labels, captions or QA markers
are visible. Intentional simplicity can succeed: do not demand a character,
many cuts, elaborate acting or extra particles solely to make it busier.
Distinguish an observable defect or unfulfilled brief from a personal colour
preference. State the visible evidence and its effect on the viewing
experience for every blocker. Do not assume musical listening: audio listening is
unavailable, this is not a full musical judgement, and you must never
claim the excerpt is musically proven. Set `listening_note` to exactly
`audio listening unavailable; not a full musical judgement`.

The prototype notice (`prototype_notice` / provenance `prototype`) is
metadata only. It is NOT a required visual overlay: never fail or pass
a review because a text disclaimer is or is not drawn into the frames.

## What the renderer can actually execute (capability truth)

The optional top-level `asset_bundle` selects a registered, hash-checked
background painting and transparent traveler stored in the repository.
It does not generate images during rendering. The same artwork persists
across scenes; palette values drive spatial colour grading and light,
preserving painted detail rather than replacing regions with flat RGB.
The public plan and provenance record the asset manifest and file hashes.
Without an asset bundle, the original procedural renderer is used.
Judge both methods by the same visible-quality standard; using generated
artwork grants no automatic aesthetic approval.

The trusted renderer consumes ONLY, per deterministic scene:

- the six palette colours: `sky_top`, `sky_bot`, `far`, `near`, `moon`,
  `glow` (RGB triples); and
- ten bounded numeric params: `figure_scale` (0.5-2.0),
  `particle_multiplier` (0.0-2.0), `light_bias` (-0.3-0.3),
  `glow_scale` (0.5-2.0), and the optional executable camera
  `camera_zoom_start`/`camera_zoom_end` (1.0-1.18, default 1.0) and
  `camera_pan_x_start`/`camera_pan_x_end`/`camera_pan_y_start`/
  `camera_pan_y_end` (-1.0-1.0, default 0.0). The camera
  crops/resamples the ENTIRE frame (landscape, moon and figure
  together), eased with smoothstep over the scene's inclusive frame
  range [start_frame, end_frame-1]; pan is a fraction of the
  available crop margin (no-op at zoom 1.0) and the crop never leaves
  the frame, so no black edges can appear. Omitting the camera keys
  means zoom 1.0 / pan 0.0. Judge framing from the contact sheet's
  start/middle/end row for each scene against these numeric values.

The renderer automatically joins palettes, the four non-camera numeric
controls, and the camera trajectories around each unchanged DSP scene
boundary. The join uses smoothstep over at most 24 frames on each side
(0.8 seconds per side at 30fps), capped to a quarter of each adjacent
scene. The boundary is the 50% blend point. Start/end samples beside a
boundary therefore contain a blend; scene interiors show the intended
palette. These are renderer-derived transitions, not new LLM timings.
Judge their actual continuity and brief adherence without lowering the
aesthetic standard.

`motif` and `camera` storyboard fields are descriptive prose only:
they are NOT executed and changing them alone changes no pixels. Scene
boundaries, clip window and accents are deterministic DSP timings and
must never be treated as storyboard-editable.

## Repair routing (`repair` object, REQUIRED)

Always return `repair` with exactly `route` and `instructions`:

- `pass`: `route` MUST be `none` and `instructions` MUST be empty.
- `fail`: `route` MUST be `storyboard` or `engine`, and
  `instructions` MUST be a non-empty list (max 20, each max 1000 chars)
  of concrete, actionable changes, each naming what to change and how
  to verify it in the re-rendered evidence (contact sheet / QA).

Choose `storyboard` ONLY if EVERY blocker can be resolved using the
executed controls above: per-scene palette colours and/or the ten
bounded numeric params (including numeric camera zoom/pan fixes --
an inward, wider or settling framing that the numeric camera can
express is a storyboard fix), within their existing ranges, at the
existing deterministic scene IDs. Write instructions in those terms
(scene ID, palette key or param, direction of change, verification),
never as requests for new timing, new scenes or prose-only edits
presented as visual fixes.

Choose `engine` if ANY blocker needs a missing composition, asset or
rendering capability (new imagery, figures, animation, composition or
effects the palette/params cannot produce), or if the blocker is a
timing, synchronisation or verification engineering defect. Engine
instructions must state what a code agent should implement or fix and
what test/evidence should verify it; do not pretend a storyboard edit
can fix it.

All blockers must be listed in `blockers`, and every blocker must be
covered by the repair instructions: blockers and instructions are
preserved verbatim into failure feedback and repair workspaces, so
write them as complete standalone statements, not references to this
prompt.

Set `reviewed_head_sha` to the expected head SHA given in section 3
only after verifying the digests belong to that head.

Return JSON matching the review schema only.

# Studio visual preflight review (Codex, actual evidence frames)

You receive, in order:

1. The provided brief (creative intent for this work).
2. The storyboard as implemented.
3. The evidence stage (`still` or `motion`) and the evidence digest
   (`evidence_sha256`, the SHA-256 of the exact `evidence.json`
   bytes) that you must bind in your review.
4. The actual attached images: native frames
   (`frame-NNNNNN.png`, indexed by the original source frame local
   to the clip) and the 3-column contact sheet (one row per scene:
   start / middle / end). For motion evidence you also receive the
   decoded start/mid/end frames of each short preview window.

Inspect the ACTUAL attached images against the provided brief.
Judge composition, shape, readability and style consistency, and
list actionable visible defects against the brief. Do not fail a
review solely because the current drawing technique is procedural,
and do not fail it solely because no named art model was used: judge
the visible evidence, not the production technique. Do not invent
any work identity (issues, PRs, commits, runs): bind only the
stage and evidence digest given above. Never claim listening: set
`listening_note` to exactly
`audio listening unavailable; not a full musical judgement`.
Prefer `false` for any applicable check whose evidence is
unavailable or insufficient.

## What a pass means here

A still pass is only a preflight pass, not final approval. A motion
pass judges only the sparse decoded samples presented: sparse
motion samples cannot certify full continuous movement or musical
taste, so never claim they do. This review is not the full
mechanical QA review and carries no PR head: do not claim the full
encoded video, mechanical QA, timing or musical quality passed.

Use the same visible-quality standard as the final aesthetic review,
within this limited evidence scope. Visibly unfinished placeholder
shapes, weak depth, unreadable figures or distracting compositional
defects must fail here with specific descriptions of the actual pixels.
Do not postpone a visible defect to the final review just because this
is a preflight. Procedural technique alone remains no reason to fail.

For stage `still`, `checks.motion_quality` set to `true`
explicitly means not-applicable (no motion was presented); it is
never a motion judgement. For stage `motion`, set it from the
sampled movement only.

## What the renderer can actually execute (capability truth)

The current drawing remains procedural. The only executable
controls are, per deterministic scene:

- the six palette colours: `sky_top`, `sky_bot`, `far`, `near`,
  `moon`, `glow`; and
- ten bounded numeric params: `figure_scale`, `particle_multiplier`,
  `light_bias`, `glow_scale`, and the numeric camera
  `camera_zoom_start`/`camera_zoom_end`,
  `camera_pan_x_start`/`camera_pan_x_end`,
  `camera_pan_y_start`/`camera_pan_y_end`.

The renderer joins palettes, the four non-camera controls and camera
trajectories with smoothstep around existing DSP boundaries: at most
24 frames (0.8s) per side, capped to a quarter of each adjacent scene.
Each unchanged boundary is the 50% blend point. Adjacent scene end/start
samples therefore contain a blend; the interiors show the scene palette.
Judge the actual result with the same aesthetic standard.

`motif` and `camera` prose fields are metadata only: they are not
executed, and changing prose alone changes no pixels. Scene
boundaries, clip window and accents are deterministic timings and
are never storyboard-editable.

## Repair routing (`repair` object, REQUIRED)

Always return `repair` with exactly `route` and `instructions`:

- `pass`: `route` MUST be `none` and `instructions` MUST be empty.
  A pass also requires all four checks true and zero blockers.
- `fail`: `route` MUST be `storyboard` or `engine`, `blockers` MUST
  be non-empty, and `instructions` MUST be a non-empty list (max
  20, each 1..1000 chars). Preserve the full previous blocker
  detail: list every blocker in full as a standalone statement and
  cover every blocker in the instructions, so the text can be
  carried verbatim into repair work.

Choose `storyboard` only when every blocker can be fixed with the
executable controls above: palette colours and/or the ten numeric
params (including numeric camera zoom/pan), within their existing
ranges at the existing scene IDs. Write instructions in those
terms (scene ID, palette key or param, direction, verification in
the re-rendered evidence).

Choose `engine` when any blocker needs missing art, asset or
drawing/rendering capability (new imagery, figures, composition or
effects the palette/params cannot produce), or an engineering
defect. State what a code agent should implement and what
test/evidence should verify it; never present a storyboard edit as
able to fix an engine blocker, and never present prose-only
`motif`/`camera` edits as visual fixes.

Return JSON matching the visual-review schema only.

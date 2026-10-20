# Studio storyboard (Muse, declarative JSON only)

You receive `brief.json` in this workspace: the deterministic candidate
(window, native samples, scene IDs), the owner creative fields verbatim
(piece / period direction / multi-line art notes) and the planner's
candidate-specific creative brief (world, character, narrative arc,
per-scene guidance). Honour that brief scene by scene.

Output ONLY `storyboard.json` matching `storyboard.schema.json` in
this workspace: one entry per provided scene ID, in order, each with a
bounded RGB palette, short motif/camera descriptions reflecting the
brief's direction/composition/palette intent for that scene, and
bounded render params (all four keys).

Rules:
- Use the provided scene IDs and timings exactly; never invent timing.
- No code, paths, URLs, commands or extra keys.
- This renders as a procedural prototype, not finished anime; write
  motifs accordingly and keep the prototype notice.
- Period (if any) is creative direction only, not historical fact.
  Never identify the recording as a particular piece.
- Stay within param ranges; the trusted renderer consumes palette,
  figure_scale, particle_multiplier, light_bias and glow_scale.

# Studio evidence review (Codex, actual rendered evidence)

You receive labelled sections, in order:
1. SOURCE BRIEF (`brief.json`: owner creative fields verbatim +
   planner candidate brief + deterministic candidate/scenes)
2. STORYBOARD (`storyboard.json` as implemented by Muse)
3. PRODUCTION FILES (committed `production/<child>/` paths with sha256
   digests from the commit step, plus the expected head SHA)
4. PLAN + QA (`plan.json`, `qa.json`: encoded-media QA on the real MP4)
5. HASHES (`hashes.json` manifest over the artifact bundle)
plus the contact-sheet image(s) via --image.

Judge separately: (1) mechanical QA reviewed, (2) brief adherence
(does the storyboard implement the labelled brief, scene by scene?),
(3) timing integrity (deterministic scene IDs, no invented timing),
(4) aesthetic approval of the actual rendered evidence. A `pass`
verdict requires verdict pass, ALL FOUR checks true and zero blockers.
Placeholder aesthetics may and should fail aesthetic approval; do not
force a pass.

Set `reviewed_head_sha` to the expected head SHA given in section 3
only after verifying the digests belong to that head.

Audio listening is unavailable: state that this is not a full musical
judgement. Never claim the excerpt is musically proven.

Return JSON matching the review schema only.

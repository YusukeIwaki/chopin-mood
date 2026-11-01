# Candle clearing end-state

Generated on 2026-11-01 with the Codex built-in image_gen tool, edit mode. Model version was not exposed. The only reference was the registered original painting `assets/visual/candle-rift-v1/world.png`. No audio was supplied. The PNG is stored unchanged; the sky reveal, cloud movement and flame are rendered locally and deterministically.

## Exact final prompt

Use case: lighting-weather.
Asset type: final cleared-sky state for a 9:16 painted classical-piano film.
Input image 1 is the edit target: the existing fire, basalt and storm world. Preserve its composition and painterly texture.
Primary edit: the storm has cleared at the triumphant ending. Replace the dense dark storm clouds with spacious, luminous pale blue sky above a warm golden opening near the same distant horizon. Leave only a few soft thin wisps toward the outer upper edges, so the central and upper sky feel open and relieved. Light should feel atmospheric and painted, with subtle cloud texture and gentle tonal depth.
Invariants: keep all rock cliffs, jagged mountain silhouettes, winding lava river, ledges, perspective, horizon location and foreground framing exactly as in the input. Keep the lava incandescent orange and preserve the charcoal rock texture. Change only weather and sky illumination; gentle warm reflected light on existing rock edges is acceptable. Do not invent new terrain or objects. Keep the same sophisticated oil/gouache rendering, detail density and portrait aspect ratio.
Avoid: visible sun or moon disc, stars, planets, people, buildings, church, candle, text, border, watermark, lightning bolts, sparks, a whiteout or uniformly blank sky. This is one matching end-state painting to reveal through the original clouds with deterministic animation. No audio is supplied.

## Visual repair 1: sky-only layer

The initial matching landscape produced ghost mountain silhouettes when composited and moved. Its generated PNG SHA-256 was `d50b8a2a0719b28df07bd7d4d8406872737fa315a66c121ae1671aec7c68459c`. It was the sole reference for a second built-in image_gen edit. The active PNG now contains sky only; the renderer preserves the original world geometry and masks the sky in source coordinates. The initial local media and review are preserved separately, and this is repair 1 of the explicit ending request.

Exact edit prompt:

Use case: lighting-weather. Edit this image into a SKY-ONLY compositing layer for a painted piano film. Preserve the existing rich oil/gouache brushwork, pale blue open upper sky, delicate peach-gold clouds at the edges and luminous warm golden opening centered near x=0.5 y=0.36. Remove EVERY mountain, jagged rock, cliff, lava river, ground, horizon silhouette and terrain object from the ENTIRE image. Replace all removed terrain, including the side spires high in the image and the whole lower portion, with continuous soft atmospheric sky/cloud color. There must be NO mountains, peaks, silhouettes, sharp triangular forms, rocks, terrain, lava, sea or objects anywhere, even in the distance. Keep the upper 40% composition of blue sky transitioning to warm golden clouds as close as possible, but replace the dark spires there with sky. The bottom 60% may continue warm softly painted atmospheric clouds because it will be masked out. This is an opaque full-canvas sky texture, not a landscape. No visible sun or moon disc, stars, text, border, watermark, buildings or figures. No audio supplied. Portrait aspect ratio identical to input. Finish with a relaxed open blue sky and golden light, no storm.

## Terms and disclosure

OpenAI Terms of Use, effective 2026-01-01, checked 2026-11-01: https://openai.com/policies/row-terms-of-use/ . Output ownership is assigned to the user to the extent permitted by law; outputs may be non-unique. This is not an exclusive copyright or third-party clearance claim.

YouTube disclosure guidance checked 2026-11-01: https://support.google.com/youtube/answer/14328491?hl=en . This is an AI-painted fictional setting; preserve provenance and disclose generated visuals when applicable during an explicitly authorized upload. No YouTube upload is part of this revision. Generation cost is unmeasured.


## Native compositing geometry (code derived, visual repair 2)

`sky-mask.png` is a 940×1672 grayscale alpha, generated from the unchanged
registered `assets/visual/candle-rift-v1/world.png` by `build_mask.py` and
`mask-guide.json`. This mask is renderer geometry, not a new AI painting.
The recipe verifies the source SHA-256, follows native red-channel edges near
manually traced ridge guides, removes isolated path impulses, and resolves the
left/right high-spire concavities as connected two-dimensional paint regions.
It fills enclosed painted highlight holes; open sky gaps under leaning spires
remain sky. A Gaussian sigma of 0.6 native pixels feathers only the immediate
pixel edge. No terrain-bearing image is affine-copied into the sky.

Rebuild with the pinned project dependencies:

```bash
python assets/visual/candle-clearing-v1/build_mask.py --verify
# Intentional regeneration (update reviewed registry hashes if bytes change):
python assets/visual/candle-clearing-v1/build_mask.py
```

The manifest binds the original world, native mask, guide, recipe and final
sky-only painting hashes. Earlier coarse masks and failed intermediate views
remain local review history and are not used by this bundle.

PNG compression bytes can differ across operating-system codec builds even
when native alpha pixels are identical. `--verify` first authenticates the
registered PNG bytes against the manifest, then compares its decoded L pixels
and dimensions with the rebuilt geometry. It does not compare a newly encoded
PNG stream with the registered compressed stream or alter the registration.

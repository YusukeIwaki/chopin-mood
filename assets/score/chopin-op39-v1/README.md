# Chopin Op. 39 symbolic reference, v1

This optional reference supports `chopin-scherzo3-coda-v1`. It contains symbolic
score data only. The manifest pins the original NIFC sources and every asset
hash. `ATTRIBUTION.md` and the unchanged upstream `LICENSE.txt` retain the
CC BY 4.0 attribution and commercial adaptation terms.

Rebuild locally, with no network or notation-library dependency:

```bash
python scripts/build_score_reference.py
python -m pytest tests/test_score_alignment.py -q
```

The compact JSON columns are described in each file. Quarter positions,
durations and beats are canonical rational pairs. The first chord subtoken
sets the Humdrum spine clock; individual subtokens retain their explicit
durations. This matters for mixed durations in the primary source, including
m210 and m365. Null-only records never advance time. Forks inherit their staff
and outstanding rhythm; merges require compatible spine state. Tied segments
retain duration and pitch but their attack flag is false. Grace notes have
zero duration. Literal **kern pitches are sounding pitches; `*8va` is a display
instruction and does not transpose the derived notes. Key signatures do not
replace explicit pitch spellings. Repeats are not expanded. Unsupported spine
structure or missing explicit rhythm stops the build rather than guessing.

Both editions independently reproduce 649 written bars of 3/4 and 1947
quarters. Registered landmarks are m542 beat1 (q1623, the major arrival), m567
beat2 (q1699, strict both-hands octave entry), and m573 beat1 (q1716, the fast
coda). The earlier m567 beat1 has RH octaves with a single LH B-sharp. At m573
the RH G-sharp3 is a tied continuation; the new LH C-sharp2 is the useful
register-specific attack. The profile does not call the entire coda major.

Alignment runs native-rate, centered power STFT chroma with explicit tuning0,
half-quarter reference occupancy, and cosine DTW. The score beginning is free,
the ending is forced; unsupported cuts, repeats or a different ending can
therefore fail. Features and cost comparisons have declared quantization.
Local refinement searches only the DTW interval plus 350ms, checks newly
entering pitches, excludes prior-pitch harmonic collisions, and requires the
lowest new pitch's direct fundamental growth. A high RH note alone cannot
supply the missing low fundamental. The direct fundamental must also reach
0.1% of the strongest spectral bin over the local search region (magnitude to
power1.5, smoothed over the same analysis frames). This fixed denominator
also prevents near-silence tails from amplifying a relative ratio. A
target-comb ratio alone can turn a continuing
note's release leakage into a false bass entry. Weak fundamentals fail this
conservative gate; pedal and masking remain limits.

The result stores integer native source samples, STFT window/hop, score
provenance and bounded signal diagnostics. Reported uncertainty is analysis
support, not a calibrated accuracy interval or a probability of correctness.
Synthetic tests validate timing, octave aliases, tied-pitch collisions and
negative inputs; they do not establish correctness for every real piano
recording. Independent music review remains necessary.

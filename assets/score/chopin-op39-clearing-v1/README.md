# Clearing extension to Chopin Op. 39 v1

This optional extension registers `chopin-scherzo3-coda-clearing-v1`. It pins
`assets/score/chopin-op39-v1/manifest.json` and its two NIFC editions. The
original three landmarks, symbolic notes, attribution and CC BY 4.0 license
remain unchanged. See the base bundle's `ATTRIBUTION.md` and `LICENSE.txt`.

The score-defined fourth landmark, `final_scale_arrival`, is RH E7 (MIDI100)
at quarter1860, m621 beat1, completing the final 25-note ascent from q1848.
Both registered editions agree. Its notated duration is one quarter. The
visual clearing begins after the detected boundary while E7 may still ring.
This symbolic meaning denotes the intended note attack; acoustic detection
supplies bounded pitch-growth evidence consistent with that target. It does
not certify written-note identity, physical keystrike or hand provenance.
This cue is not note-off, silence, a later final chord or a settling chord.

Native STFT/chroma DTW supplies global score context with the existing mean,
local-cost, interval and coverage limits. The terminal search retains its own
original DTW interval plus the unchanged 350ms refinement padding. No recording
timestamp or strongest isolated E7 over the recording supplies this cue.
Original samples remain unchanged; analysis does not resample the source.

A piano can depart from equal temperament in its high register. Independent
frequency registration fits cents versus written MIDI from the first 24 ascent
score-note windows; terminal E7 is held out. At least 18 observations must fit
within ten cents and span 24 semitones. The robust correction is bounded to 40
cents and one cent per semitone. Insufficient or incoherent registration
rejects. Fit support is frequency evidence, not certification of 18 performed
strikes. The target cannot retune itself to whichever terminal peak is largest.

The terminal fundamental uses the predicted registration and retains the
0.001 ratio to the actual strongest local spectral bin, with a fixed regional
denominator. Its positive growth must reach 0.003 of the regional maximum
fundamental magnitude, the same numerical fraction used for growth in the
unchanged original refinement. The narrow peak must exceed nearby spectral
background by four. Direct growth must contribute at least half of the
combined evidence against positive growth at f0/2, f0/3 and f0/4 within half
an STFT window. Strong continuing lower-note energy alone does not disqualify
a high-register growth event. Static energy ratios remain diagnostics.

Exactly one terminal growth candidate must meet these gates. All separated
terminal candidates are considered, without a top-eight cap or relative
2%-of-strongest pruning. Growth peaks within half an STFT window are clustered;
that resolution is not a guarantee of resolving separate physical strikes.
Unequal or slow-rise competing growth events cause abstention. The result's
method and limits explicitly describe pitch growth, not verified keystrikes.

An ordered absolute-register trajectory across the whole ascent remains a
**diagnostic only**. It searches score-path-bounded context, permits limited
omitted observations, and records an incomplete path or computation limit
without deciding terminal acceptance. Its prefix candidates are capped at
eight and its history search at 20,000 states. Neither this trajectory nor
frequency-fit observations impose transcription of every performed note or
supply terminal timing. Pedaling and weak notes can obscure individual strikes.

The original three cues retain their bass refinement and chroma cosine-cost
gate. For this new cue, terminal chroma is diagnostic; the separate
written-register growth cost is bounded to 0.5 after context, registration,
fundamental and tonality gates. No existing visual timing or QA threshold is
widened. The no-profile and original three-landmark paths retain their formats.

Known limitations are deliberately tested. A sole synthetic E7 inserted
0.20s early or 0.15/0.25s late inside the search window is detected at its actual
growth location; the detector cannot establish that the author's intended
written identity was different. Smooth 0.5Hz amplitude modulation of an
already ringing E7 can also qualify without a fresh physical strike. These
are observed identity limits, not passed wrong-note negatives or universal
accuracy claims. Monotonic decaying E7, missing target evidence, bass alone,
new lower-note partials, neighboring pitches, broadband transients, duplicated
growth events and wrong global context have separate negative calibration.

Known-native-time calibration also covers 16/48kHz, rubato, gain, stretch,
misleading prefix chroma anchors, missing or overlapping weak prefix notes,
strong simultaneous bass and continuing lower notes. Heavy interference can
leave insufficient registration and must reject, including a documented
16kHz public-analysis fixture. Mono mixtures can contain indistinguishable
hypotheses. Independent music review and listening remain separate gates.

Sustained notes causing alignment delay are discussed by Li and Duan,
[An Approach to Score Following for Piano Performances With the Sustained
Effect](https://bochenli.github.io/publication/li2016AnApproach.pdf), 2016,
DOI 10.1109/TASLP.2016.2611938; see the authors' [project
summary](https://labsites.rochester.edu/air/projects/scorefollowing.html).
That motivates testing continuation versus new growth here. This detector
is a separate bounded hypothesis, not their implementation or accuracy claim.

Later left-hand and broken figuration continues after m621. The post-flourish
m633/634 and major-resolution m644 are distinct events. They are not substituted
for the final ascent arrival. No claim is made that all notes end at this cue.

The extended candidate must contain all four native-sample landmarks and at
least sixty video frames after the first boundary at or after the final cue,
at fixed30fps. Its saved landmark frame contains the native sample; visual
activation rounds to the next boundary when needed to avoid anticipation.
This two-second visual span is separate from detector accuracy and does not
relax the six-second scene minimum. The last two landmarks remain internal
controls in scene03. Validation is local and deterministic, with no recording
asset, network analysis dependency or manual recording time in this bundle.

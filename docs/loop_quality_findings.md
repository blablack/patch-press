# Loop-quality investigation — findings

Status: the investigation below is complete; the **fixes have now been implemented**
(see *Implemented* immediately after). The diagnostics remain env-gated and provably
additive (see *Verification*).

## Implemented (fix pass)

- **Fix A — pluck-window clamp** (`envelope.py`). The pluck test averaged RMS over
  `[peak+1.5s, peak+3.0s]`; on short samples whose loudest frame lands late this overran
  the release into silence → false pluck → no loop. Now the window is anchored inside
  `[sustain_start, sustain_end]`. **Verified:** Authority's 10 false-plucks → sustained
  and now loop (19→29 looped notes); it also fixed the *same latent bug in a control*
  (Surge Bass 1 had 4 flat-sustained notes silently not looping → recovered). Genuine
  plucks still detected (3/3 synthetic), controls' classifications unchanged.

- **Fix B — length + placement ranking** (`loop.py`). The score `0.6·chroma +
  0.25·amp + 0.15·slope` had no length term, so loops collapsed to the shortest
  seam-clean window. Added a time-based length reward and an amplitude-drift (placement)
  penalty to the candidate *ranking* only — the pass/fail gate is unchanged, so which
  candidates pass (and thus the controls' candidate sets) is untouched; only the order
  changes, so the caller's first pick is a longer loop on a flat part of the envelope.
  **Verified:** MFM failures lengthened (Electricity Tear 0.20→0.92 s, Combo Organ
  1.01→1.83 s, Easy Wave 1.00→1.37 s). Controls: no loop got shorter, zero new clicks
  (amp_disc stays < 0.25, all pass validation); their loops are *longer* (Growly
  1.26→2.5 s) — harmless for static sounds but worth an ear-check.

- **Fix C — too-short validator guard: not implemented (subsumed by B).** With B ranking
  longer loops first and the existing central-region fallback guaranteeing a loop, a hard
  too-short reject would mostly replace a clean short loop with an unvalidated fallback —
  riskier, not safer.

- **Fix D — Dippin Rez octave clicks: attempted, reverted, DEFERRED.** A+B already cut
  MFM clicks to 5, all on extreme-high notes (MIDI 90–105) where bright/octave content has
  no clean period. A quick fallback-snap attempt optimized the wrong metric (pre-crossfade
  endpoint match) and made A7 *worse* (amp 0.11→0.34), so it was reverted. The real seam
  after the crossfade is `audio[LS-1]→audio[LS]`, so the proper fix is to snap the loop
  *start* to a low-slope/extremum point (or period-lock to the low octave) and needs
  listening to validate — a focused follow-up, not a safe quick change.

Regression guard: all the above checked with `tools/loop_report.py` against the control
set (PHAROH, both Odin2, Surge Bass 1 / Behemoth / Deep End / Distorted MW / Slow).

## What prompted this

Manual listening of the latest `debug_script.sh` run found loop-point problems
concentrated in **evolving / analog** sounds. The clean controls were
**Dexed PHAROH**, **both Odin2** patches, and five **Surge XT** basses (Bass 1,
Behemoth, Deep End, Distorted MW, Slow); the failures clustered in **Mini From Mars**
(sampled Minimoog hardware, a WAV library) and two **Surge XT** pads.

> **Dexed SAW EM UP is excluded from the dataset.** It is a non-musical
> "blip & bloop" preset; its loops are out of place, but it is not a representative
> sound and must not be used as a control or a tuning target. (It also has by far the
> highest movement metrics of anything measured — a reminder that those metrics do
> not track musical quality.)

The key realization: the pipeline is a *pure function of the input's properties*
(sustain length, pitch stability, timbral movement). "Same pipeline, much worse on
a third-party library" is not a curiosity — it means the self-rendered VST/CLAP
captures share a hidden, favourable profile (long controlled hold, steady sustain,
no release tail) that the pipeline was implicitly tuned for, and a hardware
recording has none of it. So the investigation measured the **inputs and the
decisions**, not the audio quality directly.

## Two failures wearing one label

The reported defects split cleanly into two buckets:

- **Bucket A — clicks / audible seam**: Authority, Dippin Rez.
- **Bucket B — no click, but musically wrong** ("too close" / "different tonal
  points"): Combo Organ, Creamy Poly, Easy Wave, Electric Tear, Fat N Creamy,
  Doomsday, Eighties Drone.

`validate_splice_reason` (`loop.py`) only measures `amp_disc` / `deriv_disc` —
seam discontinuity. **Bucket B is invisible to it by construction**, and a short
loop is the *easiest* loop to make click-free, so the validator passes exactly the
loops the ear rejects. We were validating click-freeness, not musicality, and the
dominant complaint lives entirely outside what we measured.

## How the data was gathered

- `loop.py` `find_loop_candidates` now emits one JSONL record per note when
  `PATCHPRESS_LOOP_DEBUG=1` (path via `PATCHPRESS_LOOP_DEBUG_PATH`), one file per
  worker PID. Each record carries the per-candidate breakdown (start/end, length in
  samples and **periods**, generator source, score and its chroma/amp/slope
  components, amp_disc/deriv_disc, passed flag) plus per-note aggregates
  (classification, `rms_depth`, chroma-movement, centroid CV, the winning loop).
- `tools/loop_report.py` joins that JSONL to post-hoc WAV/XML metrics (re-derived
  envelope class, chroma/centroid/MFCC movement, seam discontinuity, period count)
  into one CSV row per note, with per-source summaries and an optional spectrogram
  `--plot` mode.
- Corpus analysed: **253** MFM note-records (Combo Organ + the other MFM presets),
  plus the post-hoc CSV over all **549** zones for the full matrix.

## Findings — symptom → measured signature → cause (confirmed or hypothesis)

### 1. Library samples are ~4× shorter → a thin candidate pool
Mini From Mars sustains are ~**4.2 s** total vs **15–17 s** for the VST/CLAP
captures. Fewer periods in the body means fewer long loop candidates even exist, so
the scorer is choosing from a pool already biased short. This is an **input delta**,
not a pipeline bug — but it amplifies everything below.

### 2. The scorer has no length term → short loops win by a hair *(smoking gun)*
`_boundary_score = 0.6·chroma + 0.25·amp + 0.15·slope` — **no length / coverage
term**. Chroma similarity is maximised by the *shortest* loop (least time to drift),
and the sort then hugs the minimum length. Measured over 2,668 passing candidates:

- `corr(len_periods, score) = -0.082` — score actively (if weakly) prefers shorter.
- Of notes with ≥2 passing candidates (190), the **median** "longest available /
  chosen" length ratio is **3.0×** (p75 = 7.5×, max = 79×)…
- …while the **median score gap** between the winner and that far-longer loop is
  only **+0.089**.
- **15%** of notes had a ≥3× longer loop lose by ≤0.05 score.
- Worst example, note 66: a **0.05 s** loop @ 0.846 beat a **2.00 s** loop @ 0.837
  — a 40× longer, musically-superior loop lost by **0.009**.

This is the mechanical root of Bucket B "too close": nothing rewards length, so the
loop collapses to the shortest seam-clean window.

**The decisive separator is length, not movement.** Per-preset median loop length
cleanly splits the corpus: every clean control is **≥ 1.5 s** (PHAROH 1.75, Growly
1.50, BS Decay 2.25, Bass 1 1.50, Behemoth 2.75, Deep End 2.50, Distorted MW 1.50,
Slow 1.50), while **7 of 9 failures are ≤ 1.0 s** (Electricity Tear 0.20, Authority
0.50, Creamy Poly 0.50, Doomsday 0.75, Easy Wave/Fat N Creamy/Combo Organ ~1.0). No
movement metric draws this line: Doomsday (bad) and BS Decay (good) have near-identical
`centroid_cv` (0.55 vs 0.57), and Combo Organ / Eighties Drone / Doomsday are all
low-movement yet bad. This **closes the "route by movement" idea** (see finding 5) and
means a length preference is safe for the controls — they already sit long, and a
length term can only lengthen, never shorten. The two failures it will *not* explain
are **Dippin Rez (1.99 s)** and **Eighties Drone (3.0 s)** — long-but-bad, a different
mechanism (seam click or tonal jump on a long loop), still to be inspected.

### 3. Seams are clean everywhere → failures are invisible to the validator
Across the failing notes, `amp_disc` / `deriv_disc` / MFCC-seam distance are all
low (e.g. Combo Organ C-1: loop [32774 → 75442], amp_disc = 0.002). The failures
are **short-but-clean** (Bucket B), exactly the blind spot of
`validate_splice_reason`. Confirms the validator cannot catch the dominant defect.

### 4. Doomsday: the loop sits on the decay transient, not the stable plateau
**Correction of an earlier draft of this report.** An earlier version claimed
Doomsday was a "pluck misclassified as sustained." That was wrong — it was inferred
from a mislabeled metric, never verified against the envelope. Measuring the actual
rendered notes:

- The pluck test is `avg RMS in [1.5,3.0]s < 0.08·peak`. Doomsday measures **0.90–0.97**
  (A1, C4, A0) — nowhere near pluck. The `"sustained"` classification is **correct**.
- The envelope holds near peak for ~3 s, decays over the next ~4 s, then **settles to
  a steady ~30–50% plateau it holds until note-off** (C4 from 5 s on:
  `0.44 0.42 0.35 0.36 0.31 … 0.30 0.32`). A genuinely sustained, loopable sound.

The real defect is **loop placement**. All three inspected loops land in the first
1–4 s, straddling the steep decay slope, so amplitude falls **within the loop**:

| note | loop region | env across loop | plateau (6–14 s) ignored |
|---|---|---|---|
| A1 | 1.86 → 3.87 s of 16.2 s | 0.99 → 0.60 (Δ +0.39) | 0.39 |
| C4 | 1.30 → 3.80 s of 15.9 s | 0.94 → 0.61 (Δ +0.32) | 0.30 |
| A0 | 0.74 → 2.22 s of 16.1 s | 0.95 → 0.60 (Δ +0.35) | 0.52 |

Each wrap jumps amplitude ~0.60 → ~0.95 (≈1.6×) = audible pumping / "different tonal
points." `amp_disc ≈ 0.006` because the crossfade smooths the *seam*; the validator
is blind to the level drop *across the loop body*. This is the **same root as
findings #2 and #3** (no length/placement term; seam-only validation), applied to
amplitude — **not** a classification problem.

**On `rms_depth`:** it is `(max−min)/mean` of the RMS envelope — a *spread* measure
(hence values >1.0), not a decay level. Doomsday's high `rms_depth` (1.0–2.4)
reflects the big decay-to-plateau span and is well *above* the 0.40 modulation gate;
it is unrelated to the pluck threshold. The "high rms_depth = decay discriminator"
idea may still hold, but it is **untested** and not claimed here.

### 5. The RMS-only modulation gate is a plausible mechanism — but unconfirmed
Modulation detection is gated on **RMS depth ≥ 0.40**, then needs a periodic
autocorr peak that snaps to a BPM subdivision within 12%. Two failure modes are
*possible*: (a) a **filter sweep that moves timbre while keeping RMS flat** never
fires the gate → `"sustained"`; (b) a **free-running LFO** fires the gate but is
rejected at the BPM-snap → `"sustained"`. **Neither is verified by listening or
envelope inspection** — they remain hypotheses for the next phase.

**Doomsday is a counter-example to (a):** its RMS is *not* flat (`rms_depth` 1.0–2.4,
well above the gate), the gate *does* fire, and it is still `"sustained"` simply
because the one-time decay has no periodic modulation — which is correct. So "RMS-flat
sweep → sustained" is not the Doomsday mechanism, and may not be the mechanism for the
other "tonal points" presets (Creamy Poly, Easy Wave, …) either — those were **not**
inspected and should be before any routing change is made.

**Caveat from the data:** the source-level movement aggregates (chroma-movement,
centroid CV) do *not* cleanly separate good from bad on their own. Chroma is
pitch-class and blind to brightness; Dexed (FM) shows chroma movement while MFM
(filter sweep) does not. That non-separation is itself a finding: **movement alone
is not the predictor — movement combined with a too-short loop is.** The next phase
should route on a timbral sensor (MFCC/centroid), not chroma, and not on RMS depth.

### 6. Missing loops on plucks
29 of 253 MFM records hit the `pluck` path (no loop emitted). Reason tally over the
253: `ok = 222`, `pluck = 29`, `none_passed = 2`. The pluck count is expected for a
bass library. Whether any of these are *mis*-classified (a real sustain dropped to
pluck, or vice-versa) was **not** audited per-note and is left for the next phase.

### 7. The three "long-but-bad / missing" cases, now inspected
The three failures that the length separator could not explain were inspected
individually:

- **Eighties Drone → it *is* length.** The 3.0 s *median* masked it: ~8 of 30 notes
  (mostly the upper register: D#4, D#5, D#6, F#6, A6, F#7, A7) collapsed to **0.05–0.20 s**
  loops (`loop_frac_sustain` ≈ 0.01–0.03), while the rest are healthy 3–6 s loops. The
  short notes are the audible "too close." **Fix 1 (length) handles it.**

- **Dippin Rez Octave → genuine seam clicks (Bucket A), not length.** Loops sit on a
  *flat* envelope (start≈end RMS: 0.85→0.82, 0.70→0.70, 0.66→0.67 — placement is fine),
  yet splice `amp_disc` is **0.05–0.24** and `deriv_disc` up to **0.66** (A7 flagged
  CLICK), median `amp_disc` 0.044 vs ~0.007–0.013 for the controls. `mfcc_dist` is low,
  so it is *not* a timbre jump — it is an instantaneous **waveform** discontinuity. Cause
  is the preset: an **octave** (dual-oscillator) sound has no simple period (YIN period
  counts blow up to 785–8814), so no single loop point wraps cleanly and the crossfade
  does not fully mask it. **Length will not help; this needs period-locked seam
  alignment (to the low octave) or a longer crossfade.**

- **Authority → mixed, and the "missing loops" is a FALSE-PLUCK BUG.** Its XML has ~30
  zones but ~11 are `loopMode=0` (no loop). Inspecting them: **10 are sustained notes
  mis-classified as `pluck`**, and 1 (C0) is `none_passed`. The pluck test
  (`envelope.py:159–162`) averages RMS over `[peak_frame+1.5s, peak_frame+3.0s]` vs
  8% of peak. These are wobbly Minimoog notes whose *loudest* RMS frame lands late
  (3.1–3.9 s into a 4.7 s sample), so the window starts at ≈ peak+1.5 s ≈ **4.7 s — at
  or past the end of the file**, well beyond `sustain_end` (~4.2 s). It averages the
  release/silence (avg/peak ≈ 0.001–0.017) → false pluck → no loop. Notes that survive
  (D#0, C0) merely peak earlier (2.3–2.8 s). Every "missing" note has a strong sustained
  body (avg RMS 0.42–0.78). **This is not a routing decision — the window must be clamped
  to the sustained region so it never averages the release.** The 19 notes that do loop
  are additionally short (Fix 1) with a few clicks (`amp_disc` up to 0.13) and tonal
  jumps (`centroid_delta` up to 1272 Hz).

## Verification (of the diagnostics — the algorithm is unchanged)

- **Additive-only:** rendered `Mini From Mars/Combo_Organ.yaml` twice, once with
  `PATCHPRESS_LOOP_DEBUG=1` and once without. All **37 WAVs byte-identical**
  (md5 set match); XML **identical** in every `startLoopPos`/`endLoopPos`/
  `loopMode`/`transpose` once the deliberately-varied output-path token is
  normalized. The env var changes nothing in the output.
- **Cross-check:** `amp_disc`/`deriv_disc` from the JSONL match
  `tools/detect_clicks.py` on the same files (same loop points, same math).
- **Count check:** JSONL note-count matches the number of zones rendered.

## Summary table

Status key: **confirmed** = measured + cross-checked; **hypothesis** = plausible,
not yet verified by listening/envelope.

| # | Symptom (ear) | Measured signature | Cause | Status | Recommended fix (DEFERRED) |
|---|---|---|---|---|---|
| 1 | worse on library generally | MFM sustain ~4.2 s vs 15–17 s VST | shorter input → thin candidate pool | confirmed (MFM only; not Surge) | accept longer captures / oversample library bodies |
| 2 | "too close" | corr(len,score)=−0.08; median 3× longer loop lost by 0.089; note 66: 0.05 s beat 2.00 s by 0.009 | `_boundary_score` has no length term | confirmed | add a length/coverage preference to scoring |
| 3 | no click yet wrong | amp_disc/deriv_disc ~0 on failures | validator measures only seam discontinuity | confirmed | add a musicality/length gate beyond click-freeness |
| 4 | Doomsday "too close" | loop on decay slope (env 0.95→0.60 within loop); stable plateau at 6–14 s ignored; class=sustained is correct | placement, not classification — same root as #2/#3 | confirmed (envelope-verified) | reward placement in the settled region; penalize level drop across the loop |
| 5 | "different tonal points" | movement present in some presets; gate is RMS-only + BPM-snap | RMS-only modulation gate may misroute filter sweeps / free LFOs | hypothesis (Doomsday is a counter-example) | inspect the "tonal" presets, then route on MFCC/centroid; relax BPM-snap for library |
| 6 | missing loop (Authority) | 10 sustained notes (avg RMS 0.42–0.78) classified pluck; RMS-peak late (3.1–3.9 s), pluck window runs to peak+3 s ≈ past EOF → averages release | **false-pluck bug**: window unclamped, overruns sustain_end on short samples | confirmed (numeric) | clamp pluck window to `[…, sustain_end]` so it never averages the release |
| 7 | Eighties Drone "too close" | ~8/30 notes collapse to 0.05–0.20 s; rest healthy 3–6 s | length, on a subset of notes (median masked it) | confirmed (envelope-verified) | Fix 1 (length) — same as #2 |
| 8 | Dippin Rez "very noticeable" | flat envelope yet amp_disc 0.05–0.24, deriv up to 0.66; mfcc_dist low | waveform click — octave/dual-osc has no simple period | confirmed (envelope-verified) | period-lock seam to low octave / longer crossfade |

## Deferred to the next phase (do NOT implement yet)

1. **Reward length and stable placement in scoring** (covers #2, #4, #7 — ~8 of 10
   failures) — add a length/coverage term to `_boundary_score` *and* bias placement
   toward the settled region (penalize a level drop across the loop body), so loops
   stop hugging the minimum-length, loudest-early window. A **blanket** length term is
   safe: the controls already sit ≥ 1.5 s and a length term only lengthens. The
   movement-sensor route is **not** pursued — no movement metric separates good from
   bad (see finding 2 / 5). Note the length term must work in **time**, not period
   count: Eighties Drone's bad notes are short in seconds though they span many cycles.
2. **Validator rejects too-short loops** (covers #3) — make a sub-threshold loop length
   a rejection reason, so Fix 1 has teeth and a tiny clean loop can't win on
   seam-cleanliness alone.
3. **Dippin Rez clicks (#8)** — period-lock the seam to the *low* octave (or extend the
   crossfade); a length term will not fix a waveform that has no simple period.
4. **Authority false plucks (#6)** — clamp the pluck-detection window in
   `analyze_envelope` to the sustained region (end it at `sustain_end`, don't run to
   `peak+3.0s`) so short library samples whose RMS-peak lands late are not averaged
   against their own release/silence. Bug fix, not a routing decision: the 10 notes are
   sustained and should loop. Regression guard: VST captures peak early, so their window
   already sits inside the body and must stay unchanged.

Each should be validated by re-running `tools/loop_report.py` and comparing against
the regression-control set — **PHAROH, both Odin2, and Surge XT Bass 1 / Behemoth /
Deep End / Distorted MW / Slow** (SAW EM UP excluded) — to guard against regressing
presets that already sound good. Their loops should stay ≥ 1.5 s and ideally
byte-identical.

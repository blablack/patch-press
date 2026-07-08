---
title: Loop detection
layout: default
nav_order: 5
---

# Loop detection

Finding a good loop point is the hardest thing patch-press does. This page explains **how** it decides, **why** it sometimes doesn't loop a sample, and **what you can do** when a preset comes out sounding wrong.

If you just want the mechanical stage description ("chroma seam gate, MFCC seam gate, drift gate…"), that's on the [pipeline page](pipeline.html#4-loop-detection). This page is the practitioner's view.

## What a loop is trying to do

The Deluge holds a note by playing your captured WAV, and when it reaches the end of the *loop region*, it jumps back to the loop start and plays that section on repeat for as long as you hold the key. Two points, sample-accurate: `loop_start` and `loop_end`. Get them right and holding a key sounds indistinguishable from an infinitely-held note. Get them wrong and every 400 ms your preset clicks, glitches, or lurches to a different pitch.

Every design decision in patch-press's loop hunter is about the same thing: pick `loop_start` and `loop_end` such that `audio[loop_end]` transitions to `audio[loop_start + 1]` in a way your ear can't tell from `audio[loop_end] → audio[loop_end + 1]`.

## When it doesn't loop at all

Not every preset gets a loop. patch-press deliberately skips looping when:

1. **The profile has `loop: false`.** The `pluck` and `drums` profiles turn looping off — a plucked bass isn't held, a kick doesn't sustain, there's nothing to loop. `synth` and `pad` turn it on.
2. **The envelope classifier called it a pluck.** Even under `synth`/`pad`, if the sample's RMS collapses to under 10% of peak by the end, the loop stage doesn't run. This is the correct call: a real pluck has no sustain region, and any "loop" would be looping silence or a decayed tail.
3. **No candidate passed the quality gates AND the fallback couldn't find zero-crossings.** Very rare. Almost always the fallback kicks in.

If you're expecting a loop and not getting one, the classifier is the usual suspect. See [I want a loop but there isn't one](#i-want-a-loop-but-there-isnt-one) below.

## How it picks

Once looping is enabled and the sample has a sustain region, patch-press runs three independent candidate hunters in parallel:

### 1. Chroma-based candidates

Chroma is a 12-bin representation of pitch class content (C, C#, D, …, B) with octave folded out. patch-press samples chroma vectors every 0.5 seconds through the sustain, then runs circular cross-correlation to find pairs of moments where **the harmonic content is nearly identical**.

This is the main hunter. It's good at finding loops on tonal, evolving sounds — a slow pad LFO that comes back around, a chorused synth whose beats phase back into alignment. Loops it finds are typically 1–3 seconds long.

### 2. Tempo-synced candidates

Only enabled when `loop_use_tempo: true` in your config (default: off, but the `pad` profile turns it on). Considers loop lengths that are multiples of a quarter note at `capture.tempo_bpm`: 0.25×, 0.5×, 1×, 2×, 4×, 8×, 16× a quarter note.

Useful for presets with a built-in rhythmic element — an arpeggiator, a synced tremolo, a rhythmic filter — where the natural loop *is* the beat length. Without this, the chroma hunter will find a musically arbitrary loop that cuts across the rhythm.

### 3. Period-aligned candidates

Direct hunt for loop lengths that are exact multiples of the signal's fundamental period, between 0.3 and 3 seconds. This is a brute-force scan: try every candidate length, measure the raw waveform mismatch across the seam, take the best.

Effective on very steady tones (long-held pad notes, unchorused synths) where "one period, ten periods, a hundred periods" all seam-match perfectly and you just want the longest one that fits.

### Then: gates, scoring, ranking

All candidates from all three hunters go into a common pool and are subjected to the same **gates** (pass/fail checks) and **scoring** (which of the survivors is best):

**Gates** — a candidate is rejected outright if any of these fail:

| Gate | What it checks |
|---|---|
| Chroma seam | Waveform RMS mismatch across the loop join is under a threshold (0.20). |
| MFCC seam | Timbre (MFCC cosine distance) at the join doesn't jump (< 0.06). Catches "no click, but the sound *changes* at the wrap". |
| Amplitude discontinuity | Sample values on either side of the join don't have a visible step. |
| Derivative discontinuity | The waveform's slope doesn't kink. Catches clicks the amplitude gate misses. |
| Drift (steady sounds only) | On sounds whose envelope is basically flat, the endpoints' local peaks must match. Evolving sounds get a pass. |

**Scoring** — among candidates that passed:

- **Base score** — the quality of the seam itself. Higher = cleaner join.
- **Length reward** — longer loops get bonus points, saturating at 1.5 seconds. This is deliberate: a 200 ms loop with a perfect seam is worse than a 1.5 s loop with a very good seam, because short loops make a static sound feel unnaturally "small".
- **Placement penalty** — loops that sit deep inside the sustain (not straddling the attack or release) are preferred. Reduces the chance of "loop starts, and something audibly changes because we're still in the attack".

The winner's `(loop_start, loop_end)` is written into the Deluge XML. A 10 ms crossfade at the join further smooths any residual seam.

### The fallback

If no candidate passes the gates — usually because the sound is too evolving to have any repeating structure — patch-press falls back to a simple heuristic: **take the central 50% of the detected sustain region**, then snap those endpoints to zero-crossings and adjust them to be an integer number of fundamental periods apart.

This will always produce *some* loop. It won't always be *musical*. But it's better than shipping a preset with no loop at all when the profile said loops are wanted.

## `loop_quality_threshold`

This is the pass/fail cutoff for the base score:

- **Default: 0.75.**
- **`pad` profile raises it to 0.8** (pads are held longer, small imperfections become audible over time).
- Higher = stricter, fewer loops accepted, more presets fall through to the fallback.
- Lower = looser, more marginal loops accepted.

You almost never need to touch this. The two cases where it helps:

- **You're getting fallback loops (obvious, short, feels arbitrary) on a preset that clearly has a sustained tone** — try *lowering* to 0.7 to let the hunter's marginal candidates through.
- **You're getting audible clicks on a preset** — try *raising* to 0.85 to push more presets into the fallback (which snaps to zero-crossings and is click-proof by construction).

## Reading the flags in a scanned config

`scan-*` doesn't run loop detection — it only classifies sustain type. So loop flags appear during `sample` / `batch`, not scan. If you crank verbosity with `-v`, you'll see per-note lines like:

```
[Warmth] note 60: loop 0.984s (chroma 0.12, mfcc 0.04) ✓
[Warmth] note 63: loop 1.427s (fallback: no candidate passed) ⚠
[Warmth] note 66: loop 0.612s (chroma 0.18, mfcc 0.05) ✓
```

The `⚠` marker on the second line means the candidate hunt came up empty and the fallback took over — that note might benefit from a manual pass.

For a full JSON dump of every candidate that was considered on every note (score, seam metrics, gate results, ranking), set:

```bash
PATCHPRESS_LOOP_DEBUG=1 patch-press sample config.yaml --path /media/DELUGE --format deluge
```

That writes `loop_debug.jsonl` in the current directory. Set `PATCHPRESS_LOOP_DEBUG_PATH=/some/dir/loops.jsonl` to write elsewhere. This is the tool you reach for when you're trying to understand why a specific preset's loop sounds wrong.

## Fixing a bad loop

The escape hatch for a specific preset is manual editing. Loop points live in the Deluge XML at `SYNTHS/<preset>/<Preset>.XML`, in each `<sampleRange>`:

```xml
<sampleRange rangeTopNote="42" fileName="…" transpose="-9" loopMode="1">
  <zone startSamplePos="0" endSamplePos="192000"
        startLoopPos="21504" endLoopPos="45568"/>
</sampleRange>
```

`startLoopPos` and `endLoopPos` are sample frame indices into the file. Edit those two numbers, save the XML, put the SD card back in the Deluge — no re-render needed.

You can also **disable looping** for a single range by setting `loopMode="0"` on the `<sampleRange>` element. Useful if one specific note in a multisample refuses to loop cleanly and everything else does.

## I want a loop but there isn't one

Most common: the envelope classifier decided your sample is a pluck. Check:

1. **What profile is the config on?** If it's `pluck` or `drums`, that's why. Change `profile:` to `synth` or `pad` and re-run `sample` on that config.
2. **Is the sound genuinely sustained past the release?** Open the captured WAV in a waveform viewer (Audacity, Reaper). If the RMS collapses within the first 3 seconds, patch-press was right — there's nothing to loop. Rendering with a longer `duration_s` may help if the plugin sustains longer than the default 4-second capture.
3. **Force it.** Set `analysis.loop: true` explicitly in the config. This overrides the "profile says no loop" logic. If the sample really has no sustain, you'll get a fallback loop over the decay, which will sound wrong — but sometimes that's what you want (e.g. a plucked sound with a lush tail you *do* want to loop).

## I want a longer loop

The length reward saturates at 1.5 seconds, so above that patch-press has no preference between "long enough" and "even longer". If a preset picks a 1.6 s loop when a 3 s loop would sound better:

1. **Confirm the longer loop exists.** Look at `loop_debug.jsonl` for that note — the candidates list is sorted by score. If a longer candidate is there but scored lower, it lost on seam quality, not length.
2. **Raise `loop_quality_threshold` slightly.** This can push shorter marginal loops out of the pool, letting longer marginal loops through.
3. **Hand-edit** the XML with the loop endpoints you want.

## Some sounds simply don't loop

There's no clean loop for genuinely aperiodic material — a lone reverb tail, a filter sweep that never returns, a granular texture with no repeating structure. patch-press will always emit *something* (the fallback guarantees it) but the result won't sound musical no matter how much you tune thresholds. For those presets, `loopMode="0"` on all ranges + a longer captured tail is often better than any loop.

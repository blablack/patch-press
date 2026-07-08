---
title: Pipeline
layout: default
nav_order: 4
---

# The analysis pipeline

Every rendered or loaded sample goes through the same six-stage pipeline before it's written to the SD card. This page walks through what each stage does and what knobs in your config affect it.

```mermaid
flowchart LR
    IN[Sample audio] --> T[Trim]
    T --> E[Envelope]
    E --> P[Pitch check]
    P --> L[Loop detect]
    L --> N[Normalize]
    N --> OUT[Ready for export]
```

## 1. Trim

Trims leading and trailing silence. Walks a mono-downmixed RMS envelope from each end and cuts at the first frame whose peak crosses **−60 dB**.

Trim never cuts into an authored loop. If the input WAV has a `smpl` chunk with loop start/end (see [Honoring authored loops](#honoring-authored-loops) below), trim is constrained to stay outside those bounds.

**Off with:** `analysis.trim: false` in the config.

## 2. Envelope detection

The most subtle stage. Classifies each sample as one of:

- **pluck** — no meaningful sustain after the attack
- **sustained** — flat sustain
- **sustained_with_modulation** — sustained with a periodic (LFO/tremolo) or free-running (filter sweep) timbral movement

The classification decides whether looping is even attempted, and — for VST captures — is what `scan-from-probe` uses to auto-pick a profile.

### How it works

1. Compute the RMS envelope on a 512-sample hop.
2. Find the peak RMS frame. Walk backward looking for a plateau (frames within 90% of peak); if there's a plateau of ≥ 2 s, use its start as `sustain_start`. Otherwise the peak is the sustain start.
3. Walk forward from the peak until RMS drops below 40% of peak. That's `sustain_end`, extended forward if a stable plateau (coefficient of variation ≤ 0.25) continues for ≥ 0.5 s beyond the drop.
4. **Pluck check.** Measure RMS in a window near note-off (for VST/CLAP where note-off is known) or in `[peak + 1.5s, peak + 3.0s]` clamped to `[sustain_start, sustain_end]` (for library samples). If that RMS is < 10% of peak, it's a pluck.
5. **Modulation check.** If the RMS variance over the sustain is ≥ 40% of peak, run autocorrelation and look for periodicity in `[0.35s, 8s]`. If a period is found and it snaps to a nearby BPM subdivision (quarter, eighth…) within ±12%, mark as `sustained_with_modulation`.
6. **Spectral LFO override.** Compute the MFCC centroid over the held region. If it has high coefficient of variation (≥ 0.10) AND autocorrelation shows a slow period in `[1s, 12s]`, override to `sustained_with_modulation` even if the RMS variance test didn't trip.

The tempo used for BPM-snapping comes from `capture.tempo_bpm` in the config (default 120).

## 3. Pitch verification

Verifies that each sample plays at the note its filename (or the VST render setup) claims. Uses autocorrelation on a 1-second window taken from the first quarter of the audio, looking in the range `[sr/4000 Hz, sr/20 Hz]`.

The detected pitch is folded into the nearest octave of the expected pitch before comparison — autocorrelation regularly errors by octaves, and for the purpose of "did I render/label the right note" that's not really an error. The remaining cent difference is checked against `analysis.pitch_tolerance_cents` (default: 50, i.e. ±quarter-tone).

### The VST quirk

**Pitch verification runs only for library sources — never for VST/CLAP.** A plugin that's told to play MIDI 60 will play MIDI 60; there's nothing to verify. Library WAVs, on the other hand, might be mislabeled, octave-shifted, or de-tuned by the author on purpose. The drums profile also disables pitch verification (`pitch_verify: false`) — pitch on a drum hit is often not the fundamental you'd expect.

Out-of-tolerance samples are **flagged**, not rejected. The generated preset still includes them; the flag is a nudge to double-check.

## 4. Loop detection

Given a sustained-region audio buffer, find two sample indices `(start, end)` such that playing `audio[start:end]` on repeat is imperceptible from continuing to hold the original note.

patch-press runs three candidate hunters (chroma-based pitch matching, tempo-synced quarter-note multiples, period-aligned brute force), gates the results on seam quality and timbre continuity, scores the survivors with length and placement rewards, and picks the winner. A 10 ms crossfade smooths any residual seam.

If nothing passes the gates, a fallback takes the central 50% of the detected sustain and snaps to zero-crossings.

For the full picture — the candidate hunters explained in depth, what to do when a preset doesn't loop, how to hand-edit a bad loop point, what `loop_quality_threshold` is for — see the [loop detection page](loops.html).

### Honoring authored loops

Some libraries (Samples From Mars is one) bake loop points into the WAV file's RIFF `smpl` chunk — a standard RIFF sub-chunk that stores loop start/end frame indices.

When patch-press reads a WAV that has a `smpl` chunk with at least one loop, it uses those authored loop points **verbatim** and skips loop detection entirely. Trim is constrained to stay outside the loop bounds. The author's intent is treated as ground truth.

The Deluge itself doesn't read the `smpl` chunk — it reads loop points from the XML that patch-press emits. So patch-press only *consumes* `smpl` on input, never *writes* it on output.

## 5. Normalize

Applies gain so the sample set peaks at −1 dB. Three modes:

| Mode | Behavior |
|---|---|
| `per_set` (default for synth/pad/pluck) | All samples in the set share the same gain, chosen so the loudest hits −1 dB. Preserves relative velocity dynamics and round-robin variation. |
| `per_sample` (default for drums) | Every sample is independently normalized. Loses velocity dynamics on purpose — each drum hit gets its own headroom. |
| `none` | No gain applied. Useful when you're feeding into another normalization stage. |

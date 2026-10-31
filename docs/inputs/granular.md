---
title: Granular
layout: default
parent: Inputs
nav_order: 6
---

# Granular

A granular patch isn't a playable copy of a recording — it is a **cloud of short grains** cut from one. The 1010music Bento has a granular engine (`grtrack`), and its factory patches each run two grain oscillators over two different mono sources. patch-press builds the same shape from any WAV: one sound in, one Bento Granular patch out. **Bento only**: the Deluge has no granular engine, so a Deluge export refuses these.

```
patch-press scan-granular <folder> <config-dir>
patch-press sample <config> --format bento
```

`scan-granular` writes one config per WAV directly in `<folder>`.

## What makes a good source

Sustained, tonal, not rhythmic — something that still sounds like itself when cut into 50 ms pieces and re-scattered. Pads, chords, strings, vocals, evolving textures work. Drum hits and rhythmic loops come out as mush. Anything shorter than 1.5 s has no steady stretch to freeze on, and is flagged `REVIEW`.

The scan does not choose the sounds, only describes them. Choosing is the caller's job — `prepare-sd-cards/tools/pick_granular.py` does it for the Bento card.

## What the scan decides

One number: `granular.freeze_pos`, where in the sound the grains are read from (0–1). It is the centre of the 0.6 s window with the most loudness for the least spectral change — the steadiest loud stretch — clamped to 5–95 % so the window never hangs off an end (`analysis/granular.py`, `stable_position`).

```yaml
source: {type: granular, path: Calmbience.wav}
granular: {freeze_pos: 0.4719, tag_hint: texture}
output: {name: Calmbience}
```

## What the export builds

- **Two mono 48 kHz 24-bit excerpts** of at most 12 s, peak-normalised, as the patch's own WAVs. A sound under 12 s goes to both oscillators; a longer one gives the first the stretch around the steady moment and the second the far end, so the two oscillators differ as they do in every factory patch.
- **Two `grainosc` cells**: one *held* (parked on `freeze_pos`, with `grainfreezepos` moved a little by LFO 2) and one *moving* (denser, larger grains, reading further), both detuned slightly and panned apart.
- The rest of the factory's most common cell order: a silenced `osc`, two filters, two envelopes, two LFOs, `seqmod`, `partparms`, chorus, reverb and `fxlemon`.
- The factory routings: LFO 1 → pitch (vibrato, brought in by the mod wheel), LFO 2 → freeze position, macro X → grain size, macro Y → grain density.
- A 4 s `preview.wav` rendered by patch-press's own small grain-cloud model. It is a sketch to tell patches apart, **not** what the Bento will play.

## Status

The structure matches the 50 factory Granular patches; the parameter values are mid-range copies of theirs, not tuned by ear, and nothing here has been tried on hardware.

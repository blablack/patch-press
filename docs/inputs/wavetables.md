---
title: Wavetables
layout: default
parent: Inputs
nav_order: 5
---

# Wavetables

Wavetables are a different kind of animal from every other input on this site. A wavetable WAV isn't a recording of a note — it's a stack of single-cycle waveforms, each exactly 2048 samples long, that the Deluge's wavetable oscillator sweeps through under LFO or manual control.

Because of that, **wavetables skip the whole analysis pipeline**: no trimming, no envelope detection, no loop hunting, no normalization. The file is copied to the target bit-for-bit (preserving Serum's `clm` metadata chunk so the target device knows how to slice it), and the exporter generates the surrounding preset with envelope and filter parameters chosen from spectral analysis of the wavetable itself.

```bash
patch-press scan-wavetables "~/wavetables/ESW Core Tables" configs/ESWCore
```

## Format requirements

- **WAV file**, mono, 16-bit or 32-bit float.
- **Total sample count must be an exact multiple of 2048.** Each 2048-sample block is one frame in the wavetable.
- If Serum's `clm` chunk is present, patch-press preserves it byte-for-byte in the output. This is how the Deluge distinguishes wavetables from raw samples.

If the length isn't a clean 2048-multiple, the config gets flagged REVIEW and defaults to the `pad` archetype.

## Archetype detection

`scan-wavetables` analyzes each file's per-frame spectral content — brightness (centroid), flatness (noise-vs-tonal), odd/even harmonic ratio, timbral variance across the frames — and picks one of six archetypes. Each archetype maps to a canned set of Deluge parameters (envelope shape, filter cutoff, LFO2 rate/depth, starting WT position).

| Archetype | When it's picked | Vibe |
|---|---|---|
| **drone** | High spectral flatness (noisy, inharmonic frames) | Textural, FX, no clear pitch |
| **evolving_pad** | Large timbre range across frames (frames very different) | Long attack, table sweep provides the movement |
| **pluck** | Bright + odd-harmonic-dominant | Fast attack, no sustain, hollow character |
| **lead** | Bright + not odd-dominant | Fast attack, sustained, warm |
| **bass** | Dark + even-harmonic-dominant | Snappy, low-end focused |
| **pad** | Everything else (default) | Slow attack, long release |

The heuristics were calibrated by ear against the Echo Sound Works Core Tables corpus.

## Config shape

```yaml
source:
  type: wavetable
  path: /path/to/Warm Pad 01.wav

wavetable:
  archetype: pad
  wt_position: 0.15         # starting frame (0.0 = first, 1.0 = last)
  lfo2_rate: 0.30
  lfo2_depth: 0.60
  filter_cutoff: 0.55
  attack: 0.70
  decay: 0.50
  sustain: 0.90
  release: 0.75
  filter_type: lpf

output:
  name: WarmPad01
```

Every 0–1 parameter is a plain fraction; the exporter maps it to the Deluge's signed-32-bit param range.

## Overriding the archetype

If the auto-detection picked something you disagree with, either:

- **Re-scan with `--archetype`** to force one archetype across the whole folder:
  ```bash
  patch-press scan-wavetables "~/wavetables/Basses" configs/Basses --archetype bass
  ```
- Or **edit the YAML** for that one file (`archetype:` and the parameter block) and re-run `sample`.

## `scan-wavetables` options

| Option | Default | What it does |
|---|---|---|
| `--archetype pad\|pluck\|bass\|lead\|drone\|evolving_pad` | auto | Force a single archetype for every file in the folder. |

## On the SD card

Wavetables go into `SYNTHS/<name>/`, same as sample-based synths. The Deluge treats them as synth presets whose oscillator mode is set to `wavetable` in the XML. The WAV lives alongside in the same folder.

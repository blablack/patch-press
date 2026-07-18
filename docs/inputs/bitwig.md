---
title: Bitwig multisamples
layout: default
parent: Inputs
nav_order: 7
---

# Bitwig multisamples

A Bitwig Studio **`.multisample`** is a single file that bundles an entire multisampled instrument: it's a ZIP archive holding all the sample WAVs plus a `multisample.xml` that maps each WAV to a key zone, a velocity zone, and (for sustaining sounds) a loop.

This is the richest input patch-press consumes. Everything the other library sources have to *infer* — the root note of each WAV, whether a note is meant to loop, exactly where the loop sits — is **authoritative metadata** here. So `scan-bitwig` reads it straight from the XML: no filename parsing, no pitch tracking, no loop detection, no probe rendering.

```bash
patch-press scan-bitwig "~/Downloads/Orchestral Tools" configs/OrchestralTools
patch-press batch "configs/OrchestralTools/**/*.yaml" --path /media/DELUGE --format deluge
```

`scan-bitwig` searches **recursively** for `*.multisample` files and mirrors each archive's folder position (relative to the folder you pointed it at) into both the config tree and the on-card `output.subfolder`, so a vendor pack's own category folders (e.g. `Orchestral Brass/`) survive onto the SD card.

## What the scan reads from the XML

Each `<sample>` element carries a `<key>` (root note + zone), a `<velocity>` zone, an optional `<loop>`, and a `[sample-start, sample-stop)` play region. Two things drive the generated config:

- **Loop mode.** Bitwig writes `mode="loop"` on *every* zone of a sustaining articulation and `mode="off"` on *every* zone of a staccato / one-shot one — it's bimodal per file. So the profile follows it directly:

  | Archive's loops | Profile | Result |
  |---|---|---|
  | All zones looped | `synth` | Ships the authored sustain loop, no re-detection |
  | No zones looped | `pluck` | Ships the one-shot as-is, no loop |

  A "Sustained", "Tremolo" or "Trills" instrument becomes a looping `synth`; "Staccato", "Marcato", "Pizzicato", "Single Hits" and the "(Releases)" tails become `pluck`.

- **Root note.** Taken verbatim from `<key root>`, so pitch verification is disabled in the generated config (the roots are ground truth — an "Octave" or "Trills" patch is deliberately not at concert pitch, and re-checking it would only produce spurious warnings).

## How a DAW multisample becomes a hardware preset

Bitwig instruments carry things a Deluge/Polyend preset can't use. The adapter reduces each note to a single playable sample:

- **Velocity layering is dropped.** Hardware sampler presets play one sample per note, and both exporters collapse to one anyway. The adapter keeps the single velocity layer whose top velocity is nearest `source.velocity` (default 100). Hand-edit `velocity` in the config to pick a softer or louder layer.
- **Notes are thinned** to `--note-step` semitones apart (default 3), same as [sample libraries](sample-libraries.html).
- **Each WAV is sliced to its `[sample-start, sample-stop)` play region.** For a looped note that region ends exactly at the loop end — Bitwig stores extra recording past it that it never plays — so the export stays tight.
- **The loop crossfade is baked with Bitwig's own `fade` length** and the loop is handed to the pipeline as an *authored* loop, so it ships verbatim (no second crossfade, no re-detection). Bitwig's auto-looper relies on that runtime crossfade, so baking it is the faithful equivalent.
- **Per-sample `gain` is applied** before normalization, preserving the author's note-to-note balance.

`reverse` is honored as read (all-`false` in practice), and non-zero `tune` values are rare enough to be left to the exporter's transpose.

## REVIEW flags

`scan-bitwig` flags a config as REVIEW when **most zones are fixed-pitch** (`<key track="0">`) — typically a percussion map where each key is a different drum played at its natural pitch rather than a repitched instrument. It still exports as a `pluck` multisample; the flag is a nudge to check the keyboard mapping or consider splitting it into a kit by hand. Files with a parse problem (not a real ZIP, missing `multisample.xml`, no mapped samples) are skipped with a message and don't produce a config.

## Overrides

- `--profile synth|pluck|pad|drums` forces the profile instead of reading it from the loop flag.
- `--note-step N` changes note thinning (`1` keeps every mapped note).
- `source.velocity:` in a generated config selects which velocity layer to keep.

As always, the YAML is the seam: if the scan picked the wrong layer or profile, hand-edit the one config and re-run `sample` on it — no re-scan needed.

---
title: Oneshots
layout: default
parent: Inputs
nav_order: 4
---

# Single-note oneshots

Some libraries — Monosounds is the classic example — ship a folder full of WAVs where **each file is its own preset**. Not a multisample, not a drum kit. A minimoog sample library might have 200 files, each holding a single held note of a different patch.

The distinguishing feature: **no shared notes to group by**, and often no octave information in the filename (just a pitch class like `MMS_Warm_Bass_A.wav`).

```bash
patch-press scan-oneshots "~/samples/Monosounds/Minimoog Oneshots" configs/Minimoog
```

## What scan-oneshots does

For each WAV in the folder:

1. Runs a pitch tracker over the file's sustained portion.
2. Picks the MIDI note whose fundamental most closely matches (respecting the pitch class in the filename when available, so `A` in the name gets snapped to the correct A across octaves).
3. Pins the resolved MIDI note into `source.note` in the config so `sample`/`batch` don't have to re-detect.
4. Writes one config per WAV — the preset name comes from the file stem.

## Config shape

```yaml
source:
  type: library
  path: /path/to/MMS_Warm_Bass_A.wav
  note: 45              # detected root, pinned once at scan time

profile: pluck

output:
  name: MMS_Warm_Bass_A
```

The `source.path` points at a single WAV (not a folder) — that's the marker that says "this is a single-file source, use `note` as the root".

## `scan-oneshots` options

| Option | Default | What it does |
|---|---|---|
| `--profile pluck\|synth\|pad\|drums` | auto | Override the auto-picked profile. |

## When to use this vs `scan-library`

- If every file will become its own preset → `scan-oneshots`.
- If files share notes (RR layers, multiple velocities of the same instrument, per-note captures of one patch) → `scan-library --type multisample` or `--type kit`.
- If the folder is a drum kit with instrument keywords in filenames → `scan-library --type drumkit`.

The name is the giveaway: if you see the same instrument at multiple notes (`Bass_A0.wav`, `Bass_A#0.wav`, `Bass_B0.wav`) that's a multisample. If you see many different sounds each at one note (`Bass_A.wav`, `Lead_C.wav`, `Pad_F.wav`) that's a folder of oneshots.

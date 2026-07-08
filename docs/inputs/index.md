---
title: Inputs
layout: default
nav_order: 3
has_children: true
permalink: /inputs/
---

# Inputs

patch-press can consume seven different kinds of source. Each has a dedicated scan command that walks the source, figures out what it's looking at, and writes one YAML config per resulting preset.

## Which command do I want?

| I have… | Structure | Command | Page |
|---|---|---|---|
| A **VST plugin** with presets | The plugin's GUI is the only way to reach them | `scan-from-probe` (after [patch-probe](vst-workflow.html)) | [VST workflow](vst-workflow.html) |
| A **CLAP plugin** with a preset directory | Loose `*.clap-preset` files on disk | `scan-clap` | [CLAP plugins](clap-plugins.html) |
| A **multisampled instrument** library | One subfolder per instrument; per-note WAVs inside | `scan-library --type multisample` | [Sample libraries](sample-libraries.html) |
| A **drum kit** organized by instrument | One subfolder per drum (kick, snare…); RR layers inside | `scan-library --type kit` | [Sample libraries](sample-libraries.html) |
| A **drum kit** in a flat folder | Loose one-shot WAVs; instrument encoded in filename | `scan-library --type drumkit` | [Sample libraries](sample-libraries.html) |
| A folder of **single-note oneshots** | Each WAV is its own preset (Monosounds-style) | `scan-oneshots` | [Oneshots](oneshots.html) |
| **Serum-format wavetables** | Each WAV = one wavetable; 2048-sample cycles | `scan-wavetables` | [Wavetables](wavetables.html) |
| A **bag-of-hits** drum library | Instrument categories × shared "flavor" subfolders | `assemble-kits` | [Kit assembly](kit-assembly.html) |

## The rough shape of every scan

Every scan command does roughly the same thing:

1. **Walk the source** — find the files/presets, group them into what will become presets.
2. **Look at each one** — parse filenames, render a probe note, run spectral analysis, whatever the source needs.
3. **Pick a profile** — `synth`, `pad`, `pluck`, or `drums` (see [profiles](../config-reference.html#profiles)) based on what it detected.
4. **Write a YAML config** — one file per preset, into the `config-dir` you pointed it at.

You then run `patch-press batch "config-dir/*.yaml"` to actually export the presets.

The YAML files are the seam between "figuring out what you've got" (scan) and "producing Deluge files" (batch). If the scan misidentified something — wrong profile, wrong root note, wrong root file — you can hand-edit the config and re-run just `sample` on that one, no re-scanning needed.

## When scan writes a REVIEW flag

Several scan commands can flag a config as REVIEW (in a comment at the top of the YAML) when they're not confident:

- **`scan-from-probe`** / **`scan-clap`** — low sustain-classification confidence, or the probe classifier disagreed with itself.
- **`scan-oneshots`** — no clear fundamental detected in the pitch tracker.
- **`scan-wavetables`** — file length isn't a clean multiple of 2048 samples.
- **`scan-library --type drumkit`** — every filename landed in the same instrument category (probably not a real kit).
- **`assemble-kits`** — a category fell back beyond its own flavor to `Various` or "any file".

REVIEW configs still get written; they're not skipped. The flag is a nudge to eyeball the config before running `batch`, not a hard failure.

---
title: Outputs
layout: default
nav_order: 6
has_children: true
permalink: /outputs/
---

# Outputs

patch-press's pipeline is deliberately **format-agnostic**. Everything up to the final export stage — trim, envelope, pitch, loop, normalize — produces a plain `SampleSet` that contains audio buffers, note assignments, and loop points, with no idea what device it's going to end up on.

The `--format` flag on `sample` and `batch` picks a target-specific exporter that turns that `SampleSet` into files on disk in whatever layout that device expects.

The one place the target leaks upstream is *how many notes get captured*. Formats differ in how much of a melodic capture they can actually use — the Deluge maps every note to its own keyzone, a `.pti` has no keyzones and ships one repitched sample — so rendering a full note grid for the latter pays for renders the exporter discards. Each exporter declares its appetite via `notes_used(notes)`, and `runner/pipeline.py:notes_to_capture` consults it before a VST/CLAP capture starts. The analysis stages stay untouched and format-blind; only the size of the note grid changes. A new exporter that omits `notes_used` simply gets the full grid.

## Currently supported targets

| `--format` | Target | Docs |
|---|---|---|
| `deluge` | [Synthstrom Deluge](https://synthstrom.com/product/deluge/) — XML preset + WAVs in the SD-card layout | [Deluge output](deluge.html) |
| `pti` | [Polyend Tracker](https://polyend.com/tracker/) family — self-contained `.pti` instruments | [Polyend Tracker output](polyend.html) |

Deluge is what shipped first because it's the device patch-press was built for. The pipeline architecture is set up to add more targets over time — anything with a defined preset format is a candidate.

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

## Currently supported targets

| `--format` | Target | Docs |
|---|---|---|
| `deluge` | [Synthstrom Deluge](https://synthstrom.com/product/deluge/) — XML preset + WAVs in the SD-card layout | [Deluge output](deluge.html) |

Deluge is what shipped first because it's the device patch-press was built for. The pipeline architecture is set up to add more targets over time — anything with a defined preset format is a candidate — but the current release only implements the Deluge exporter.

If your target is Deluge, keep reading in [Deluge output](deluge.html) for the full SD-card layout, XML structure, and pad ordering details.

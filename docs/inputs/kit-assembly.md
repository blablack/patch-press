---
title: Kit assembly
layout: default
parent: Inputs
nav_order: 6
---

# Kit assembly

Some drum libraries don't ship as kits at all. They ship as **browsing banks**: one big folder per instrument category, each subdivided by a descriptive taxonomy — "Clean", "Color", "Tape", "Various", etc. The idea is you dig through and pick your own kit. Samples From Mars ships their "Individual Hits" this way.

`assemble-kits` reads that structure and generates one kit per shared flavor tag automatically.

```bash
patch-press assemble-kits "~/samples/909_from_mars/Individual Hits" configs/909Assembled
```

## Expected structure

```
Individual Hits/
├── 01. Bass Drum/
│   ├── Clean/          ← files tagged "CLEAN"
│   ├── Color/          ← "COLOR"
│   ├── Tape/           ← "TAPE"
│   └── Various/        ← fallback pool
├── 02. Snare/
│   ├── Clean/
│   ├── Color/
│   ├── Tape/
│   └── Various/
├── 03. Hi-Hat/
│   ├── Clean/
│   ├── Color/
│   └── …
└── Cymbals/            ← categories with no flavor split are fine too
```

Flavors are discovered from **shared subfolder names**. A folder name only becomes a flavor tag if it appears under at least `--min-categories` different instrument families (default 2). This is what stops per-instrument idiosyncrasies (a lone "808-style" subfolder that only exists under Snare) from becoming spurious kits.

## What gets generated

For each shared flavor tag (Clean, Color, Tape, …), assemble-kits picks one file per instrument category:

1. **Tier 1** — a file from the category's own subfolder for this flavor.
2. **Tier 2** — if that flavor's subfolder is empty for this category, fall back to `Various/`.
3. **Tier 3** — if `Various/` is also empty, take any file from the category.

Tier 2 and Tier 3 fallbacks are marked in a REVIEW comment on the config so you know which pads to eyeball.

The resolved file list is written into `source.files` in the YAML **at scan time**. Later runs of `sample`/`batch` load that fixed list — they don't re-scan and re-pick. If assemble-kits chose a file you don't like for one pad, you can hand-edit `source.files` on that one line and never worry about a re-scan overwriting it.

## Config shape

```yaml
source:
  type: library
  path: /path/to/Individual Hits          # the library root, for reference
  drumkit: true
  files:
    - /path/to/Individual Hits/01. Bass Drum/Clean/BD_Clean_01.wav
    - /path/to/Individual Hits/02. Snare/Clean/SD_Clean_03.wav
    - /path/to/Individual Hits/03. Hi-Hat/Clean/HH_Clean_02.wav
    - …

profile: drums

output:
  name: 909_Clean
```

The `files` list is what actually drives export — the `path` is retained for provenance.

## `assemble-kits` options

| Option | Default | What it does |
|---|---|---|
| `--min-categories N` | 2 | Minimum distinct instrument families a subfolder tag must appear under to be treated as a flavor. Raise it if your library has many single-category subfolders you want ignored. |

## Scope

`assemble-kits` only fires when the flavor taxonomy lives in **subfolder names**. Libraries that put descriptive words inside flat filenames (with no shared subfolder structure) aren't handled by this command — that would need audio-feature clustering which patch-press doesn't do. If your library is flat + keyword-tagged, use [`scan-library --type drumkit`](sample-libraries.html#--type-drumkit) instead.

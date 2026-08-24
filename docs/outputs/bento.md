---
title: 1010music Bento output
layout: default
parent: Outputs
nav_order: 3
---

# 1010music Bento output

`--format bento` exports [1010music Bento](https://1010music.com/) patches. A Bento patch is a *folder*, not a file: a `patch.xml` describing one track, with the WAVs it references sitting next to it and named without any path.

```
patch-press sample config.yaml --path /media/BENTO/MyPlugin --format bento
patch-press batch 'configs/*.yaml' --path staging/Bento/MyCollection --format bento
```

Unlike the `.pti` export, the Bento has real multisample keyzones, so a melodic capture ships **every** note it recorded — the same fidelity as the Deluge export.

## Layout

`--path` is `<card-root>/<collection>`, the same convention the Deluge export uses. Presets are filed under `UserPatches/`, the user-content root the firmware hardcodes; the read-only factory bank in `Patches/` is never touched.

```
<card-root>/
└── UserPatches/
    ├── SampInst/                     ← melodic multisample patches
    │   └── <collection> - <subfolder…> - <Preset Name>/
    │       ├── patch.xml
    │       └── *.wav
    └── OneShots/                     ← drum kits
        └── <collection> - <Kit Name>/
            ├── patch.xml
            └── *.wav
```

`SampInst` and `OneShots` are two of the seven patch-type roots the firmware knows (`Granular`, `Loops`, `OneShots`, `SampInst`, `Slicer`, `Shredder`, `Wavetable`) — the type folder is what tells the device which engine a patch belongs to, so it is not optional.

**The patch folder is exactly one level deep — no nesting.** Every patch on a real card (`Patches/SampInst/Afternoon Raver`, `Patches/OneShots/1010 Bento Kit`, …) sits directly under its type root; there is no category subfolder. A folder one level deeper is still visible in the device's own file browser — it's just a directory — but the patch loader never resolves a `patch.xml` that isn't a direct child of `SampInst`/`OneShots` (confirmed on hardware: patches nested a level deeper showed up but silently failed to load). So the collection and any `output.subfolder` are folded into the one folder name instead of separate nested folders, joined with ` - `.

## How presets map onto a Bento patch

| Preset | Mapping |
|---|---|
| Multisample (synth/pad/pluck) | A `multisamtrack`: one `saminst` instrument cell plus one `samasst` per note. Each sample keeps its own root note and gets a keyzone **centred** on it — every boundary sits midway between two neighbouring roots, so a key is repitched by at most half the capture step in either direction, and the outermost zones stretch to 0 and 127 so the whole keyboard sounds. Loop points ship as frame indices. Velocity and round-robins collapse to one sample per note (velocity closest to 100, then lowest RR). |
| Drumkit | A `samtrack` of **16 pads**, each pad its own `saminst`/`samasst` pair addressed by `celldisppos`, one-shot triggered and unlooped. Pads land in the canonical kick → snare → hats → clap/perc → cymbals → toms → congas order, the same order the Deluge kit export uses. |
| Wavetable | **Not supported** — see below. The config exports fine with `--format deluge` or `--format pti`. |

Samples ship exactly as the pipeline produced them: no resampling, no downmix, no bit-depth change. Factory patches contain mono and stereo, 16- and 24-bit, 44.1 and 48 kHz WAVs in every combination, so there is nothing to convert to.

## Why wavetables aren't exported

The Bento's wavetable engine (`wttrack`) picks its table with a `wavesel` attribute, and `wavesel` is a **0-based index into a fixed list of 103 table names compiled into the firmware** — not a reference to the file the patch also names. Across all 130 wavetable cells in the factory bank that index matches the referenced filename exactly, with no exceptions, so the oscillator is choosing from a closed set.

A user table has no index to claim. A patch built from a patch-press wavetable would name a WAV the oscillator cannot select, and would silently play a factory table instead. So `--format bento` raises on a wavetable config rather than shipping something that looks right and sounds wrong:

```
ERROR  Wavetable_27: the Bento can't play user wavetables. Its wavetable oscillator
picks a table by `wavesel`, an index into the 103 tables built into the firmware…
```

In a `batch` run that's one error line per wavetable config; everything else in the batch still builds.

## Caveats

**Kits are capped at 16 pads.** Every factory kit has exactly 16, addressed by `celldisppos` 0–15. When a kit has more, the survivors are picked **round-robin across instrument categories** rather than by taking the first 16 in canonical order — a 32-pad kit with four kicks and four snares would otherwise spend every slot before reaching a cymbal. Each category places its first pad before any places its second, and the survivors go back into canonical order for the pad layout. The dropped pads are named in a warning:

```
01. Classic Multi Kit: the Bento has 16 pads, kit has 32 — dropping BD Clean Vinyl 09, …
```

**Looping is per-instrument, not per-sample.** A `multisamtrack` has one `loopmodes` flag for the whole patch, while patch-press decides looping per note. When a set is mixed, the majority wins and the minority is logged:

```
Vibey Lead: 2 of 3 notes have loop points but the Bento loops per-instrument — shipping loopmodes=1
```

Turning looping off would cost the looped notes their sustain; turning it on makes an unlooped note repeat its whole length. Notes that disagree with the chosen mode get a whole-sample loop, which is what the factory bank itself does for the odd unlooped note in a looped instrument.

**No `preview.wav` and no tag index.** Factory patches carry a `preview.wav` for the browser and an entry in `Patches/patchindex.xml` giving them tags (Bass, Pad, Keys…). Neither is written: the preview is the device's own to generate, and rewriting the card's tag index from a build is more invasive than an export should be. Presets browse and load fine without them; they just won't show up under a tag filter.

## What the format is based on

Nothing here is guessed from a wiki. The mapping was read off a real card's factory content and cross-checked against the strings compiled into the device firmware (`bento1.bin`):

- The `UserPatches\<Type>` roots, the cell and attribute names, and the `Wavetables must be mono WAVs` constraint all appear verbatim in the firmware.
- `samlen`, `loopstart` and `loopend` are frame indices — verified against the actual `data` chunk sizes of factory samples, not inferred.
- Attribute names, their order, and the cell sequence in a generated patch are checked to match the factory `SampInst`/`OneShots` patches exactly.

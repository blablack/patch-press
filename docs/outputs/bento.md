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
    ├── patchindex.xml                ← tags, for the browser's filter
    ├── SampInst/                     ← melodic multisample patches
    │   └── SFM VS Keys Pads - Polaris Space Delay Soft D2/
    │       ├── patch.xml
    │       └── *.wav
    └── OneShots/                     ← drum kits
        └── SFM VDM - 14 DDD1 Kit/
            ├── patch.xml
            └── *.wav
```

`SampInst` and `OneShots` are two of the seven patch-type roots the firmware knows (`Granular`, `Loops`, `OneShots`, `SampInst`, `Slicer`, `Shredder`, `Wavetable`) — the type folder is what tells the device which engine a patch belongs to, so it is not optional.

**The patch folder is exactly one level deep — no nesting.** Every patch on a real card (`Patches/SampInst/Afternoon Raver`, `Patches/OneShots/1010 Bento Kit`, …) sits directly under its type root; there is no category subfolder. A folder one level deeper is still visible in the device's own file browser — it's just a directory — but the patch loader never resolves a `patch.xml` that isn't a direct child of `SampInst`/`OneShots` (confirmed on hardware: patches nested a level deeper showed up but silently failed to load). So the collection and any `output.subfolder` are folded into the one folder name instead of separate nested folders.

## Naming: the folder name is the whole interface

There is no display-name field in `patch.xml` — a factory patch names its track `cellname="Track 1"` and nothing else. So the folder name is simultaneously the label the browser prints, the key it sorts a single flat list by, and the only thing distinguishing one patch from the next. Factory names are built for that: 9.5 characters on average across the 65 `SampInst` patches, never more than 18.

Spelling every level out in full does not survive contact with a real library. `Samples from Mars` alone produces 1242 presets, and joined naively they read:

```
Samples from Mars - Vinyl Synths from Mars - 02 Keys & Pads - Polaris Space Delay Soft Vinyl Synths D2
```

102 characters, of which the first 61 are identical for hundreds of neighbours — on a truncating display they are one indistinguishable block. So each level is shortened to a **short, stable label**:

```
SFM VS Keys Pads - Polaris Space Delay Soft D2
```

Across the ~2200-preset corpus that takes the median name from 57 characters to 31 and the longest from 102 to 65, with no two presets colliding. Four rules do it, in `_flat_patch_folder`:

| Rule | Effect |
|---|---|
| Ordering prefixes go | `01. Bass` → `Bass`, `1 BASS` → `Bass`. Capped at two digits, so `808 From Mars` and `2600 From Mars` keep theirs. |
| Shouting is normalised, acronyms are not | `1 BASS` → `Bass`, but `Vinyl SP from Mars` and `06. FX` keep `SP` and `FX` — only all-caps words of 4+ characters are recased, because `Sp` and `Fx` read like typos. |
| A repeated collection is said once | `Samples from Mars` + `Vinyl Synths from Mars` → `SFM Vinyl Synths`, not `SFM Vinyl Synths from Mars`. |
| Long multi-word labels become initials | `Vinyl Synths` → `VS`, `Fernando's hardware factory` → `FHF`. Labels of 11 characters or less stay whole (`Dream Synth`, `Third Party`), and a single token is never abbreviated — `BrontoScorpio` would collapse to `B`. |

On top of that the preset's own name loses whatever the prefix already says: a library stamps itself onto every file it ships, so `Orchestral Brass Full Ensemble Marcato` inside the `Orchestral Brass` folder becomes `Full Ensemble Marcato`, and `MS20 Fuzz Mod Vinyl Synths C0` becomes `MS20 Fuzz Mod C0`. A run of two or more words counts as an echo wherever it sits — note that last one carries the folder name *and then* the root note, so matching only the head and tail would miss it. A single word only counts at the head or the tail, so `HS Bass Nine` under `1 BASS` keeps its name: it is naming itself, not repeating the folder.

**The prefix never looks at the preset name.** It is a pure function of the collection and `output.subfolder`, because it is the only thing holding a bank together in one flat alphabetical list — deriving any part of it from the preset name would file `HS Bass Nine` and `HS Boomer` under different headings. For the same reason each label is shortened on its own length alone, never against a budget for the joined prefix: that would render the same bank as `Vinyl Synths` under one category folder and `VS` under a longer-named sibling, scattering it through the list.

The 2200-preset corpus lands in 153 such prefixes — `Diva Bass`, `SFM VS Leads`, `WAE Bass Sustained`, `OT Woodwinds`, `SFM EW Drums MPC60` — which is what a flat card gets in place of folders.

**Shortening can in principle collide** where the full path could not, so `patch-press batch` checks the whole set before it builds and logs a warning naming both configs if two would claim the same folder. The current corpus produces none.

## Tags: the only filter a flat card gets

Sorting a name-ordered list only takes you so far across 2200 presets. The one other handle the device offers is the browser's **tag filter**, and `--format bento` fills it in: every patch it writes also gets an entry in `UserPatches/patchindex.xml`, the index the firmware's `PatchMetadataFile::LoadMetadata` reads.

**The vocabulary is closed — 15 tags, and you cannot add one.** `bento1.bin` holds a 17-entry pointer table at file offset `0x14e460`: `All`, then the 15 tag names alphabetically, then `User`. Each of those 15 strings is referenced exactly once in the entire binary, from that one table, and the only tag-related UI strings in the firmware are `Patch Tagger` and `Instrument Tags` — there is no "new tag" anywhere. A tag invented outside the list was confirmed ignored on hardware.

```
Atmosphere  Bass  Drum  Foley  Guitar  Keys  Lead  Orchestral
Organ  Pad  Percussion  SFX  Strings  Synth  Vocal
```

The index file itself, though, is an ordinary file the device honours whether it or a build wrote it (confirmed on hardware).

**Tags come from the library's own labels, not from audio analysis.** Sample libraries categorise themselves better than any classifier would — `01. Bass`, `03. Leads`, `Orchestral Woodwinds`, `Distant Choir`, `Vinyl Drums` — so `bento_index.py:derive_tags` reads that vocabulary. Sources are consulted in order of how much they were trusted: the deepest source folder first (the library's own category), then shallower ones, then the collection, then the preset's own name. Run-together names are split on their capitals first, so `CalmBell` and `SubOsc` read as two words each — device names like `MS20` and `VP330` have no such boundary and stay whole. A drum kit is a `Drum` before anything else. Up to three tags are kept, matching where the factory bank's own tagging sits (179 of its 236 patches carry two or three). Anything with no signal at all lands on `Synth`.

Over the ~2200-preset corpus that produces:

| | | | | |
|---|---|---|---|---|
| Synth 1076 | Bass 389 | Drum 385 | Keys 229 | Lead 197 |
| SFX 145 | Atmosphere 126 | Orchestral 125 | Pad 122 | Percussion 83 |
| Strings 61 | Organ 37 | Vocal 33 | Guitar 12 | Foley 6 |

**Writing the index is safe to repeat and safe to share.** Each export does a locked read-modify-write — `build_presets.py --jobs K` runs several `batch` processes over one output tree at once, and they all claim this one file. Entries the run didn't produce are read back and preserved, so a resumed build, a second collection, or a tag you set on the device by hand all survive. If the file can't be written the export still succeeds: an untagged patch browses and loads normally.

**One caveat when staging.** Building into `output/Bento/` puts the index at `output/Bento/UserPatches/patchindex.xml`, and it only knows about patches in that tree. `rsync`ing it onto a card overwrites the card's index, taking any tag you set on the device with it. Either build straight to the card (`--path /media/BENTO/<collection>`), so the merge happens against the real index, or hold the file back:

```
rsync -a --exclude patchindex.xml output/Bento/ /media/BENTO/
```

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

**The loop crossfade is the device's job, not ours.** For every other target patch-press bakes the loop crossfade into the audio: the Deluge and the Tracker hard-wrap at the seam and have no fade of their own, so it has to be in the samples. The Bento has `loopfadeamt` — the front panel's `Loop Fade` — so `--format bento` ships the WAVs **exactly as captured** and hands the device the job instead.

That's deliberate, and the reason isn't just avoiding a double fade. A baked crossfade is permanent: once it's on the card there is no route back to the unfaded audio, and a seam that turns out too long or too short means rebuilding the preset. Leaving it to `loopfadeamt` keeps it a knob you can turn on the instrument, on the spot, per patch.

The exporter declares this with `bakes_loop_crossfade()`, the same optional-classmethod shape as `notes_used`; `runner/pipeline.py` reads it and passes `bake_crossfade=False` into `analyze_sampleset`. The analysis still works the length out per note and records it in `analysis["loop_crossfade_ms"]` — it just doesn't touch the audio.

**An authored loop gets no fade at all.** A library WAV's `smpl` chunk or a Bitwig zone's own loop is a seam the author already made clean, and fading it would only smear the join they chose — which is exactly why the pipeline ships those verbatim. Those patches get `loopfadeamt=0`. Only a loop this pipeline *detected* asks the device to fade. Like `loopmodes` this is one value for the whole instrument, so the majority decides.

**The starting value is 200, and it is meant to be changed.** The firmware's parameter table gives `loopfadeamt` a name (`Loop Fade`) but no units, and the factory bank is almost no help: 3 of the 481 cells carrying the attribute are non-zero — 100, 200 and 299 — because factory samples are looped clean by hand. 200 is what `SampInst/SciFi` uses, a 109-sample looped multisample and the closest factory analogue to what this exporter produces. It is a starting point on a knob, not a derived value; `_LOOP_FADE` in `io/exporters/bento.py` moves it.

**One thing is still baked.** A Bitwig `.multisample` zone carries its own `fade` length, and `io/adapters/bitwig.py` applies it while slicing — before any exporter is involved, because an adapter doesn't know the output format. That one is the source file's own instruction rather than a patch-press decision, so it is reproduced rather than deferred to the device.

**No `preview.wav`.** Factory patches carry one for the browser. It isn't written — that's the device's own to generate — and presets browse and load fine without it.

## What the format is based on

Nothing here is guessed from a wiki. The mapping was read off a real card's factory content and cross-checked against the strings compiled into the device firmware (`bento1.bin`):

- The `UserPatches\<Type>` roots, the cell and attribute names, and the `Wavetables must be mono WAVs` constraint all appear verbatim in the firmware.
- `samlen`, `loopstart` and `loopend` are frame indices — verified against the actual `data` chunk sizes of factory samples, not inferred.
- Attribute names, their order, and the cell sequence in a generated patch are checked to match the factory `SampInst`/`OneShots` patches exactly.

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
    │       ├── preview.wav           ← what the browser plays when you audition it
    │       └── *.wav
    └── OneShots/                     ← drum kits
        └── SFM VDM - 14 DDD1 Kit/
            ├── patch.xml
            ├── preview.wav
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

A third case: tooling that ships a *curated subset* of a build rather than the whole tree (a fixed patch list smaller than everything `sample`/`batch` produced) can't use either — the whole source index would tag patches never copied to the card, and excluding it throws away tags for the patches that *were*. `bento_index.sync_index(source_index, dest_index, wanted)` merges just the `wanted` patch paths from the build's index into the card's, under the same locked read-modify-write as `update_index`, leaving every other entry alone. `prepare-sd-cards/bento.py`'s `sync_patches()` is the example caller.

## How presets map onto a Bento patch

| Preset | Mapping |
|---|---|
| Multisample (synth/pad/pluck) | A `multisamtrack`: one `saminst` instrument cell plus one `samasst` per note (or per velocity layer within a note — see below). Each sample keeps its own root note and gets a keyzone **centred** on it — every boundary sits midway between two neighbouring roots, so a key is repitched by at most half the capture step in either direction, and the outermost zones stretch to 0 and 127 so the whole keyboard sounds. Loop points ship as frame indices. Round-robins always collapse to one sample per (note, velocity layer) — lowest RR, since nothing points to Bento having a round-robin engine. Velocity is different: `SampInst` has a genuine velocity-zone engine, confirmed against the factory bank (11 of 65 patches ship real per-note velocity zones, up to 4 layers, e.g. "Medium Muff"), so a source that captures more than one velocity per note ships every layer as its own `samasst`, with the same centering used for keyranges applied to velocity (boundaries at the midpoint between neighbouring layers' velocities, outermost zones stretching to 0/128 — verified to reproduce Medium Muff's own zone boundaries exactly). `velroot` is that layer's own captured velocity. Today only a Bitwig `.multisample` source can supply more than one velocity per note (`keep_velocity_layers` on the adapter, gated by `BentoExporter.keeps_velocity_layers()`); every other source still produces one velocity per note and collapses to the pre-existing single full-range zone (`velrangebottom=0`/`velrangetop=128`/`velroot=63`) with no change in output. |
| Drumkit | A `samtrack` of **16 pads**, each pad its own `saminst`/`samasst` pair addressed by `celldisppos`, one-shot triggered and unlooped. The physical grid is two rows of 8 (`celldisppos` 0-7 top, 8-15 bottom), and pads land by physical position, not canonical list order (unlike the Deluge kit export, which is still document-order kick → snare → …): kick(s) then snare(s) fill the bottom row from the left, hi-hat(s) fill the top row from the left, and everything else (clap/perc, cymbals, toms, congas) fills whatever's left over, in the same canonical adjacency order — see `_pad_positions` in `io/exporters/bento.py`. |
| Wavetable | A `wttrack` under a third root, `UserPatches/Wavetable/`. The table WAV ships in the patch folder and the oscillator names it — see below. |

Samples ship exactly as the pipeline produced them: no resampling, no downmix, no bit-depth change. Factory patches contain mono and stereo, 16- and 24-bit, 44.1 and 48 kHz WAVs in every combination, so there is nothing to convert to. A library sample the analysis never modified goes further and is copied byte-for-byte from the vendor's own file — see [Shipping the vendor's own file](../inputs/sample-libraries.html#shipping-the-vendors-own-file).

## Wavetables

A wavetable config exports to a `wttrack` patch under `UserPatches/Wavetable/`, built
from the same archetype analysis that drives the Deluge export
([Wavetables](../inputs/wavetables.html)). The table WAV sits in the patch folder and
the oscillator cell names it, exactly the way a sample cell names its sample:

```xml
<cell type="wavetable">
  <params pitch="-60" level="1000" wavesel="0" wavepos="0" samlen="0" filename="111.WAV"/>
  <modsource dest="wavepos" src="lfo1" slot="0" amount="450"/>
</cell>
```

**The table must be mono.** The firmware rejects anything else outright with
`Wavetables must be mono WAVs.` — a stereo source is downmixed and truncated to whole
2048-sample windows on the way out, the same narrowing the Deluge needs. A mono file is
copied byte-for-byte.

### What `wavesel` is, and why it isn't the table

The firmware carries a 103-entry catalogue of stock table names — display names at
`0x660f8`, filenames at `0x664f0` — and `wavesel` is a 0-based index into it. That
index agrees with the cell's own `filename` in all 130 factory wavetable cells, which
is what makes the catalogue easy to mistake for the source of the audio.

It isn't. The firmware holds the *names*, not the samples: 103 tables at ~3 MB each is
roughly 300 MB against a 1.4 MB binary, and there is no wavetable library folder
anywhere on the card. Every factory patch ships its own byte-identical copy of each
table it uses — `AyEeAyeOh.wav` appears three times on the card, once per patch that
plays it. So even a factory patch can only be reading the WAV beside its own
`patch.xml`, and `filename` is what selects the audio. The firmware's load path is
file-based to match: `Double-tap to load WAV`, `No WAV`, `Invalid Wavetable`,
`Wavetables must be mono WAVs.`

A user table therefore needs no catalogue entry. `wavesel` is written as `0` — a valid
index (entry 0 is `AEAHOHOOo.wav`), so the picker always has something in range to
display, rather than a sentinel that could trip `Invalid Wavetable` and cost you the
patch. It names a table the patch does not play.

### The patch around the table

The cell order is the factory bank's and does not vary: two wavetable cells, an analog
osc, two filters, two envelopes, two LFOs, the modulation sequencer, the part
parameters, then the effects. All 66 factory wavetable patches carry all of them, so
all of them are written even where this patch leaves them neutral.

- **Both wavetable cells play the one table**, detuned ±6 cents with the position LFO
  inverted on the second. That is what the 37 single-table factory patches do
  (`Bigbrute` runs its two cells 11 cents apart with `wavepos` modulated +481/−503).
- **The analog oscillator is silenced** (`level="0"`). The preset is the table.
- **Envelope 1 is the amp envelope**, implicitly — it needs no `modsource`, so the
  archetype's ADSR goes there and envelope 2 stays neutral and unrouted, matching what
  the Deluge export does with the same analysis.
- **LFO 1 sweeps the table.** The analysis calls that modulator `lfo2` because that is
  the LFO the Deluge uses for it; on the Bento the factory bank drives `wavepos` from
  LFO 1 in 93 cells against LFO 2's 13, so it lands on LFO 1 here.
- `wavepos` is a scan position over the table on a 0–255 scale, not a window index.

## Caveats

**Kits are capped at 16 pads.** Every factory kit has exactly 16, addressed by `celldisppos` 0–15. When a kit has more, the survivors are picked **round-robin across instrument categories** rather than by taking the first 16 in canonical order — a 32-pad kit with four kicks and four snares would otherwise spend every slot before reaching a cymbal. Each category places its first pad before any places its second, and the survivors go into canonical order first, which `_pad_positions` then remaps onto the physical grid (kick/snare bottom row, hi-hat top row, everything else filling the leftover slots). The dropped pads are named in a warning:

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

## The browser preview

Every patch folder gets a `preview.wav`: the clip the patch browser plays when you tap **Preview** to audition a patch without loading it.

**The device makes these, but only on its own save path.** Saving or importing a patch *on the hardware* triggers a note and records the output in real time — the 1.3 release notes flag it as a known issue ("you are still hearing the sound triggered to create the preview file"). A patch folder written by a build tool never goes through that path, so it never gets one, and its row in the browser is silent. All 236 factory patches ship a preview; nothing this exporter wrote had one until it started writing them. Re-saving a few thousand patches by hand on the device is not a route, so `--format bento` renders the clip offline instead.

**The format is copied from the factory bank, not guessed.** It is invariant across all 236 factory previews, and generated files match it byte-for-byte in the header and to the byte in total size:

- RIFF PCM, **stereo, 48 kHz, 24-bit** — no exceptions anywhere in the factory bank.
- Chunks `fmt `(16) + `JUNK`(460 zero bytes) + `data`, which puts the audio payload on byte 512. That sector alignment is the fingerprint of the device's own streaming recorder: factory *sample* WAVs are plain `fmt`+`data` at `0x2c`, so the previews demonstrably come from a different writer. The padding does nothing for playback, but matching the layout removes a variable.
- Exactly **4.000 s** for the three roots this exporter writes. (The factory bank uses 10 s for `Slicer` and 16 s for `Loops`, neither of which patch-press produces.)
- Referenced by nothing — not `patch.xml`, not `patchindex.xml`. The loader finds it by name, beside `patch.xml`.

**What's in the clip follows what the factory previews contain**, measured the same way. A factory multisample or wavetable preview is one note held for the whole four seconds (a single onset, sound from 0.00 to 4.00); a factory kit preview is a handful of pad hits (3 to 13 across the 26 kits). So:

| Patch | Preview |
|---|---|
| `SampInst` | the sample nearest middle C, at its own root pitch, sustained to 4 s through its own loop points |
| `OneShots` | the kit's pads struck in order, spread evenly across the 4 s, tails ringing on under each other |
| `Wavetable` | a synthesised approximation of the patch (see below) |

A multisample whose chosen sample has no loop simply ends where it ends and the clip runs out into silence — which is what the device does with a one-shot too. A kit preview auditions the pads the patch *shipped*, after the 16-pad thinning, so it never plays a hit that isn't on the card.

**A wavetable preview is an approximation, and deliberately so.** The other two are built from the very audio in the patch folder, so they are what the device will play. A wavetable's sound comes from the device's own oscillator, and there is nothing to copy — so `bento_preview.py` synthesises it from the same `WavetableAnalysis` the patch was written from, mirroring `_build_wavetable`: two oscillators detuned ±6 cents reading the one table, position swept by an LFO inverted on the second voice, one lowpass, one ADSR. Verified to land on middle C (261.8 Hz measured) with the table position genuinely moving. It is meant to be recognisable in a browser list, not to match the hardware sample for sample. The fraction-to-Hz/seconds mappings that requires (`_ENV_MAX_S`, `_LFO_HZ`, `_CUTOFF_HZ`) are plausible ranges, **not derived** — the Bento's parameter scales are unitless 0–1000 integers in the firmware, so there is nothing to read them off. Same status as the archetype thresholds in `analysis/wavetable.py`: tune by ear.

**Previews are levelled; the factory ones are not.** Factory clips peak anywhere from 0.43 to 1.0 because they are recordings of a device whose output gain is already set. A corpus of thousands of library and rendered presets has no such common reference, and a preview too quiet to hear does not do its job, so each one is peak-normalised to 0.89.

**A failed preview never fails an export.** It is best-effort, like the tag index: a patch with no `preview.wav` is silent in the browser but browses and loads exactly as before — which is precisely the state every patch built before this existed is in.

**Cost.** 1,152,512 bytes per patch, always — the format is fixed-length, so a mono kit costs the same as a stereo pad. Around 3.9 GB across a ~3,500-preset card.

**Patches built before this existed can be backfilled without rebuilding them.** A resumed build won't add previews on its own — `run_batch` skips a preset when any of its `expected_outputs` exists, and `patch.xml` still does — and `batch --no-skip` would re-render every VST/CLAP preset just to gain a 4-second clip. It isn't necessary: a patch folder already contains everything a preview needs. The WAVs sit next to `patch.xml`, and `patch.xml` carries the loop points, the root notes, the pad order and the wavetable's whole oscillator/filter/envelope setup. So `debug_scripts/backfill_bento_previews.py` walks an existing tree — a build directory or a card — reconstructs just enough of a `SampleSet` per patch, and calls the same `write_preview` the exporter calls:

```
.venv/bin/python debug_scripts/backfill_bento_previews.py output/Bento --jobs 8
.venv/bin/python debug_scripts/backfill_bento_previews.py /run/media/you/BENTO --jobs 8
```

It skips patches that already have one (`--force` to overwrite, `--dry-run` to just count), and only loads the WAV it actually needs — a 100-note multisample reads one file, not a hundred.

How close is a backfilled preview to a rebuilt one? Measured against a fresh export of the same presets:

| Type | Result |
|---|---|
| `SampInst` | **bit-identical** |
| `OneShots` | differs below **−80 dBFS** peak (−92 dB RMS) — the shipped WAV is 16-bit, the exporter mixed from the float in memory |
| `Wavetable` | same timbre and level (spectra within 1.2%, centroids equal to the Hz, RMS equal to 0.00 dB), different LFO phase — the sweep rate is read back from the patch's quantised 0–1000 integer, and a hair of rate difference is seconds of phase by the end of the clip |

None of that is audible; the kit case is below the noise floor of its own source file.

## What the format is based on

Nothing here is guessed from a wiki. The mapping was read off a real card's factory content and cross-checked against the strings compiled into the device firmware (`bento1.bin`):

- The `UserPatches\<Type>` roots, the cell and attribute names, and the `Wavetables must be mono WAVs` constraint all appear verbatim in the firmware.
- `samlen`, `loopstart` and `loopend` are frame indices — verified against the actual `data` chunk sizes of factory samples, not inferred.
- Attribute names, their order, and the cell sequence in a generated patch are checked to match the factory `SampInst`/`OneShots`/`Wavetable` patches exactly.
- `preview.wav`'s audio format, chunk layout and duration were measured across all 236 factory previews (identical in every one), and its role confirmed against 1010music's own documentation — the Quick Start Guide ships previews as content ("add the files needed to support Patch Previewing"), and the 1.3 release notes describe the device recording one at save/import time.

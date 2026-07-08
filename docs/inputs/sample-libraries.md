---
title: Sample libraries
layout: default
parent: Inputs
nav_order: 3
---

# Sample libraries

Downloaded sample packs come in three main shapes. `scan-library` handles all of them with a `--type` flag.

```bash
patch-press scan-library <library-root> <config-dir> --type multisample|kit|drumkit
```

## `--type multisample`

**One instrument, many notes.** Each subfolder is one preset; the WAVs inside cover the note range.

```
Mini_From_Mars/
├── Bass 01/
│   ├── Mini_Bass01_A0.wav
│   ├── Mini_Bass01_A#0.wav
│   ├── Mini_Bass01_B0.wav
│   └── …
├── Bass 02/
│   ├── Mini_Bass02_A0_0001.wav      ← round-robin 1
│   ├── Mini_Bass02_A0_0002.wav      ← round-robin 2
│   └── …
└── Pad Warmth/
```

Each subfolder → one config. The subfolder **name** doesn't matter to the analysis (the note is parsed out of each WAV's filename); it becomes the preset's `output.name`.

## `--type kit`

**One kit, many instruments.** Each top-level subfolder is one preset (a kit); each nested subfolder is one drum in that kit; the WAVs inside are round-robin layers.

```
Roland_909/
├── Kick/
│   ├── kick_01.wav
│   ├── kick_02.wav
│   └── kick_03.wav
├── Snare/
├── HH Closed/
├── HH Open/
└── Clap/
```

Each per-instrument subfolder becomes one pad in the exported kit. Filenames are used for round-robin ordering only; the drum name comes from the subfolder.

## `--type drumkit`

**One kit as a flat pile of WAVs.** Every file in the folder is a single drum hit; the instrument is identified from filename keywords. This is the layout that Samples From Mars uses for the "808 From Mars", "909 From Mars", etc. kit folders.

```
808_From_Mars/
├── 01.BD.wav
├── 02.SD.wav
├── 03.CH.wav             ← closed hat
├── 04.OH.wav             ← open hat
├── 05.CP.wav             ← clap
├── 06.TL.wav             ← tom low
├── 07.TM.wav
└── 08.TH.wav
```

Recognized keywords (case-insensitive, matched anywhere in the filename):

- **Kick** — `BD`, `KICK`, `KCK`
- **Snare** — `SD`, `SNARE`, `SNAR`
- **Hat closed** — `HH`, `HAT`, `CH` (without `OPEN`/`OH` modifier)
- **Hat open** — `HH OPEN`, `OH`, `HAT_OPEN`
- **Clap** — `CLAP`, `CP`
- **Rim/Sidestick** — `RIM`, `RIMSHOT`
- **Cymbals** — `CRASH`, `RIDE`, `CYM`
- **Toms** — `TOM` + optional `LOW`/`LO`/`HIGH`/`HI`/`MID` modifier
- **Congas / Bongos** — `CONGA`/`BONGO` + optional `LOW`/`HIGH`
- **Cowbell** — `COWBELL`, `CB`
- **Claves** — `CLAVES`
- **Maracas / Shaker** — `MARACA`, `SHAKER`

Unrecognized files land in an `other` bucket and become extra pads — they aren't dropped.

If **every** file lands in the same category (a folder of just kicks, say), scan flags the config REVIEW rather than shipping a one-pad "kit". Chromatic single-instrument folders are more often multisamples in disguise.

### Canonical pad order

The Deluge kit XML has no explicit note attribute — pad order is document order in the XML. patch-press sorts pads into the canonical order used by Synthstrom's own factory kits (TR-808, TR-909, R-100):

**kick → snare → hat_closed → hat_open → clap/rim → cymbals → toms (low→mid→high) → congas (low→mid→high) → other**

You get the same layout regardless of how the source files were named or ordered.

## Filename conventions (multisample & kit)

For `multisample` and `kit`, the note (and optional round-robin index) is parsed out of each WAV's stem — patch-press scans anywhere in the name.

| Filename | Parsed as |
|---|---|
| `A3.wav` | Note A3, RR 1 |
| `C#2.wav` | Note C#2, RR 1 |
| `Mini_Bass01_A#0.wav` | Note A#0, RR 1 |
| `C#2_0001.wav` | Note C#2, RR 1 |
| `Mini_Bass01_A0_0002.wav` | Note A0, RR 2 |

**Both accidentals work:** `A#3` and `Bb3` both mean MIDI 70.

**When numbered round-robins exist for a note, the unnumbered base is ignored.** If you have `Kick.wav` and `Kick_0001.wav` in the same folder, only the numbered one is used — `Kick.wav` is treated as a leftover preview file.

## `scan-library` options

| Option | Default | What it does |
|---|---|---|
| `--type multisample\|kit\|drumkit` | (required) | Library layout, see above. |
| `--profile pluck\|synth\|pad\|drums` | auto | Override the auto-picked profile. |
| `--note-step INT` | 3 | Only relevant when the library's note coverage is sparser than every semitone — patch-press keeps the closest sample per note. |
| `--start-note NOTE` | C1 | Ignore samples below this note. |
| `--end-note NOTE` | C6 | Ignore samples above this note. |

## Which type is this?

- **Every filename has a note in it** → `multisample`
- **Each top-level subfolder is one drum (kick/snare/…)** → `kit`
- **Flat pile of WAVs with `BD`/`SD`/`HH` in the names** → `drumkit`
- **Flat pile of WAVs, each is its own preset (a bass hit here, a bell hit there…)** → not `scan-library`; use [`scan-oneshots`](oneshots.html)
- **Files organized by instrument category × a shared "flavor" taxonomy** → not `scan-library`; use [`assemble-kits`](kit-assembly.html)

# SFZ and Kontakt instruments

Many sample libraries ship, next to their WAVs, one mapping file per instrument: an
**SFZ** (open plain-text format) or a **Kontakt `.nki`**. Either says outright what a
folder of WAVs can only hint at — root note, key and velocity range, authored loop points,
which L/R files are one stereo sound — so `scan-sfz` / `scan-kontakt` read it instead of
guessing from filenames. Bitwig `.multisample` works the same way and all three share one
adapter base.

```
patch-press scan-sfz     <folder> <config-dir>
patch-press scan-kontakt <folder> <config-dir>
```

One config per mapping file, found recursively; the folder position is mirrored into the
config tree and `output.subfolder`. Options: `--profile`, `--note-step`, `--strip-suffix`
(removed from every preset name, e.g. `" - Kawaii Dreams From Mars"`) and `--name-from-folder`
(name a preset after the WAV folder it plays from, when that folder is its alone — the name the
WAV-folder scan would give, so switching a library keeps its patch names).

## What is read

- **SFZ**: `<control>/<global>/<master>/<group>/<region>` inheritance, `#define`, `#include`,
  `default_path`, note names, `lokey/hikey/key/pitch_keycenter`, `lovel/hivel`, `offset/end`,
  loop opcodes (a loop mode without points falls back to the WAV's `smpl` loop),
  `volume`, `pan`. Paths with backslashes and wrong case resolve.
- **Kontakt**: Kontakt 2 – 4.1 instruments (zlib XML program) and Kontakt 5+ instruments
  (NI container with FastLZ-compressed sub trees and binary preset chunks, `io/kontakt5.py`).
  Zones, groups, loops and crossfades. The container layout and sample-path encoding follow
  ConvertWithMoss (LGPL-3.0). Checked against all 2,972 Kontakt 5+ NKIs of Samples From Mars
  and Wave Alchemy Essence (all parse), and against a pack's own SFZ (same zones, roots,
  ranges) and the WAVs' `smpl` loops (identical for ~92% of looped zones; the rest were
  re-edited in Kontakt).
- **Not read**: Kontakt 1, 4.2 (FastLZ), NI-protected (encrypted) instruments, NKM multis and
  monoliths. They are skipped with a printed reason; use the SFZ or WAV folders for those.
- Skipped on purpose: release-trigger regions/groups and keyswitched articulations.
- Hard-panned L/R zones on the same root/range are joined into one stereo sample.
- Recorded but not applied: per-zone tuning (`tune_cents`).

## Audio

Vendor masters ship **verbatim**: the generated configs set `pitch_verify`, `loop_detect`
and `trim` off and `normalize: none`, so a WAV that plays whole is copied byte-for-byte.
A level shared by every zone (Kontakt's default −6 dB instrument volume) is a mixer setting
and is not applied; only the balance between zones is. The loop points are the mapping's;
the mapping's crossfade is not baked into on-disk WAVs (the Bento has its own loop-fade
knob; the file's loop is the author's). Profile is `synth` when more than
half of the zones loop, otherwise `pluck`.

## Which source wins

prepare-sd-cards picks per pack: **Kontakt > SFZ > WAV folders**, and only a format that
parses in full with all samples present (`lib/mapped.py`).

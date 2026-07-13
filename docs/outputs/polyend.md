---
title: Polyend Tracker output
layout: default
parent: Outputs
nav_order: 2
---

# Polyend Tracker output

`--format pti` exports Polyend Tracker instruments — one self-contained `.pti` file per preset (a 392-byte header followed by the sample's 16-bit 44.1 kHz PCM). Works on the original Tracker, Tracker+ and Tracker Mini; stereo output needs a Tracker+ or Mini (the original plays it mono).

```
patch-press sample config.yaml --path /media/TRACKER/Instruments --format pti
patch-press batch 'configs/*.yaml' --path staging/Polyend/MyCollection --format pti
```

## Layout

Unlike the Deluge's SYNTHS/KITS/SAMPLES split, the layout is flat — one file per preset, with `output.subfolder` mirrored as directories:

```
<--path>/
├── <subfolder…>/
│   └── <Preset Name>.pti
└── <Kit Name>.pti
```

Copy the files anywhere on the Tracker's SD card and load them from the file browser, or place them under `Instruments/`.

## How presets map onto .pti

The `.pti` format has **no multisample keyzones** — one sample per instrument — so each preset type collapses differently:

| Preset | Mapping |
|---|---|
| Multisample (synth/pad/pluck) | **One sample**: the capture nearest MIDI 60 (tie-break: velocity closest to 100, lowest round-robin). Loop points are embedded (forward loop) and `tune` offsets the root back to C4 (`60 − note`, clamped ±24 with a warning). Detected attack is re-applied as the volume-envelope attack. |
| Drumkit | **One sliced instrument**: all pads concatenated in the canonical kick → snare → hats → … order with a slice marker per pad, playback mode *Slice*. The format caps at **48 slices**; extra pads are dropped with a warning. |
| Wavetable | Native wavetable mode: the file's 2048-frame windows ship byte-for-byte, with wavetable position, position-LFO, ADSR and low-pass cutoff translated from the scan's archetype settings. |

Losses vs. the Deluge export, by design: multisamples lose their keyzone spread (the device repitches one sample chromatically), kits lose per-pad velocity layers and round-robins (one hit per pad).

Stereo sources are written stereo (planar PCM — the device infers channel count from data size); true-mono sources stay mono, halving the file. Library WAVs at other rates are resampled to 44.1 kHz (soxr HQ) with loop points rescaled; a loop too short to survive the header's 1/65535-of-file position resolution ships as one-shot with a warning.

## Validation

`tools/inspect_pti.py` is an independent decoder (it shares no code with the exporter) that checks structure — magic, CRC, PCM size vs. header frames, loop/slice ordering, envelope and LFO ranges — and prints a summary per file:

```
.venv/bin/python tools/inspect_pti.py staging/Polyend/
```

The writer and validator were both verified byte-for-byte against the 139 device-saved instruments in the [jaap3/pti-file-format](https://github.com/jaap3/pti-file-format) test corpus (firmware 1.5.0b2): unused header regions are byte-identical to what the device itself writes, and the validator passes the full corpus.

## bulk builds

`tools/build_presets.py` takes a global `--format` flag (before the command). Output goes to `output/Polyend/` and build state is tracked separately from the Deluge build (`.build_state.pti.json`), so both targets can be built from one scan:

```
.venv/bin/python tools/build_presets.py --format pti build
.venv/bin/python tools/build_presets.py --format pti status
```

## On-device checklist (first load on real hardware)

Each of these is a one-constant fix in `src/patch_press/io/exporters/polyend.py` if it's wrong:

- [ ] A multisample preset plays at the right pitch at C4 (unity note / `tune` sign).
- [ ] A stereo preset plays in stereo on Tracker+/Mini (planar layout inference).
- [ ] A kit's slices trigger the right drums in order.
- [ ] A wavetable preset scans smoothly and the initial position sounds like the Deluge export (window index vs. fraction assumption at header offset 88).
- [ ] The wavetable position LFO speed feels comparable to the Deluge LFO2 (steps-enum mapping).

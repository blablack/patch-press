---
title: Config reference
layout: default
nav_order: 7
---

# Config reference

Every preset patch-press exports is described by a YAML file. Scan commands write these for you; the schema is documented here for when you need to hand-tune one.

A minimal config only has to specify **what to sample** (`source`) and **what to call it** (`output.name`). Everything else — capture range, analysis flags, normalization — has a default (either from the [profile](#profiles) you selected or from the code's built-in fallback).

## Anatomy

```yaml
profile: synth          # optional; merges profile defaults first
category: synth         # optional; usually set by the profile

source: …               # required — see below
capture: …              # optional overrides for capture range/timing
analysis: …             # optional overrides for the analysis pipeline
output:
  name: MyPreset        # required — used in filenames and XML

wavetable: …            # only present for wavetable presets
```

Load order: built-in defaults ← profile file ← config file. Every key in your config overrides the profile.

---

## `source`

Exactly one of the four source types.

### VST

```yaml
source:
  type: vst
  plugin: /path/to/Plugin.vst3        # required
  preset: "Warm Pad"                  # optional; documentation only when raw_state is set
  raw_state: "<base64 blob>"          # main path; captured by patch-probe
```

### CLAP

```yaml
source:
  type: clap
  plugin: /path/to/Plugin.clap        # required
  plugin_id: "com.example.plugin"     # required
  preset: "Warm Pad"                  # optional
  preset_path: /path/to/warm.clap-preset   # optional
  raw_state: "<base64 blob>"          # optional; if set, bypasses preset_path
```

### Library

```yaml
source:
  type: library
  path: /path/to/folder-or-file       # required
  filename_pattern: "regex"           # optional; override default note-parsing regex
  note: 60                            # optional; pin the MIDI root for a single-file source
  drumkit: false                      # true when path is a flat folder of drum one-shots
  files:                              # optional; explicit file list (kit assembly)
    - /path/to/hit1.wav
    - /path/to/hit2.wav
```

The `files` list is used by [`assemble-kits`](inputs/kit-assembly.html) — when set, the file list is the source of truth and `path` is retained for provenance.

### Wavetable

```yaml
source:
  type: wavetable
  path: /path/to/wavetable.wav
```

---

## `capture`

Controls how VST/CLAP plugins are played — how many notes, how loudly, for how long. Library sources ignore most of these (the library already IS the capture) but still use `tempo_bpm`.

| Key | Type | Default | What it does |
|---|---|---|---|
| `note_range` | `[int, int]` | `[36, 96]` (usually overridden by profile) | MIDI range to capture. |
| `note_step` | `int` | `3` | Semitones between captured notes. `1` = every note, `12` = every octave. Drums profile uses `1`. |
| `velocities` | `list[int]` | `[100]` | MIDI velocities to capture. Drums profile uses `[64, 100, 127]`. |
| `round_robins` | `int` | `1` | Number of consecutive captures per (note, velocity). Drums profile uses `3`. |
| `duration_s` | `float` | `4.0` | Note hold length in seconds. Pad profile uses `8`, pluck uses `2`. |
| `release_tail_s` | `float` | `2.0` | Post note-off recording time. Pad uses `5`. |
| `tempo_bpm` | `float` | `120.0` | Tempo the plugin is told; also used by loop / envelope BPM-snap detection. |
| `sample_rate` | `int` | `48000` | 44100 / 48000 / 96000. |
| `mono` | `bool` | `false` | Capture one channel instead of two. Set by the scan when the preset renders the same signal to both channels (see [CLAP plugins](inputs/clap-plugins.html#mono-presets-are-captured-in-mono)); flip it to `false` to force stereo. |

---

## `analysis`

Controls the [analysis pipeline](pipeline.html).

| Key | Type | Default | What it does |
|---|---|---|---|
| `trim` | `bool` | `true` | Enable silence trim. |
| `pitch_verify` | `bool` | `true` | Enable pitch verification. Drums profile sets this `false`. Auto-skipped for VST/CLAP regardless. |
| `pitch_tolerance_cents` | `float` | `50.0` | Max cents deviation before a sample is flagged out-of-tune. |
| `loop` | `bool` | `false` | Whether this preset loops at all. Enabled by `pad` and `synth` profiles. |
| `loop_detect` | `bool` | `true` | Whether loop points may be **derived** when the source carries none of its own. `false` = authored loops only (a WAV `smpl` chunk, a Bitwig zone); a sample without one ships as a one-shot. Written as `false` by the scan commands for any library that ships `smpl` loops — see [`--loop-detect`](cli-reference.html#scan-library). |
| `loop_use_tempo` | `bool` | `false` | Include BPM-synced candidates in the loop search. |
| `loop_quality_threshold` | `float` | `0.75` | Minimum score for a candidate to be accepted. |
| `loop_crossfade_ms` | `float` | *(unset)* | Crossfade duration at the loop seam, for **detected** loops. Unset (default) = **adaptive**: chosen per note from the fundamental period and the residual seam discontinuity (a fixed millisecond value is wrong across the register — too short to cover one cycle on bass, needlessly long on treble). Set a number to force a fixed length. Authored loops (WAV `smpl` chunks, Bitwig `.multisample`) ignore this and keep their own fade. |
| `tempo_bpm` | `float?` | none | Overrides `capture.tempo_bpm` for analysis-only tempo decisions (leave unset unless you want them different). |
| `normalize` | `str` | `per_set` | `per_sample`, `per_set`, or `none`. Scan commands write `none` (with `trim: false`) for melodic library sources, so their WAVs reach the card unmodified — see [`--audio`](cli-reference.html#scan-library). |
| `classify_drums` | `bool` | `false` | Run filename-keyword drum classification. Enabled by `drums` profile. |

---

## `output`

```yaml
output:
  name: MyPreset          # required
```

`output.name` is what the preset is called. It becomes the XML filename, the SAMPLES subfolder, and the display name inside Deluge browsers. Spaces and slashes are sanitized to underscores.

---

## `wavetable`

Only present in wavetable configs. Every 0–1 parameter is a plain fraction that the exporter maps into the Deluge's signed-32-bit param range.

| Key | Type | What it does |
|---|---|---|
| `archetype` | `str` | `pad` / `pluck` / `bass` / `lead` / `drone` / `evolving_pad` |
| `wt_position` | `float` | Starting frame index (0.0 = first, 1.0 = last). |
| `lfo2_rate` | `float` | LFO2 speed (drives the wavetable position sweep). |
| `lfo2_depth` | `float` | LFO2 modulation depth. |
| `filter_cutoff` | `float` | Initial low-pass cutoff. |
| `attack` / `decay` / `sustain` / `release` | `float` | Amp-envelope shape. |
| `filter_type` | `str` | `lpf` (only mode currently). |

The archetype's canned parameter block is what `scan-wavetables` writes; you can edit any field independently.

---

## Profiles

Profiles are pre-shipped bundles of capture and analysis defaults that get merged in when you set `profile:` in your config. Scan commands set the profile automatically; you can override with `--profile`.

| Profile | Category | Note range | Step | Velocities | RR | Duration | Loop | Normalize |
|---|---|---|---|---|---|---|---|---|
| `pluck` | synth | 21–108 | 3 | `[100]` | 1 | 2 s | off | per_set |
| `synth` | synth | 21–108 | 3 | `[100]` | 1 | 4 s | on | per_set |
| `pad` | synth | 24–96 | 3 | `[100]` | 1 | 8 s | on + tempo | per_set |
| `drums` | drum | 21–108 | 1 | `[64, 100, 127]` | 3 | 2 s | off | per_sample |

Auto-selection maps sustain classification to profile:
- **pluck** → `pluck`
- **sustained (short)** → `synth`
- **sustained_with_modulation** or long sustained → `pad`
- **kit or drumkit library type** → `drums`

Override with `--profile` on any scan command, or with `profile:` in the YAML.

## Example configs

Under `examples/` in the repo:

- `examples/dexed_first.yaml` — a VST preset.
- `examples/diva_warmth.yaml` — a VST pad with a loop.
- `examples/drum_kit.yaml` — a library kit.

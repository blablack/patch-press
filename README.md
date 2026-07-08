# patch-press

Captures VST/CLAP plugin presets and sample libraries and exports Deluge-ready XML + WAV presets. The goal is zero manual YAML writing — scan commands auto-generate configs from patch-probe output, CLAP plugin directories, or sample library folders. YAML files are the escape hatch when auto-detection needs a nudge.

## Prerequisites

- Python ≥ 3.14
- [patch-render](../patch-render) — companion JUCE-based audio host that drives VST/CLAP rendering
- A Deluge SD card (or a directory laid out like one) as the output target

## Installation

```bash
pip install -e .
```

## Workflows

### VST presets (via patch-probe)

Use [patch-probe](../patch-probe) to capture raw preset states from the plugin GUI, then:

```bash
# Generate one config per preset
patch-press scan-from-probe ~/patch-probe/Dexed configs/Dexed

# Export all presets to the SD card
patch-press batch "configs/Dexed/*.yaml" --path /media/DELUGE/SYNTHS/Dexed --format deluge
```

### CLAP presets

```bash
# Scan plugin presets (plugin ID is auto-discovered)
patch-press scan-clap /usr/lib/clap/MyPlugin.clap ~/presets/MyPlugin configs/MyPlugin

# Export
patch-press batch "configs/MyPlugin/*.yaml" --path /media/DELUGE/SYNTHS/MyPlugin --format deluge
```

### Sample library

```bash
# Multisample: subfolders contain per-note WAVs
patch-press scan-library "~/samples/Mini From Mars" configs/Mini --type multisample

# Kit: subfolders are instruments, WAVs inside are round-robin layers
patch-press scan-library "~/samples/808 Kit" configs/808 --type kit

# Export
patch-press batch "configs/Mini/*.yaml" --path /media/DELUGE/SYNTHS/Mini --format deluge
```

## Commands

| Command | Purpose |
|---|---|
| `sample <config.yaml>` | Run the full pipeline for one config |
| `batch <configs/*.yaml>` | Run all configs, skip existing outputs |
| `scan-from-probe <probe-dir> <config-dir>` | Generate configs from patch-probe YAMLs |
| `scan-clap <plugin.clap> <preset-dir> <config-dir>` | Generate configs from CLAP preset files |
| `scan-library <folder> <config-dir>` | Generate configs from a sample library |
| `classify <configs/*.yaml>` | Print sound type classification without exporting |
| `profiles` | List available profiles |

**Global flags:** `-v` (verbose progress), `--debug` (limit scans to 5 items for quick testing)

### Scan options

All scan commands accept:

| Option | Default | Description |
|---|---|---|
| `--profile` | auto | Override profile: `pluck` / `synth` / `pad` / `drums` |
| `--note-step` | 3 | Semitones between captured notes (1 = every note, 12 = one per octave) |
| `--duration` | 15.0 | Sustain capture duration in seconds |
| `--sample-rate` | 48000 | 44100 / 48000 / 96000 — VST/CLAP only |
| `--probe-note` | 60 | MIDI note used to detect sustain type |
| `--probe-velocity` | 100 | Velocity used when probing |
| `--tempo-bpm` | 120 | Tempo used for rhythm detection |

### Batch options

| Option | Default | Description |
|---|---|---|
| `--path PATH` | required | Output root directory |
| `--format FORMAT` | required | Output format (`deluge`) |
| `--workers N` | 1 | Parallel analysis threads |
| `--no-skip` | — | Re-run even if output already exists |

## Profiles

Profiles set capture timing and analysis defaults. Scan commands auto-detect the best profile by analysing a few representative notes — override with `--profile` when needed.

| Profile | Use for | Duration | Loop | Velocities | RR |
|---|---|---|---|---|---|
| `synth` | General melodic | 4 s | no | [100] | 1 |
| `pad` | Sustained, evolving pads | 8 s | yes | [100] | 1 |
| `pluck` | Short transients | 2 s | no | [100] | 1 |
| `drums` | Drum kits | 2 s | no | [64, 100, 127] | 3 |

## Config YAML reference

Scan commands write these automatically. Edit to override any auto-detected value.

```yaml
profile: synth          # baseline defaults — pluck / synth / pad / drums

source:
  type: vst             # vst | clap | library
  plugin: /path/to/Plugin.vst3
  preset: "Preset Name"
  raw_state: "<base64>" # state blob captured by patch-probe; bypasses preset lookup

capture:
  note_range: [36, 96]  # MIDI note range
  note_step: 3          # semitones between captured notes
  velocities: [100]     # which velocities to capture
  round_robins: 1       # max round-robin layers per note
  duration_s: 4.0       # note hold duration
  release_tail_s: 2.0   # recording time after note off
  sample_rate: 48000    # 44100 | 48000 | 96000

analysis:
  trim: true
  loop: false           # attempt loop-point detection
  loop_quality_threshold: 0.75
  normalize: per_set    # per_sample | per_set | none

output:
  name: MyPreset        # preset name used in the Deluge XML and file paths
```

Library configs use `source.type: library` with `source.path` instead of plugin/preset fields.

## Deluge output layout

```
<--path>/
  MyPreset.xml
  SAMPLES/
    <dir name>/
      MyPreset/
        note036_T120_V100_RR1.wav
        note039_T120_V100_RR1.wav
        ...
```

Copy the output directory to the matching location on the SD card (`SYNTHS/` or `KITS/`).

## Library filename conventions

Note and round-robin index are parsed from WAV filenames anywhere in the stem:

- `A3.wav` → note A3
- `C#2_0001.wav` → note C#2, round-robin 1
- `Mini_Patch_A#0_0001.wav` → note A#0, round-robin 1

When numbered round-robin files exist for a note, unnumbered base files for the same note are ignored.

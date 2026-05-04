# patch-press

Automatically captures VST presets or loads sample libraries and exports Deluge-ready XML + WAV presets. Zero manual YAML writing is the goal — scan commands handle discovery, YAML files are the escape hatch when auto-detection gets something wrong.

## Pipeline

```
YAML config → load_config() → VSTAdapter or LibraryAdapter
                                        ↓
                              analyze_sampleset()
                              (trim, envelope, pitch*, loop, normalize)
                                        ↓
                              DelugeExporter → WAV files + XML
```

*Pitch verification runs only for library sources, never for VST.

## Commands

| Command | Purpose |
|---|---|
| `scan <vst> <config-dir>` | Probe every VST preset, write one YAML per preset |
| `scan-library <folder> <config-dir>` | Write one YAML per subfolder of a sample library |
| `sample <config.yaml>` | Run the full pipeline for one config |
| `batch <configs/*.yaml>` | Run all configs sequentially, skip existing outputs |

## Profiles

Two profiles exist: `synth` (melodic, full range) and `drums` (multi-velocity, multi-RR). Duration and loop settings are inferred by the scan command — profiles are just the baseline.

## Key conventions

- Source type drives behaviour: VST skips pitch verification, library keeps it
- `scan` probes with middle C (MIDI 60); `--sustain-duration` controls capture length for sustaining sounds (default 4s, use 20s for complex synths like Diva/Zebra2)
- Library filenames: note+octave parsed anywhere in the stem (`Mini_Patch_A#0_0001.wav` → note A#0, RR 1); base files (no `_NNNN` suffix) ignored when numbered RRs exist for the same note
- Output is always Deluge format; `output.path` in the config is the directory on the SD card

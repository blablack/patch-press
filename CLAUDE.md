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
| `scan-from-probe <probe-dir> <config-dir>` | Analyse patch-probe YAMLs, write one config per preset |
| `scan-library <folder> <config-dir>` | Write one YAML per subfolder of a sample library |
| `scan-oneshots <folder> <config-dir>` | Write one YAML per WAV in a folder of single-note oneshot presets (e.g. Monosounds) |
| `sample <config.yaml>` | Run the full pipeline for one config |
| `batch <configs/*.yaml>` | Run all configs sequentially, skip existing outputs |

## Profiles

Four profiles exist: `synth` (melodic, full range), `pad` (sustained + evolving/rhythmic timbre, still loops), `pluck` (no sustain to loop — also auto-assigned when the non-loopable detector flags a continuously-evolving timbre), and `drums` (multi-velocity, multi-RR, no note_range/note_step). `synth`/`pad`/`pluck` are auto-detected from probe classification (`_sound_type_to_profile` in `runner/scan.py`); duration and loop settings are inferred by the scan command — profiles are just the baseline.

## Key conventions

- Source type drives behaviour: VST skips pitch verification, library keeps it
- `scan-from-probe` re-renders each preset at `--probe-note` (default 60) to classify sustain type; `--quality low/medium/high` controls note step and capture duration
- Library filenames: note+octave parsed anywhere in the stem (`Mini_Patch_A#0_0001.wav` → note A#0, RR 1); base files (no `_NNNN` suffix) ignored when numbered RRs exist for the same note
- `scan-library` assumes one subfolder = one preset (multisample notes or a kit's per-instrument dirs). When a folder instead holds many loose single-note oneshot WAVs that are each their own preset (no shared notes to group by), use `scan-oneshots` on that folder instead — it writes one config per file, with the root note auto-detected via pitch tracking (since e.g. Monosounds filenames carry a pitch class but no octave) and pinned into `source.note`
- Output is always Deluge format; `output.name` in the config sets the preset name; the SD card output directory is supplied via the CLI `--path` argument

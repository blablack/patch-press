# patch-press

Automatically captures VST presets or loads sample libraries and exports ready-to-play presets for hardware samplers — Deluge (XML + WAV) or Polyend Tracker (self-contained `.pti`). Zero manual YAML writing is the goal — scan commands handle discovery, YAML files are the escape hatch when auto-detection gets something wrong.

## Pipeline

```
YAML config → load_config() → VSTAdapter or LibraryAdapter
                                        ↓
                              analyze_sampleset()
                              (trim, envelope, pitch*, loop, normalize)
                                        ↓
                              Exporter (--format deluge|pti) → preset files
```

*Pitch verification runs only for library sources, never for VST.

## Commands

| Command | Purpose |
|---|---|
| `scan-from-probe <probe-dir> <config-dir>` | Analyse patch-probe YAMLs, write one config per preset |
| `scan-library <folder> <config-dir> --type multisample\|kit\|drumkit` | Write one YAML per subfolder of a sample library |
| `scan-oneshots <folder> <config-dir>` | Write one YAML per WAV in a folder of single-note oneshot presets (e.g. Monosounds) |
| `scan-wavetables <folder> <config-dir>` | Write one YAML per WAV in a folder of Serum-format wavetables (e.g. Echo Sound Works Core Tables) |
| `scan-bitwig <folder> <config-dir>` | Write one YAML per Bitwig `.multisample` archive (searched recursively; e.g. Orchestral Tools packs) |
| `assemble-kits <folder> <config-dir>` | Synthesize kit configs from a bag-of-hits library organized by instrument category (e.g. Samples From Mars "Individual Hits") |
| `sample <config.yaml>` | Run the full pipeline for one config |
| `batch <configs/*.yaml>` | Run all configs sequentially, skip existing outputs |

## Profiles

Four profiles exist: `synth` (melodic, full range), `pad` (sustained + evolving/rhythmic timbre, still loops), `pluck` (no sustain to loop — also auto-assigned when the non-loopable detector flags a continuously-evolving timbre), and `drums` (multi-velocity, multi-RR, no note_range/note_step). `synth`/`pad`/`pluck` are auto-detected from probe classification (`_sound_type_to_profile` in `runner/scan.py`); duration and loop settings are inferred by the scan command — profiles are just the baseline.

## Key conventions

- Source type drives behaviour: VST skips pitch verification, library keeps it
- `scan-from-probe` re-renders each preset at `--probe-note` (default 60) to classify sustain type; `--note-step` (default 3) sets the semitone step between captured notes and `--duration` (default 15.0 s) sets the sustain capture duration
- Library filenames: note+octave parsed anywhere in the stem (`Mini_Patch_A#0_0001.wav` → note A#0, RR 1); base files (no `_NNNN` suffix) ignored when numbered RRs exist for the same note
- `scan-library` assumes one subfolder = one preset (multisample notes, a kit's per-instrument dirs, or — with `--type drumkit` — a flat folder of loose one-shot drum hits, e.g. Samples From Mars "808 From Mars"/"909 From Mars" kit folders). When a folder instead holds many loose single-note oneshot WAVs that are each their own preset (no shared notes to group by), use `scan-oneshots` on that folder instead — it writes one config per file, with the root note auto-detected via pitch tracking (since e.g. Monosounds filenames carry a pitch class but no octave) and pinned into `source.note`
- `--type drumkit`: each WAV in the flat folder becomes one kit pad; the instrument is identified purely from filename keywords (`analysis/drumkit.py:classify_instrument` — BD/SD/CH/OH/Clap/Rim/Tom Low·Mid·Hi/Conga Low·Mid·Hi/Cowbell/Claves/Maracas/Cym/etc, unrecognized tokens fall to `other` rather than being dropped). Deluge kit XML has no explicit note attribute — pad order is purely `<soundSources>` document order — so pads are sorted into the canonical kick → snare → closed/open hat → clap/percussion → cymbals → toms (low→high) → congas (low→high) → other order, matching Synthstrom's own factory kits (TR-808/TR-909/R-100). A folder where every file lands in the same single category (e.g. a chromatic one-instrument folder) is flagged REVIEW rather than silently shipped as a real kit.
- `assemble-kits`: for libraries that ship as a *browsing bank* rather than pre-made kits — one subfolder per instrument category, each subdivided by a descriptive "flavor" taxonomy that repeats across categories (Clean/Color/Tape/Various, etc — confirmed against a real vendor-shipped kit that it's not a guessed convention, see `analysis/drumkit_assemble.py`). Generates one kit per shared flavor tag (a tag only counts if it spans ≥2 instrument families — `--min-categories`), picking one file per category from that flavor's subfolder, falling back to that category's `Various` pool and then to any file when the exact flavor isn't available for that pad (flagged REVIEW). The resolved file list is written into `source.files` in the YAML once at scan time — `sample`/`batch` just load that fixed list, so a wrong pick is a one-line hand-edit, not a re-scan. **Scope boundary**: this only fires when flavor tags live in shared subfolder names; libraries where descriptive words only appear inside flat filenames (no shared subfolder taxonomy) aren't handled — that would need real audio-feature coherence, not built here.
- `scan-bitwig`: a Bitwig `.multisample` is a ZIP of WAVs + a `multisample.xml` that authoritatively maps each WAV to a key/velocity zone and (for sustaining sounds) a loop — so there's no filename parsing, pitch tracking, loop detection or probe rendering (`io/adapters/bitwig.py`, `analysis/drumkit`-style metadata read). The loop flag is bimodal per file (Bitwig writes `mode="loop"` on every zone of a sustaining articulation, `mode="off"` on every zone of a staccato/one-shot one), so profile follows it: all-loop → `synth` (ships authored loop), else → `pluck`. Root notes are ground truth (pitch_verify off). Per note the adapter keeps ONE velocity layer (nearest `source.velocity`, default 100 — hardware presets don't layer velocity and both exporters collapse anyway), thins by `note_step`, slices each WAV to its `[sample-start, sample-stop)` region (a looped note's region ends exactly at loop-end; Bitwig keeps unused recording past it), bakes the loop crossfade with Bitwig's own `fade` length and marks it authored so the pipeline ships it verbatim, and applies per-sample `gain`. Recursive scan; mirrors source subfolders into config tree + `output.subfolder`. Fixed-pitch-dominant files (`<key track="0">`, percussion maps) flagged REVIEW. See `docs/inputs/bitwig.md`.
- Wavetables (Serum-format, `clm` chunk, 2048 samples per cycle — a trailing partial window is dropped rather than rejected, since Polyend's own stock tables ship a few samples short and the Tracker floors to whole windows; stereo ships stereo to `.pti` as planar PCM with the length field counting frames per channel, and is downmixed only for the Deluge, whose wavetable oscillator is mono-only) are a different pipeline entirely — no trim/envelope/loop/normalize, the file ships to the SD card unmodified and its sound comes from the Deluge's own wavetable-scan oscillator. `scan-wavetables` analyses each file's own spectral content (brightness, flatness, frame-to-frame timbral variance) to pick an archetype (pad/pluck/bass/lead/drone/evolving_pad) and WT-position/LFO2-depth — see `docs/inputs/wavetables.md` for the rationale and `analysis/wavetable.py` for the thresholds (first pass, tune by ear like the loop-detection constants elsewhere in this codebase)
- Output format is selected per-run via `--format deluge|pti` (Deluge XML+WAV, or self-contained Polyend Tracker `.pti` — see `docs/outputs/`); `output.name` in the config sets the preset name; the SD card / output directory is supplied via the CLI `--path` argument
- Capture size is format-aware, and only there: an exporter declares which of a melodic note grid it actually ships (`notes_used(notes)` — Deluge keyzones every note, `.pti` has no keyzones and ships one repitched sample), and `runner/pipeline.py:notes_to_capture` consults it before a VST/CLAP render so a pti run doesn't pay for renders the export discards. Same method picks the sample at export time, so narrowed capture and export can't drift. Optional — an exporter without it gets the full grid. Nothing is cached between runs, so building both targets from one config renders twice

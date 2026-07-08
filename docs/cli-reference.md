---
title: CLI reference
layout: default
nav_order: 8
---

# CLI reference

Every command and every flag. All commands accept the two global flags below.

## Global flags

| Flag | What it does |
|---|---|
| `-v`, `--verbose` | Per-item progress and INFO-level log output. Without this, only warnings are printed. |
| `--debug` | Limit scans to the first 5 items and enable DEBUG-level logging. Good for shaking down settings on a big directory. |

Both go **before** the subcommand: `patch-press -v batch …`.

---

## `sample`

Run the full pipeline for one config file.

```
patch-press sample <config.yaml> --path <output-dir> --format <format> [--workers N]
```

| Argument | Required | Default | Notes |
|---|---|---|---|
| `config` | yes | — | Path to the YAML config. |
| `--path PATH` | yes | — | Output root — usually the SD-card mount point or a staging directory. |
| `--format FORMAT` | yes | — | Target export format. Currently ships `deluge`; the pipeline is designed for additional targets. See [Outputs](outputs/). |
| `--workers N` | no | 1 | Parallel analysis workers. Independent per-sample work parallelizes well. |

---

## `batch`

Run many configs in sequence, skipping ones whose output already exists.

```
patch-press batch <configs...> --path <output-dir> --format <format> \
                  [--workers N] [--no-skip] [--only-profile P]
```

| Argument | Required | Default | Notes |
|---|---|---|---|
| `configs` | yes | — | One or more paths or globs. Globs are expanded by the shell OR by patch-press if they didn't match anything. |
| `--path PATH` | yes | — | Output root. |
| `--format FORMAT` | yes | — | Target export format. Currently `deluge`. |
| `--workers N` | no | 1 | Parallel workers per config. |
| `--no-skip` | no | off | Re-run even if the output already exists. |
| `--only-profile {pluck,synth,pad,drums}` | no | — | Filter to just configs matching this profile. Debug/re-run aid. |

---

## `scan-from-probe`

Turn a directory of patch-probe YAMLs into a directory of configs. See [VST workflow](inputs/vst-workflow.html).

```
patch-press scan-from-probe <probe-dir> <config-dir> [options...]
```

| Argument | Required | Default | Notes |
|---|---|---|---|
| `probe_dir` | yes | — | Directory of patch-probe output. |
| `config_dir` | yes | — | Where to write generated configs. |
| `--profile {pluck,synth,pad,drums}` | no | auto | Override auto-detected profile. |
| `--sample-rate {44100,48000,96000}` | no | 48000 | Rendering sample rate. |
| `--tempo-bpm BPM` | no | 120.0 | Tempo used for rhythm detection. |
| `--probe-note MIDI` | no | 60 | Which note to render during the classification probe. |
| `--probe-velocity VEL` | no | 100 | Velocity for the probe. |
| `--note-step INT` | no | 3 | Semitones between notes at batch time. |
| `--duration FLOAT` | no | 15.0 | Probe hold length in seconds. |
| `--start-note NOTE` | no | C1 | Lowest note. Accepts note names (`C1`, `A#0`) or MIDI numbers. |
| `--end-note NOTE` | no | C6 | Highest note. |

---

## `scan-clap`

Same shape as `scan-from-probe`, but scans a CLAP plugin's own preset directory (no patch-probe required). See [CLAP plugins](inputs/clap-plugins.html).

```
patch-press scan-clap <plugin.clap> <preset-dir> <config-dir> [options...]
```

| Argument | Required | Default |
|---|---|---|
| `plugin` | yes | — |
| `preset_dir` | yes | — |
| `config_dir` | yes | — |
| `--profile` / `--sample-rate` / `--tempo-bpm` / `--probe-note` / `--probe-velocity` / `--note-step` / `--duration` / `--start-note` / `--end-note` | no | Same defaults as `scan-from-probe` above. |

---

## `scan-library`

Generate configs from a sample library. See [Sample libraries](inputs/sample-libraries.html).

```
patch-press scan-library <library-root> <config-dir> --type {multisample,kit,drumkit} [options...]
```

| Argument | Required | Default | Notes |
|---|---|---|---|
| `library` | yes | — | Library root. |
| `config_dir` | yes | — | Output dir for configs. |
| `--type {multisample,kit,drumkit}` | yes | — | See [sample libraries](inputs/sample-libraries.html). |
| `--profile {pluck,synth,pad,drums}` | no | auto | Override auto-detected profile. |
| `--note-step INT` | no | 3 | Only relevant with sparse coverage. |
| `--start-note NOTE` | no | C1 | Ignore samples below this note. |
| `--end-note NOTE` | no | C6 | Ignore samples above this note. |

---

## `scan-oneshots`

Turn a folder of single-note oneshot WAVs into one config per file. See [Oneshots](inputs/oneshots.html).

```
patch-press scan-oneshots <folder> <config-dir> [--profile P]
```

| Argument | Required | Default |
|---|---|---|
| `folder` | yes | — |
| `config_dir` | yes | — |
| `--profile {pluck,synth,pad,drums}` | no | auto |

---

## `scan-wavetables`

Turn a folder of Serum-format wavetable WAVs into one config per file. See [Wavetables](inputs/wavetables.html).

```
patch-press scan-wavetables <folder> <config-dir> [--archetype A]
```

| Argument | Required | Default |
|---|---|---|
| `folder` | yes | — |
| `config_dir` | yes | — |
| `--archetype {pad,pluck,bass,lead,drone,evolving_pad}` | no | auto |

---

## `assemble-kits`

Synthesize kit configs from a bag-of-hits library. See [Kit assembly](inputs/kit-assembly.html).

```
patch-press assemble-kits <folder> <config-dir> [--min-categories N]
```

| Argument | Required | Default |
|---|---|---|
| `folder` | yes | — |
| `config_dir` | yes | — |
| `--min-categories N` | no | 2 |

---

## `classify`

Print each config's sound-type classification (pluck / sustained / sustained+rhythm) without exporting. Useful for auditing what `scan-*` did.

```
patch-press classify <configs...> [--workers N] [--save-path PATH]
```

| Argument | Required | Default |
|---|---|---|
| `configs` | yes | — |
| `--workers N` | no | 1 |
| `--save-path PATH` | no | — | If set, writes the classification-render WAVs under `PATH/<preset_name>/` for inspection. |

---

## `profiles`

Print the list of available profile names, one per line.

```
patch-press profiles
```

No flags.

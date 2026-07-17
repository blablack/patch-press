---
title: CLAP plugins
layout: default
parent: Inputs
nav_order: 2
---

# CLAP plugins

CLAP (the CLever Audio Plugin format) is friendlier than VST for tooling: presets are files on disk, and the plugin exposes them through the CLAP preset-discovery API. patch-press can scan a CLAP plugin's preset directory directly — **no patch-probe required**.

```bash
patch-press scan-clap ~/.clap/Surge XT.clap ~/.local/share/clap/surge-xt configs/SurgeXT
```

The three arguments:

1. Path to the `.clap` plugin file itself.
2. Directory containing `*.clap-preset` files (usually the plugin ships one under `~/.local/share/clap/<name>/` or `~/Library/Preferences/clap/<name>/`).
3. Destination for the generated configs.

## What scan-clap does

For each `.clap-preset` file:

1. Loads the plugin, hands it the preset file to restore, and captures the resulting state.
2. Renders one probe note (default: MIDI 60) to classify sustain type.
3. Writes one config per preset with `source.type: clap`, `source.raw_state` baked in, and `source.preset_path` set to the original file.

## Config shape

```yaml
source:
  type: clap
  plugin: /path/to/Plugin.clap
  plugin_id: "com.example.plugin"      # auto-discovered from the .clap
  preset: "Init"                        # documentation
  preset_path: /path/to/init.clap-preset
  raw_state: "<base64>"
```

`plugin_id` is required by the CLAP host to instantiate the right plugin — `scan-clap` reads it out of the `.clap` file for you.

## `scan-clap` options

Same set as [`scan-from-probe`](vst-workflow.html#scan-from-probe-options):

- `--profile`, `--sample-rate`, `--tempo-bpm`
- `--probe-note`, `--probe-velocity`
- `--note-step`, `--duration`
- `--start-note`, `--end-note`

## After scanning

Identical to the VST path:

```bash
patch-press batch "configs/SurgeXT/*.yaml" --path /media/DELUGE --format deluge
# ...or --path staging/Polyend --format pti for a Polyend Tracker instrument
```

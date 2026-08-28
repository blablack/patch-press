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
3. Measures whether the preset is actually stereo (below).
4. Writes one config per preset with `source.type: clap`, `source.raw_state` baked in, and `source.preset_path` set to the original file.

## Mono presets are captured in mono

A plugin always hands back a stereo buffer, whether or not the patch has anything stereo to say. A preset with no unison spread and no stereo chorus, delay or reverb writes the identical signal to both channels — and shipping that to the card as a stereo WAV doubles the file, doubles the sampler's voice memory and doubles the SD read for a difference nobody can hear.

So the scan measures it, on the renders the probe is already paying for, and writes the answer into the config:

```yaml
# confidence=high sustains=yes release=4.0s channels=mono (side identical)
capture:
  duration_s: 15.0
  release_tail_s: 4.0
  mono: true
```

The measurement is the level of the side signal relative to the mid, over the whole render:

```
mid = (L + R) / 2    side = (L - R) / 2    side_db = 20*log10(rms(side) / rms(mid))
```

At or below **-60 dB** the two channels are the same signal and the capture ships one channel. The threshold is nowhere near a close call: across the 1356 Diva presets in this corpus, 255 render *bit-identical* channels on every note, two more sit at -113 and -94 dB, and the next preset up is at -38.7 dB — a 56 dB hole in the distribution. Any threshold between -90 and -40 dB sorts them identically (19.0% mono). Presets don't change their mind across the keyboard either: over a 339-preset subset, not one was mono at some notes and stereo at others.

`mono: true` is a plain capture setting like any other — delete it (or set it to `false`) and the next build ships stereo again. It is only ever written by the plugin scans; sample libraries ship whatever channel count the vendor mastered.

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

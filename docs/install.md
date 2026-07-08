---
title: Install
layout: default
nav_order: 2
---

# Install

patch-press runs on **Linux** and **macOS**. Both are POSIX enough that `pathlib` and the audio dependencies behave the same; the main practical difference is where your SD card mounts.

## Prerequisites

**Python ≥ 3.14.** Older versions won't install — check with `python3 --version`.

**[patch-render](https://github.com/blablack/patch-render)** — a small JUCE-based headless audio host that patch-press uses to render VST and CLAP plugins. Pulled in automatically as a dependency, but requires a working C++/JUCE build toolchain on the machine (Xcode Command Line Tools on macOS; `build-essential` and the JUCE build prerequisites on Linux).

If you're only working with sample libraries or wavetables — no VST/CLAP scanning — patch-render is still pulled in but you'll never invoke it.

**A target output location.** patch-press writes into whatever directory you point `--path` at, in the layout your `--format` expects. For the Deluge target (the exporter that ships today), that's a directory with `SYNTHS/`, `KITS/`, and `SAMPLES/` subfolders — either the Deluge SD card mounted directly, or a staging directory you `rsync` to the card later. See [Outputs](outputs/) for what each target writes.

## Install patch-press

```bash
git clone https://github.com/blablack/patch-press
cd patch-press
pip install -e .
```

The `-e` (editable) install is handy if you plan to poke at thresholds in the analysis code — changes take effect without reinstalling.

## Verify

```bash
patch-press profiles
```

Should print:

```
drums
pad
pluck
synth
```

If you see `command not found`, your Python `bin/` isn't on `PATH`. Either activate the virtualenv you installed into, or invoke as `python -m patch_press …`.

## Linux notes

- **SD card mount path** is typically `/media/$USER/DELUGE` or `/run/media/$USER/DELUGE` depending on your distro. Pass whichever your system gives you to `--path`.
- **Audio hosts for VST/CLAP** need the plugin files on disk somewhere findable by JUCE. `.vst3` bundles typically live in `~/.vst3/` or `/usr/lib/vst3/`. `.clap` files go in `~/.clap/` or `/usr/lib/clap/`.
- **numba/librosa** will JIT-compile on first run — expect the first `sample` or `batch` invocation to spend a few seconds warming up before it starts logging progress. Later runs use the cached compilation.

## macOS notes

- **SD card mount path** is `/Volumes/DELUGE` (or whatever you named the volume).
- **Plugins** live in `~/Library/Audio/Plug-Ins/VST3/` (system) or `/Library/Audio/Plug-Ins/VST3/`. `.clap` files: `~/Library/Audio/Plug-Ins/CLAP/`.
- **Xcode Command Line Tools** — `xcode-select --install` if you haven't already. patch-render's JUCE build needs it.

## Next

- **Have VST presets?** → [VST workflow](inputs/vst-workflow.html)
- **Have a sample library?** → [Sample libraries](inputs/sample-libraries.html)
- **Have Serum wavetables?** → [Wavetables](inputs/wavetables.html)

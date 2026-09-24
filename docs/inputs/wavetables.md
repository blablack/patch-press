---
title: Wavetables
layout: default
parent: Inputs
nav_order: 5
---

# Wavetables

Wavetables are a different kind of animal from every other input on this site. A wavetable WAV isn't a recording of a note — it's a stack of single-cycle waveforms, each exactly 2048 samples long, that the target device's wavetable oscillator sweeps through under LFO or manual control.

Because of that, **wavetables skip the whole analysis pipeline**: no trimming, no envelope detection, no loop hunting, no normalization. The file is copied to the target bit-for-bit where the device allows it (preserving Serum's `clm` metadata chunk so the target device knows how to slice it), and the exporter generates the surrounding preset with envelope and filter parameters chosen from spectral analysis of the wavetable itself.

```bash
patch-press scan-wavetables "~/wavetables/Liam Wavetables" configs/LiamWT
```

## Format requirements

- **WAV file**, mono or stereo, 16-bit or 32-bit float.
- **At least 2048 samples.** Each 2048-sample block is one frame in the wavetable.
- If Serum's `clm` chunk is present, patch-press preserves it byte-for-byte in the output. This is how the Deluge distinguishes wavetables from raw samples.

A length that isn't an exact 2048-multiple is fine: the trailing partial window is ignored, matching how the Tracker itself floors to whole windows. Polyend's own stock wavetables need this — they ship a few samples shy of 256 windows (e.g. 524267 = 255 windows + 2027 samples). The file is still analysed normally on its whole windows and the config is flagged REVIEW noting how many samples were dropped. Only a file shorter than a single 2048-sample window is rejected.

On the card, such a file is **truncated** for the Deluge and the Bento rather than copied bit-for-bit, unless it carries a `clm` chunk. Without one, the Deluge firmware only loads a file as a wavetable if its length is an exact 2048-multiple (anything else fails with `FILE_NOT_LOADABLE_AS_WAVETABLE`), so a verbatim copy of a Polyend stock table silently doesn't work there. The truncated copy is re-encoded at the source's own bit depth, and raised to full scale while it's being rewritten anyway: Polyend's stock tables sit at −6 dBFS, which made their presets noticeably quieter than the rest.

### Stereo wavetables

Stereo is accepted, and what happens to it depends on the target — the two devices genuinely differ:

- **Polyend Tracker Mini / Tracker+**: both channels ship. The `.pti` length field counts frames *per channel*, so the window count is unchanged and the PCM is planar (all left, then all right) exactly like a stereo sample instrument.
- **Deluge** and **1010music Bento**: downmixed to mono. The Deluge's wavetable oscillator is mono-only and the firmware won't force-load a stereo file as a wavetable at all; the Bento rejects one outright with `Wavetables must be mono WAVs.` The copy is no longer bit-for-bit for these files: it's re-encoded at the source's own bit depth, truncated to whole windows, with any `clm` chunk carried across.

Downmixing is a real loss for material designed as stereo — Polyend's own stereo bank has tables whose channels correlate as low as −0.32, where mono summing cancels content rather than just narrowing it. So it's done only where the device leaves no choice.

The archetype analysis always runs on the mono sum, on every target.

## What the analysis measures

Each 2048-sample frame is exactly one cycle, so an FFT of it lands harmonic *k* precisely on bin *k* — no pitch detection needed, and **no analysis window** (a window only exists to hide the discontinuity of a non-periodic frame; here there isn't one, and windowing actively smears each harmonic into its neighbours). Four features come out of that:

| Feature | What it is | Units |
|---|---|---|
| **brightness** | mean spectral centroid | harmonics (a value of 139 = energy centred on the 139th harmonic) |
| **movement** | how far the centroid travels across the frames | harmonics |
| **tonality** | share of energy in the 32 strongest bins | 0–1 (square ≈ 0.99, white noise ≈ 0.14) |
| **hollowness** | odd harmonics from the 3rd up vs the even ones | 0–1 (0.5 = balanced, →1 = odd-only/hollow) |

## Archetype detection

The archetype sets the **envelope and filter shape**. There are three, but auto-detection only ever picks two:

| Archetype | When it's picked | Envelope |
|---|---|---|
| **sustaining** | default (~72% of the reference corpus) | responsive attack, high sustain — a patch that speaks when you press the key |
| **evolving** | `tonality < 0.65` (noise/texture, no clear pitch) **or** `movement > 150` harmonics | long attack, full sustain, long release, table sweep provides the motion |
| **percussive** | **never auto-detected** — `--archetype percussive` only | fast attack, no sustain |

`percussive` is deliberately not inferred. Timbre and playing style are independent axes: a bright hollow wave is equally a clav, a reed lead or a pad, and nothing in a single-cycle table says which. The one feature that could plausibly have split it — hollowness — is *unimodal* across the 701-file reference corpus (a single spike at 0.48 with no second cluster), so any threshold on it would have been arbitrary. It stays available as a template you can force on a folder you already know is plucky.

A file whose deciding feature lands within 5% of its threshold is flagged `REVIEW` in the generated config rather than silently committed — about 7% of the reference corpus. Those are the ones worth auditioning.

## Timbre tags

Separately from the archetype, the scan writes a `tag_hint` — one of `drone` / `evolving` / `bass` / `lead` / `pad` — describing how the table *sounds*. This is what brightness and harmonic character legitimately measure, even though they say nothing about how the patch should be played. It's used by the [Bento](../outputs/bento.html) exporter, which feeds it to its tag deriver as if it were a word in the preset name (`pad`/`bass`/`lead` map to themselves, `drone`/`evolving` to `Atmosphere`). A real folder label always wins; this only speaks when nothing else does.

### Calibration status

The thresholds are quantiles of a 701-file reference corpus (Liam Wavetables + Polyend Wavetables) — chosen so the corpus actually spreads across them, not picked in the abstract. They have **not** been ear-validated on hardware yet; treat them the same as the loop-detection constants elsewhere in this codebase. The `REVIEW` flags are the shortlist to start from.

## Config shape

```yaml
source:
  type: wavetable
  path: /path/to/Warm Pad 01.wav

wavetable:
  archetype: sustaining
  wt_position: 0.15         # starting frame (0.0 = first, 1.0 = last)
  lfo2_rate: 0.30
  lfo2_depth: 0.60         # clamped so wt_position + lfo2_depth <= 1.0
  filter_cutoff: 0.55
  attack: 0.10
  decay: 0.35
  sustain: 0.90
  release: 0.35
  filter_type: lpf
  tag_hint: pad            # browser tag only; does not affect the sound

output:
  name: WarmPad01
```

Every 0–1 parameter is a plain fraction; the exporter maps it to the Deluge's signed-32-bit param range.

## Overriding the archetype

If the auto-detection picked something you disagree with, either:

- **Re-scan with `--archetype`** to force one archetype across the whole folder:
  ```bash
  patch-press scan-wavetables "~/wavetables/Plucks" configs/Plucks --archetype percussive
  ```
- Or **edit the YAML** for that one file (`archetype:` and the parameter block) and re-run `sample`.

## `scan-wavetables` options

| Option | Default | What it does |
|---|---|---|
| `--archetype sustaining\|evolving\|percussive` | auto | Force a single archetype for every file in the folder. This is the only way to get `percussive`. |

## On the SD card

- **Deluge**: `SYNTHS/<name>/`, same as sample-based synths — a synth preset whose oscillator mode is set to `wavetable` in the XML, with the WAV alongside in the same folder.
- **Polyend Tracker Mini**: a self-contained `.pti`, the table embedded as PCM.
- **1010music Bento**: a `wttrack` patch folder under `UserPatches/Wavetable/`, the table WAV next to `patch.xml` and named by the oscillator cell. See [Bento](../outputs/bento.html#wavetables) for why the firmware's built-in table catalogue isn't what selects it.

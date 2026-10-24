---
title: Deluge output
layout: default
parent: Outputs
nav_order: 1
---

# Deluge output

patch-press writes directly into the Deluge SD-card layout. Point `--path` at the SD-card root (or a staging directory laid out the same way), pass `--format deluge`, and every file lands in the right place.

Deluge is the target the pipeline was originally built for. The pipeline itself is format-agnostic (see [Outputs](./)) — a [Polyend Tracker exporter](polyend.html) ships alongside it — and other targets can be added without touching the analysis code.

## SD-card layout

```
<--path>/                            # e.g. /media/DELUGE
├── SYNTHS/
│   └── <preset-name>/
│       └── <PresetName>.XML         # synth preset (multisample or wavetable)
├── KITS/
│   └── <kit-name>/
│       └── <KitName>.XML            # kit preset
└── SAMPLES/
    └── <preset-name>/
        ├── note036_T120_V100_RR1.wav
        ├── note039_T120_V100_RR1.wav
        └── …                        # or the raw wavetable WAV, unmodified
```

**SYNTHS vs KITS** is decided by the config's `category`:

- `synth` — melodic multisamples, wavetables → `SYNTHS/`
- `drum` — drum kits → `KITS/`

The `output.name` in the config becomes both the folder name and the XML filename (with sanitization: spaces and slashes become underscores).

## Sample WAV filenames

Sample WAVs go under `SAMPLES/<preset-name>/`. Each filename encodes the MIDI note, tempo, velocity, and round-robin index:

```
note<MIDI>_T<TEMPO>_V<VELOCITY>_RR<INDEX>.wav
```

Example: `note060_T120_V100_RR1.wav` = MIDI 60 (C4), tempo 120 BPM, velocity 100, round-robin 1. This lets a human eyeball the folder and spot missing notes or wrong velocities without loading anything.

A sample that came from a library keeps its own filename instead, and when the analysis left its audio untouched the vendor's file is **copied byte-for-byte** rather than re-encoded — original bit depth, channel count and metadata chunks intact. See [Shipping the vendor's own file](../inputs/sample-libraries.html#shipping-the-vendors-own-file).

Every WAV is checked against what the Deluge firmware will actually load: PCM at 8/16/24/32 bits or 32-bit float, mono or stereo, 5–96 kHz, and a plain `fmt ` header. The firmware refuses WAVE_FORMAT_EXTENSIBLE even around ordinary 24-bit PCM, so a file whose only fault is that header gets it rewritten as plain PCM/float, with the audio and every other chunk left byte-identical. A file the Deluge genuinely can't play is transcoded: extra channels folded to stereo, and a sample rate outside the range halved or doubled until it fits, with the preset's loop points scaled to match.

## XML: multisample synth

```xml
<sound polyphonic="poly" voicePriority="1" mode="subtractive" lpfMode="24dB">
  <osc1 type="sample" loopMode="2">
    <sampleRanges>
      <sampleRange rangeTopNote="42"
                   fileName="SAMPLES/MyPatch/note039_T120_V100_RR1.wav"
                   transpose="-9">
        <zone startSamplePos="0" endSamplePos="192000"
              startLoopPos="21504" endLoopPos="45568"/>
      </sampleRange>
      <sampleRange rangeTopNote="45"
                   fileName="SAMPLES/MyPatch/note042_T120_V100_RR1.wav"
                   transpose="-6">
        <!-- loops through the release: endSamplePos is the loop end -->
        <zone startSamplePos="0" endSamplePos="44800"
              startLoopPos="20736"/>
      </sampleRange>
      <!-- one <sampleRange> per captured note -->
    </sampleRanges>
  </osc1>

  <patchCables>
    <patchCable source="velocity"   destination="volume"/>
    <patchCable source="aftertouch" destination="volume"/>
    <patchCable source="y"          destination="lpfFrequency"/>
  </patchCables>

  <!-- envelope, filter, LFOs, etc. -->
</sound>
```

- One `<sampleRange>` per captured note.
- `rangeTopNote` is the top note the range covers — the next range's bottom is one higher.
- `transpose` maps the sample's captured root to its target Deluge note, so a sample captured at C3 can serve C3 up to some ceiling before the next range takes over.
- A range with an `endLoopPos` loops between `startLoopPos` and `endLoopPos` while the key is held; at note-off playback leaves the loop and plays on to `endSamplePos`. That comes from `endLoopPos` alone, in any loop mode (`VoiceSamplePlaybackGuide::getLoopingType` in the firmware). There is no per-range loop mode: the firmware's `<sampleRange>` parser reads only `fileName`, `rangeTopNote`, `zone`, `transpose` and `cents`. The loop points come from the [loop detection stage](../pipeline.html#4-loop-detection).
- **What plays after note-off decides each range's layout.** When every note of a preset loops, the oscillator switches to `loopMode="2"` (loop), and each range gets one of two layouts:
  - **The tail is a real release: `endLoopPos` is kept.** At note-off the range leaves the loop and plays the release the sample already holds. This applies to a library sample whose file fades at least 20 dB below the loop's level by its end, i.e. the author recorded the release there (about three quarters of Samples from Mars's looped notes).
  - **Anything else loops through the release: no `endLoopPos`, and `endSamplePos` is the loop end.** The range keeps looping while the envelope's release fades it, so turning the release up on the device still works. This is Rephazer's layout, used by 402 of their 446 sample presets. It applies to every plugin capture, whose audio after the loop end is seconds of held sustain before the plugin even saw its note-off. It also applies to a library tail that is still loud when the file ends, which would otherwise play on and then cut out mid-sound. A capture's WAV is cut at the loop end, since nothing past it can be heard; a library WAV still ships byte-for-byte, its unplayed tail included.

  A preset with even one unlooped note stays in `loopMode="0"` (cut) with every range keeping its `endLoopPos`, because in loop mode a range without loop points would loop its whole sample.
- **Envelope 1's release follows the plugin's own.** On a plugin capture the note-off is known, so the release is timed: seconds from note-off until the level is 40 dB below where it stood, median over the shipped notes. That time is matched to the release knob value whose own fall to −40 dB is closest. The firmware's knob-to-time curve was worked out from its lookup tables: 10 ≈ 0.5 s, 25 ≈ 1.9 s, 30 ≈ 2.7 s, 40 ≈ 5.9 s, 50 ≈ 21 s to −40 dB. A library sample, or a capture that had already decayed to silence before its note-off, keeps the default of 25.
- One sample per note: the velocity closest to 100, then the lowest round robin. The Deluge has no velocity zones or round-robin engine, and **only the WAVs the XML references are written**: anything else would never be loaded, but would still cost card space and lengthen the folder that every sample lookup scans (the firmware resolves each file path with a linear directory search). Kit pads likewise ship one file each.
- **Velocity-sensitive presets get a velocity → filter cable instead of layers.** A plugin preset the scan judged velocity-sensitive asks for several velocity layers (`capture.velocities`), but a `<sampleRange>` has no velocity zones. So a Deluge build renders only the one layer it ships (`DelugeExporter.velocities_used`, asked before capture just like `notes_used`), and the config's verdict reaches the exporter as a flag instead. Its brightness change is handed to the filter with TAL's recipe, the same on all 49 of TAL's velocity-filtered factory presets: a 12 dB lowpass at 40 and a velocity → cutoff cable at +20, so a soft hit closes the filter and a hard one opens it.

### Performance settings

A config's [`performance:`](../config-reference.html#performance) block (written by `scan-clap` from a u-he `.h2p`) maps onto the sound as far as the Deluge can follow it:

| Setting | On the Deluge |
|---|---|
| `voice_mode` | `polyphonic="poly"` / `"mono"` / `"legato"` |
| `glide` | `portamento`, the same fraction of its range |
| `aftertouch: vibrato` | An LFO1 → pitch cable (+5) with its depth controlled by aftertouch at +50. Used as a depth control, aftertouch reads −1 at rest, so the depth is 1 + (−1 × 1) = 0: no vibrato until the key is pressed, twice the base depth at full pressure. |
| `mod_wheel: darken` | The default Y → cutoff cable flipped to −20. The capture is the wheel at rest, so the filter stays open there and the wheel closes it. |
| `mod_wheel: vibrato` | Not mapped. The wheel reaches the `y` source unipolar, 0 at rest, so a wheel-scaled depth (1 + y × amount) still plays at its base depth with the wheel down. The default LFO1 → pitch gold knob is the vibrato control instead. |
| `mod_wheel: brighten` | Not mapped. It would need brightness the capture doesn't have: the sample already is the wheel at rest. |

The +5 vibrato depth and the −20 darken amount are starting values, not ear-tuned.

## XML: drum kit

```xml
<kit>
  <soundSources>
    <soundSource name="Kick"  …>
      <zones><zone startSamplePos="0" endSamplePos="…"/></zones>
    </soundSource>
    <soundSource name="Snare" …>
      <zones><zone startSamplePos="0" endSamplePos="…"/></zones>
    </soundSource>
    <soundSource name="HH Closed" …/>
    <soundSource name="HH Open"  …/>
    <!-- sorted in the canonical Synthstrom order -->
  </soundSources>
  <pads>
    <pad padNumber="0" soundIndex="0"/>   <!-- pad 0 → first sound in list (kick) -->
    <pad padNumber="1" soundIndex="1"/>
    <!-- … -->
  </pads>
</kit>
```

### Canonical pad order

Deluge kit XML **has no explicit note attribute on a pad** — pad ordering is document order in `<soundSources>`. That means the order patch-press writes the sounds in is the order they appear on your Deluge's pads.

patch-press sorts kit sounds into the canonical order used by Synthstrom's factory kits (TR-808, TR-909, R-100):

**kick → snare → hat_closed → hat_open → clap/rim/snap → cymbals → toms (low → mid → high) → congas (low → mid → high) → other**

Within a category, ties break on filename alphabetical order.

This means a scan of Samples From Mars "909 From Mars" and a scan of "808 From Mars" produce kits laid out on the same pads — kick on pad 0, snare on pad 1, closed hat on pad 2, and so on. Muscle memory ports across kits.

### Per-pad settings

Each pad gets the roles Synthstrom's own factory kits give it:

- **Play once** (`loopMode="1"`, 238 of 290 factory pads). A hit runs to its end whatever the note length; in cut mode a one-step sequencer note stopped an open hat, crash or 808 tail at the step. A pad with loop points still loops while held, since the firmware obeys a loop end in any mode.
- **Only kicks feed the sidechain** (`sideChainSend`, on 25 of 32 factory kicks and no snare, clap or tom). Any non-zero send registers a sidechain hit, so a send on every pad made hats and claps duck whatever had sidechain ducking dialled in. Other pads omit the attribute, which is the firmware's default of 0.
- **Hi-hats choke** (`polyphonic="choke"`, 30 of 60 factory hat pads). A choke-mode pad silences the kit's other choke-mode pads when it plays, so a closed hat cuts the open one.

Kick and hi-hat are the same filename classification that orders the pads.

## XML: wavetable synth

Wavetables use the same `<sound>` element as multisamples, but `type="wavetable"` oscillators and no `<sampleRanges>` — the whole file is one wavetable, sweep-scanned by the Deluge's wavetable oscillator. Envelope/filter/LFO parameters come from the [archetype](../inputs/wavetables.html#archetype-detection) chosen at scan time.

The layout matches the [Bento export](bento.html#wavetables): the table plays on **both** oscillators, detuned ±6 cents, and LFO2 sweeps their positions in opposite directions (`lfo2_depth` × 50 on oscillator A, the negative on B), so the pair move apart. Both positions sit on the last gold-knob pair (`oscBWavetablePosition`, `oscAWavetablePosition`), in place of bitcrush and sample-rate reduction. That's the slot every Boards of Deluge wavetable preset uses; the other 14 knobs are the firmware defaults.

The WAV file itself is copied bit-for-bit (no re-encoding), preserving Serum's `clm` metadata chunk so the Deluge knows how to slice the frames.

## Moving to the SD card

If you pointed `--path` at the SD card directly, you're done — unmount and pop it in. If you rendered to a staging directory, `rsync` (or a plain copy) the three top-level folders across:

```bash
rsync -av --progress \
  ~/staging/SYNTHS/ /media/DELUGE/SYNTHS/
rsync -av --progress \
  ~/staging/SAMPLES/ /media/DELUGE/SAMPLES/
```

`rsync -a` preserves timestamps, which is what `patch-press batch --no-skip` uses to decide what to re-render.

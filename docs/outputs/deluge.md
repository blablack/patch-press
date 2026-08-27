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

## XML: multisample synth

```xml
<sound polyphonic="poly" voicePriority="1" mode="subtractive" lpfMode="24dB">
  <osc1 type="sample" loopMode="0">
    <sampleRanges>
      <sampleRange rangeTopNote="42"
                   fileName="SAMPLES/MyPatch/note039_T120_V100_RR1.wav"
                   transpose="-9" loopMode="1">
        <zone startSamplePos="0" endSamplePos="192000"
              startLoopPos="21504" endLoopPos="45568"/>
      </sampleRange>
      <sampleRange rangeTopNote="45"
                   fileName="SAMPLES/MyPatch/note042_T120_V100_RR1.wav"
                   transpose="-6" loopMode="1">
        <zone startSamplePos="0" endSamplePos="188160"
              startLoopPos="20736" endLoopPos="44800"/>
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
- `loopMode="1"` on a range enables looping between `startLoopPos` and `endLoopPos` inside its zone. These come from the [loop detection stage](../pipeline.html#4-loop-detection).
- Round-robins are collapsed to one sample per note in the XML (the closest velocity to 100 wins). The Deluge doesn't have a native RR mechanic; extra RR captures are still on the SD card as WAV files, just not wired up.

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

## XML: wavetable synth

Wavetables use the same `<sound>` element as multisamples, but `<osc1 type="wavetable">` and no `<sampleRanges>` — the whole file is one wavetable, sweep-scanned by the Deluge's wavetable oscillator. Envelope/filter/LFO parameters come from the [archetype](../inputs/wavetables.html#archetype-detection) chosen at scan time.

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

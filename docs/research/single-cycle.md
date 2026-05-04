# Single Cycle Waves → Deluge Research

## How the Deluge Handles Single Cycle Waves

Since firmware v4.0, the Deluge auto-detects any WAV shorter than 20ms as a single-cycle waveform and loads it using the wavetable engine rather than the sample engine. It also reads Serum-format wavetables (with a `clm` tag). Files are likely already compatible — no conversion needed.

For a synth patch using a single-cycle wave, the oscillator type is `SAMP` and the file path is embedded in the patch XML. Patches live in `SYNTHS/` on the SD card as plain XML.

## Naive Approach (not very creative)

For each WAV → generate one XML patch from a fixed template. Just a file renamer with XML — produces 500 identical patches with different wavetables.

## Creative Approach: Analyse the Wave, Drive the Patch

Use `librosa`/`scipy` to analyse each wave and use the results to set patch parameters:

| Feature | Parameter driven |
|---|---|
| Spectral centroid (brightness) | Filter cutoff |
| Spectral flatness (harmonic richness) | Resonance / oscillator drive |
| Fundamental frequency | Root note / transpose |
| Odd vs even harmonic dominance | Envelope archetype (warm pad vs sharp pluck) |

## Combinatorial Layering (OSC1 + OSC2)

- Cluster waves by timbre using k-means on spectral features
- Pair OSC1 + OSC2 from *different* clusters to guarantee timbral contrast
- Avoids random pairing, produces musically coherent two-oscillator patches

## Modulation Archetypes

Define a small set of XML templates (pad, pluck, bass, lead, evolving) with different envelopes, LFO routings, filter types. Assign each wave to the best-matching archetype based on spectral analysis.

## Wavetable Sequences

If waves come from the same synth family, sort by spectral similarity and concatenate 2048-sample cycles into a Serum-format wavetable. The Deluge reads these natively — gives one patch with wavetable scanning instead of hundreds of individual patches.

## Recommended Pipeline

```
analyse → cluster → pair cross-cluster → assign archetype → generate XML
```

~100 lines of Python, produces genuinely varied patches across the whole library.

## Key XML Parameters

| Parameter | Value |
|---|---|
| `osc1` type | `sample` |
| `osc1` loopMode | `1` (loop sustain) |
| `fileName` | Relative path to WAV on SD card |
| `transpose` | Root note offset |
| Envelope | Match to archetype |
| Filter cutoff | Driven by spectral centroid |

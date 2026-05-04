# Wavetable → Deluge Research

## The Root Issue

"Same template, different sample" is boring because patch parameters aren't derived from the source material. A wavetable already contains musical character — the goal is to amplify what's already in it, not apply a generic wrapper.

## Spectral Analysis → Patch Archetypes

Analyse each wavetable and assign it to a sound design archetype automatically:

| Spectral Feature | What It Tells You | Patch Decision |
|---|---|---|
| High spectral centroid | Bright, harmonic-rich | Open filter, shorter attack, lead/pluck template |
| Low centroid + low flatness | Dark, pure tones | Closed filter, long attack/release, pad template |
| High flatness (noise-like) | Inharmonic, textural | High resonance, drone/FX template |
| Strong odd harmonics | Hollow, square-ish | Bandpass filter, mid-range presence |
| Strong even harmonics | Warm, saw-ish | Low-pass, bass or brass template |
| Amplitude variation across frames | Movement already in the wave | Slow LFO on WT position to expose it |
| Flat amplitude across frames | Static wave | Fast LFO or envelope on WT position to add movement |

## Key Creative Angle: Let the Wavetable Drive Modulation Depth

The Deluge allows modulating wavetable position with LFO2. Rather than a fixed LFO depth, measure timbral variance across wavetable frames — if there's a lot of change between frame 0 and frame N, a slow LFO sweep will be dramatic and musical. If frames are similar, a faster or deeper sweep is needed to add movement.

```python
# For each 2048-sample frame, compute spectral centroid
# then measure variance across all frames
frame_centroids = [spectral_centroid(frame) for frame in wavetable_frames]
timbral_range = max(frame_centroids) - min(frame_centroids)
# high range → slow, wide LFO sweep; low range → faster, tighter sweep
```

## Practical Archetypes (design these by hand on the Deluge first)

| Archetype | Envelope | WT Position | Filter | Notes |
|---|---|---|---|---|
| Pad | Long attack, long release | Slow LFO sweep | Low-pass semi-open | — |
| Pluck | Zero attack, fast decay, no sustain | Snapped to brightest frame | — | — |
| Bass | Short attack, medium decay | Darkest frame | High-pass rolled off | — |
| Lead | Medium attack, tight release | Most harmonically rich frame | — | — |
| Drone/texture | Very long everything | LFO on filter + WT pos | — | Reverb |
| Evolving pad | Envelope mod on WT pos | Full range sweep | — | — |

## Recommended Pipeline

1. Parse wavetable into 2048-sample frames
2. Compute per-frame spectral centroid, flatness, odd/even harmonic ratio
3. Measure timbral variance across frames
4. Classify into archetype based on features
5. Load matching XML template, set WT start position + LFO depth dynamically
6. Write to `SYNTHS/` on SD card

## End Result

Not just "500 patches" — but "500 patches where dark evolving wavetables are pads, bright harmonically-rich ones are leads, and noisy textural ones are drones." A usable library where patches feel intentional.

## Shared Ground with Single-Cycle

See `docs/research-single-cycle.md` — the spectral analysis approach and archetype concept are identical. The main difference is wavetables have multiple frames, so timbral variance across frames is an additional signal unique to wavetables.

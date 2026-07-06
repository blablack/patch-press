"""Spectral analysis → archetype classification for Serum-format wavetables.

See docs/research/wavetable.md: rather than one generic XML template for every
wavetable, analyse each file's own spectral content and let it pick the patch's
archetype (envelope/filter shape) plus a couple of continuous parameters (WT scan
start position, LFO2→position depth) driven by that file's own timbral variance.

Thresholds below are a first pass, not a calibrated model — same status as e.g.
_LOOP_TIMBRE_SEAM_MAX in runner/scan.py: tune by ear once real exports are auditioned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..model.audio import AudioBuffer

FRAME_SIZE = 2048

# Envelope (attack/decay/sustain/release, 0..1 fractions later spread across the
# Deluge's full Q31 param range by the exporter's _q31()) + filter type + a cutoff
# bias layered on top of the file's own centroid-driven cutoff. From the "Practical
# Archetypes" table in docs/research/wavetable.md.
_ARCHETYPES: dict[str, dict] = {
    "pad": dict(attack=0.7, decay=0.5, sustain=1.0, release=0.8, filter_type="lpf", cutoff_bias=-0.15),
    "pluck": dict(attack=0.0, decay=0.25, sustain=0.0, release=0.15, filter_type="lpf", cutoff_bias=0.1),
    "bass": dict(attack=0.05, decay=0.4, sustain=0.6, release=0.2, filter_type="lpf", cutoff_bias=-0.3),
    "lead": dict(attack=0.1, decay=0.3, sustain=0.85, release=0.25, filter_type="lpf", cutoff_bias=0.15),
    "drone": dict(attack=0.9, decay=0.9, sustain=1.0, release=0.9, filter_type="lpf", cutoff_bias=0.0),
    "evolving_pad": dict(attack=0.8, decay=0.6, sustain=1.0, release=0.85, filter_type="lpf", cutoff_bias=0.0),
}


@dataclass
class WavetableAnalysis:
    archetype: str
    wt_position: float
    lfo2_rate: float
    lfo2_depth: float
    filter_cutoff: float
    attack: float
    decay: float
    sustain: float
    release: float
    filter_type: str
    flags: list[str] = field(default_factory=list)


def _frame_features(audio: AudioBuffer, frame_size: int = FRAME_SIZE) -> dict | None:
    """Per-2048-sample-frame spectral features, or None if not on the WT frame grid."""
    mono = audio.to_mono().astype(np.float64)
    n = len(mono)
    if n < frame_size or n % frame_size != 0:
        return None

    n_frames = n // frame_size
    frames = mono.reshape(n_frames, frame_size)
    window = np.hanning(frame_size)

    centroids: list[float] = []
    flatnesses: list[float] = []
    odd_even_ratios: list[float] = []
    for frame in frames:
        spectrum = np.abs(np.fft.rfft(frame * window))
        spectrum[0] = 0.0  # drop DC so it can't skew centroid/flatness
        total = spectrum.sum()
        if total < 1e-9:
            centroids.append(0.0)
            flatnesses.append(0.0)
            odd_even_ratios.append(1.0)
            continue

        bins = np.arange(len(spectrum))
        centroids.append(float((bins * spectrum).sum() / total))

        nz = spectrum[spectrum > 1e-12]
        flatnesses.append(float(np.exp(np.log(nz).mean()) / nz.mean()) if len(nz) else 0.0)

        # The frame IS one wavetable cycle by format convention, so FFT bin k is
        # harmonic k directly — no separate fundamental detection needed.
        odd = spectrum[1::2].sum()
        even = spectrum[2::2].sum()
        if even > 1e-9:
            odd_even_ratios.append(float(odd / even))
        else:
            odd_even_ratios.append(float(odd) if odd > 1e-9 else 1.0)

    return {
        "centroids": centroids,
        "flatnesses": flatnesses,
        "odd_even_ratios": odd_even_ratios,
        "n_frames": n_frames,
    }


def _template_result(archetype: str, wt_position: float, lfo2_rate: float, lfo2_depth: float,
                      filter_cutoff: float, flags: list[str]) -> WavetableAnalysis:
    t = _ARCHETYPES[archetype]
    return WavetableAnalysis(
        archetype=archetype,
        wt_position=wt_position,
        lfo2_rate=lfo2_rate,
        lfo2_depth=lfo2_depth,
        filter_cutoff=filter_cutoff,
        attack=t["attack"],
        decay=t["decay"],
        sustain=t["sustain"],
        release=t["release"],
        filter_type=t["filter_type"],
        flags=flags,
    )


def classify_archetype(audio: AudioBuffer) -> WavetableAnalysis:
    """Classify a wavetable file into an archetype and derive its patch parameters."""
    feats = _frame_features(audio)
    if feats is None:
        return _template_result(
            "pad", wt_position=0.0, lfo2_rate=0.3, lfo2_depth=0.3, filter_cutoff=0.5,
            flags=["audio length is not a multiple of 2048 samples — not a valid wavetable "
                   "frame grid, defaulted to pad, verify manually"],
        )

    centroids = feats["centroids"]
    n_frames = feats["n_frames"]
    mean_centroid = float(np.mean(centroids))
    mean_flatness = float(np.mean(feats["flatnesses"]))
    mean_odd_even = float(np.mean(feats["odd_even_ratios"]))
    centroid_range = float(np.max(centroids) - np.min(centroids))

    # Normalize centroid (an FFT-bin index, 0..frame_size/2) to a 0..1 "brightness" fraction.
    norm = FRAME_SIZE / 2
    bright = mean_centroid / norm
    timbral_range = centroid_range / norm

    # Thresholds calibrated against the full 379-file Echo Sound Works Core Tables
    # corpus (see verification notes) rather than picked in the abstract: raw
    # centroid/flatness/range values for this kind of harmonic wavetable material
    # cluster much lower than a generic "0..1 fraction" intuition would suggest.
    if mean_flatness > 0.30:
        archetype = "drone"
    elif timbral_range > 0.10:
        archetype = "evolving_pad"
    elif bright > 0.10:
        # Both "bright" per the doc; split on harmonic character rather than frame
        # count (frame count turned out barely reachable as a discriminator here) —
        # strong odd harmonics read as hollow/percussive (pluck), even-dominant as
        # the more sustained, warmer "lead" character.
        archetype = "pluck" if mean_odd_even >= 1.0 else "lead"
    elif bright < 0.03 and mean_odd_even < 1.0:
        archetype = "bass"
    else:
        archetype = "pad"

    # WT start position: brightest frame for pluck/lead, darkest for pad/bass, table
    # start for drone/evolving_pad (LFO2 sweeps the whole thing anyway).
    if archetype in ("pluck", "lead"):
        idx = int(np.argmax(centroids))
    elif archetype in ("pad", "bass"):
        idx = int(np.argmin(centroids))
    else:
        idx = 0
    wt_position = idx / max(1, n_frames - 1)

    # Key creative angle from the doc: a lot of movement already in the wave → slow,
    # wide LFO2 sweep exposes it; a static wave → faster/deeper sweep manufactures it.
    if timbral_range > 0.15:
        lfo2_rate, lfo2_depth = 0.15, 0.45
    elif timbral_range > 0.05:
        lfo2_rate, lfo2_depth = 0.3, 0.6
    else:
        lfo2_rate, lfo2_depth = 0.55, 0.75

    cutoff_bias = _ARCHETYPES[archetype]["cutoff_bias"]
    filter_cutoff = max(0.05, min(0.95, bright + cutoff_bias))

    return _template_result(archetype, wt_position, lfo2_rate, lfo2_depth, filter_cutoff, flags=[])

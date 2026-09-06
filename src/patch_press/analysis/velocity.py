"""Is a preset's TIMBRE velocity-sensitive, not just its loudness?

A hardware sampler always applies its own velocity-to-volume scaling on playback, so a
preset whose only response to velocity is loudness gains nothing from capturing more
than one layer — the device already reproduces that from a single capture. What a
single capture can never reproduce is a genuine timbral difference: a softer hit that's
also duller (velocity->filter modulation), a harder hit that's also brighter or more
saturated. Multiple layers are only worth their 2-3x render/storage cost — and only
usable at all, on the one format with a real velocity-zone engine, see
io/exporters/bento.py `keeps_velocity_layers` — when that's actually present.

The measure has to be insensitive to level and sensitive to spectral shape: the
spectral centroid (energy-weighted mean frequency, expressed as a 0..1 fraction of
Nyquist) of a soft and a loud render of the same note. A uniform gain change scales
every frequency bin by the same factor, which cancels out of a weighted average — so
the centroid is naturally close to gain-invariant, and shifts specifically when the
harmonic balance itself changes.
"""

from __future__ import annotations

import numpy as np

from ..model.audio import AudioBuffer

# Minimum centroid shift before a multi-velocity capture is judged worth its cost. NOT
# derived from a corpus study the way MONO_SIDE_DB was — this is a starting knob, tune
# by ear against real presets with known velocity->filter/wave modulation.
VELOCITY_TIMBRE_SHIFT = 0.03


def _spectral_centroid(buf: AudioBuffer) -> float:
    """Energy-weighted mean frequency of a render, as a 0..1 fraction of Nyquist."""
    mono = buf.data.astype(np.float64).mean(axis=0)
    spectrum = np.abs(np.fft.rfft(mono))
    spectrum[0] = 0.0  # drop DC so it can't skew the average
    total = spectrum.sum()
    if total == 0.0 or spectrum.shape[0] < 2:
        return 0.0  # silence (or a render too short to say anything) — no signal to weigh
    bins = np.arange(spectrum.shape[0], dtype=np.float64)
    return float((bins * spectrum).sum() / total / (spectrum.shape[0] - 1))


def velocity_timbre_shift(soft: AudioBuffer, loud: AudioBuffer) -> float:
    """Spectral-centroid difference between a soft and a loud render of the same note."""
    return abs(_spectral_centroid(loud) - _spectral_centroid(soft))


def is_velocity_sensitive(shift: float, threshold: float = VELOCITY_TIMBRE_SHIFT) -> bool:
    """Whether a measured centroid shift means "this preset's timbre actually changes
    with velocity" — worth asking a capable format to ship as real velocity zones.
    """
    return shift >= threshold

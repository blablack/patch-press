"""Is a rendered preset actually stereo, or a mono signal in a stereo buffer?

A VST/CLAP plugin always hands back a stereo buffer, whether or not the preset has
anything stereo to say. Plenty don't: a patch with no unison spread and no stereo
chorus/delay/reverb runs one DSP path and writes the same samples to both channels.
Shipping that to the card as a stereo WAV doubles the file, doubles the sampler's
voice memory and doubles the SD read for a difference nobody can hear, so the scan
commands measure it once per preset and record the answer in the config.

The measure is the level of the side signal relative to the mid, over the whole
render (silence included — it contributes nothing to either):

    mid = (L + R) / 2    side = (L - R) / 2    side_db = 20*log10(rms(side) / rms(mid))

Bit-identical channels give -inf dB. A genuinely stereo patch sits within tens of dB
of the mid. Out-of-phase channels (mid ~ 0) give +inf, which is stereo by any reading.
"""

from __future__ import annotations

import math

import numpy as np

from ..model.audio import AudioBuffer

# Side level at or below which the two channels are the same signal for our purposes.
# See docs/inputs/clap-plugins.md for the measured distribution this comes from.
MONO_SIDE_DB = -60.0


def side_level_db(buf: AudioBuffer) -> float:
    """Side-to-mid level of one render, in dB. -inf when the channels are identical."""
    left = buf.data[0].astype(np.float64)
    right = buf.data[1].astype(np.float64)
    mid_rms = float(np.sqrt(np.mean(((left + right) * 0.5) ** 2)))
    side_rms = float(np.sqrt(np.mean(((left - right) * 0.5) ** 2)))
    if side_rms == 0.0:
        return -math.inf  # identical channels — including a silent render
    if mid_rms == 0.0:
        return math.inf  # channels cancel completely: as stereo as it gets
    return 20.0 * math.log10(side_rms / mid_rms)


def is_mono(side_db: float, threshold_db: float = MONO_SIDE_DB) -> bool:
    """Whether a measured side level means "one signal, duplicated"."""
    return side_db <= threshold_db


def format_side_db(side_db: float) -> str:
    """Short human form of a side level, for config comments and log lines."""
    if side_db == -math.inf:
        return "identical"
    if side_db == math.inf:
        return "anti-phase"
    return f"{side_db:.0f} dB"

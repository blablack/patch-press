"""Reading a source sound for a Bento granular patch (see docs/inputs/granular.md).

A granular oscillator chops its source into short overlapping grains. The one thing worth
reading off the audio is *where* to hold the cloud: freezing on an attack or a fade-out
gives a thin, unrepresentative drone, while the steadiest loud stretch gives the sound's
sustained character. Everything else about the patch is a fixed recipe.
"""

from __future__ import annotations

import numpy as np

from ..model.audio import AudioBuffer

_FRAME = 4096
_HOP = 2048
_WINDOW_S = 0.6  # how long a stretch has to hold steady to count
_EDGE = 0.05  # never freeze within 5% of either end
MIN_SECONDS = 1.5


def stable_position(audio: AudioBuffer) -> float:
    """The centre (0..1 of the sound's length) of its steadiest loud stretch.

    Per frame: loudness, and how much the spectrum changed from the previous frame
    (L1 distance between level-normalised magnitude spectra). A window scores its mean
    loudness relative to the loudest frame, divided by its mean spectral change -- loud
    and unchanging wins. A sound shorter than the window, or silent, returns 0.5.
    """
    mono = audio.to_mono().astype(np.float64)
    n = len(mono)
    win_frames = max(2, int(_WINDOW_S * audio.sample_rate / _HOP))
    n_frames = (n - _FRAME) // _HOP + 1
    if n_frames < win_frames + 1:
        return 0.5

    hann = np.hanning(_FRAME)
    mags = np.empty((n_frames, _FRAME // 2 + 1))
    for i in range(n_frames):
        mags[i] = np.abs(np.fft.rfft(mono[i * _HOP : i * _HOP + _FRAME] * hann))
    loud = np.sqrt((mags**2).sum(axis=1))
    if loud.max() < 1e-9:
        return 0.5
    norm = mags / (mags.sum(axis=1, keepdims=True) + 1e-12)
    flux = np.concatenate([[0.0], np.abs(np.diff(norm, axis=0)).sum(axis=1)])

    best, best_score = n_frames // 2, -1.0
    for start in range(n_frames - win_frames + 1):
        sl = slice(start, start + win_frames)
        score = (loud[sl].mean() / loud.max()) / (0.05 + flux[sl].mean())
        if score > best_score:
            best, best_score = start + win_frames // 2, score
    centre = (best * _HOP + _FRAME / 2) / n
    return float(min(1.0 - _EDGE, max(_EDGE, centre)))

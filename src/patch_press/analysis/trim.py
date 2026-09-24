import numpy as np

from ..model.audio import AudioBuffer

_FRAME = 512


def trim_bounds(buf: AudioBuffer, threshold_db: float = -60.0) -> tuple[int, int]:
    """Return (lead, trail) sample indices of the non-silent region of `buf`.

    Separated from trim_silence so callers that need to map indices from the original
    audio into the trimmed audio (e.g. a known note-off position) can shift by `lead`.

    `threshold_db` is relative to the buffer's own peak, not to full scale. An absolute
    -60 dBFS cut assumed every source peaks near 0 dBFS, and raw multitrack libraries
    don't: Just Add Drums' soft velocity layers peak around -45 dBFS, so a crash cymbal
    that rings for 4.6 s was cut after 0.23 s, once it fell a mere 15 dB. For a source
    that does peak near full scale the two readings are the same cut.
    """
    mono = buf.data.mean(axis=0)
    n = len(mono)
    peak = float(np.abs(mono).max()) if n else 0.0
    if peak == 0.0:
        return 0, n
    threshold = peak * 10 ** (threshold_db / 20.0)

    lead = 0
    for i in range(0, n, _FRAME):
        if np.abs(mono[i : i + _FRAME]).max() >= threshold:
            lead = i
            break

    trail = n
    for i in range(n, 0, -_FRAME):
        if np.abs(mono[max(0, i - _FRAME) : i]).max() >= threshold:
            trail = i
            break

    return lead, trail


def trim_silence(buf: AudioBuffer, threshold_db: float = -60.0) -> AudioBuffer:
    lead, trail = trim_bounds(buf, threshold_db)
    return AudioBuffer(data=buf.data[:, lead:trail], sample_rate=buf.sample_rate)

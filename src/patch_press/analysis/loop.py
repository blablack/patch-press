import numpy as np

from ..model.audio import AudioBuffer

_FRAME = 2048
_MIN_GAP_S = 0.25
_MAX_GAP_S = 2.0
_STRIDE = 4  # check every Nth zero crossing to keep runtime reasonable


def find_loop_points(
    buf: AudioBuffer,
    quality_threshold: float = 0.8,
) -> tuple[tuple[int, int], float] | None:
    """
    Find loop (start, end) frame indices with a spectral-similarity score.
    Returns ((start, end), quality) or None if no acceptable loop is found.
    """
    mono = buf.data.mean(axis=0)
    sr = buf.sample_rate
    n = len(mono)

    region_start = n // 4
    region_end = 3 * n // 4
    if region_end - region_start < sr // 4:
        return None

    segment = mono[region_start:region_end]
    signs = np.sign(segment)
    crossings = np.where(np.diff(signs) != 0)[0] + region_start
    if len(crossings) < 2:
        return None

    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)
    half = _FRAME // 2
    best_quality = 0.0
    best_pair: tuple[int, int] | None = None

    candidates = crossings[::_STRIDE]
    for start in candidates:
        for end in candidates:
            gap = end - start
            if gap < min_gap or gap > max_gap:
                continue
            a = mono[max(0, start - half) : start + half]
            b = mono[max(0, end - half) : end + half]
            if len(a) != len(b) or len(a) < 2:
                continue
            if a.std() == 0 or b.std() == 0:
                continue
            quality = float(np.corrcoef(a, b)[0, 1])
            quality = max(0.0, quality)
            if quality > best_quality:
                best_quality = quality
                best_pair = (int(start), int(end))

    if best_pair is None or best_quality < quality_threshold:
        return None

    return best_pair, best_quality

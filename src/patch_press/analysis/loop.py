import numpy as np

from ..model.audio import AudioBuffer

_FRAME = 2048
_MIN_GAP_S = 0.25
_MAX_GAP_S = 8.0
_MAX_CANDIDATES = 300

_ENV_HOP = 512
_ENV_MIN_PERIOD_S = 0.25
_ENV_MAX_PERIOD_S = 8.0
_REFINE_RADIUS = 256
_SLOPE_WINDOW = 16

# Quarter-note multipliers covering common tempo-synced delay/LFO rates
_TEMPO_SUBDIVISIONS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 8.0, 16.0]


def _rms_envelope(mono: np.ndarray, hop: int) -> np.ndarray:
    n_hops = len(mono) // hop
    frames = mono[: n_hops * hop].reshape(n_hops, hop)
    return np.sqrt(np.mean(frames**2, axis=1))


def _detect_macro_period(mono: np.ndarray, sr: int) -> int | None:
    env = _rms_envelope(mono, _ENV_HOP)
    if len(env) < 4:
        return None
    env_ac = env - env.mean()
    n = len(env_ac)
    fft_env = np.fft.rfft(env_ac, n=2 * n)
    autocorr = np.fft.irfft(np.abs(fft_env) ** 2)[:n]
    autocorr /= autocorr[0] + 1e-12

    min_lag = max(1, int(_ENV_MIN_PERIOD_S * sr / _ENV_HOP))
    max_lag = min(n // 2, int(_ENV_MAX_PERIOD_S * sr / _ENV_HOP))
    if min_lag >= max_lag:
        return None

    search = autocorr[min_lag:max_lag]
    if search.max() < 0.25:
        return None

    peak_frame = min_lag + int(np.argmax(search))
    return peak_frame * _ENV_HOP


def _boundary_score(
    mono: np.ndarray,
    start: int,
    end: int,
) -> float:
    n = len(mono)
    half = _FRAME // 2

    # NCC of windows around start and end
    a = mono[max(0, start - half) : start + half]
    b = mono[max(0, end - half) : end + half]
    if len(a) != len(b) or len(a) < 2:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    denom = np.sqrt((a * a).sum() * (b * b).sum())
    ncc = max(0.0, float(np.dot(a, b) / denom)) if denom > 0 else 0.0

    # Amplitude match at the exact boundary frame
    xs = float(mono[start])
    xe = float(mono[end])
    max_amp = max(abs(xs), abs(xe), 1e-9)
    amp_score = 1.0 - min(abs(xs - xe) / max_amp, 1.0)

    # Slope match (first derivative estimated over _SLOPE_WINDOW)
    w = _SLOPE_WINDOW
    s_start = float(mono[min(start + w, n - 1)] - mono[max(start - w, 0)])
    s_end = float(mono[min(end + w, n - 1)] - mono[max(end - w, 0)])
    max_slope = max(abs(s_start), abs(s_end), 1e-9)
    slope_score = 1.0 - min(abs(s_start - s_end) / max_slope, 1.0)

    return 0.6 * ncc + 0.25 * amp_score + 0.15 * slope_score


def _tempo_candidates(
    sr: int,
    tempo_bpm: float,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int]]:
    quarter_frames = int(60.0 / tempo_bpm * sr)
    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)
    pairs: list[tuple[int, int]] = []
    for mult in _TEMPO_SUBDIVISIONS:
        loop_len = int(mult * quarter_frames)
        if loop_len < min_gap or loop_len > max_gap:
            continue
        for start in range(region_start, region_end - loop_len, _REFINE_RADIUS):
            pairs.append((start, start + loop_len))
    return pairs


def _macro_period_candidates(
    sr: int,
    t_macro: int,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int]]:
    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)
    multipliers = [N for N in range(1, 33) if min_gap <= N * t_macro <= max_gap]
    pairs: list[tuple[int, int]] = []
    step = max(1, t_macro // 4)
    for N in multipliers:
        loop_len = N * t_macro
        for start in range(region_start, region_end - loop_len, step):
            pairs.append((start, start + loop_len))
    return pairs


def _best_from_candidates(
    mono: np.ndarray,
    candidates: list[tuple[int, int]],
) -> tuple[tuple[int, int], float] | None:
    n = len(mono)
    best_score = 0.0
    best_pair: tuple[int, int] | None = None
    for start, end in candidates:
        if start < 0 or end >= n - _FRAME:
            continue
        # Local refinement: slide ±REFINE_RADIUS keeping loop length fixed
        loop_len = end - start
        for delta in range(-_REFINE_RADIUS, _REFINE_RADIUS + 1, 8):
            s = start + delta
            e = s + loop_len
            if s < 0 or e >= n - _FRAME:
                continue
            score = _boundary_score(mono, s, e)
            if score > best_score:
                best_score = score
                best_pair = (s, e)
    return (best_pair, best_score) if best_pair is not None else None


def find_loop_points(
    buf: AudioBuffer,
    quality_threshold: float = 0.8,
    tempo_bpm: float | None = None,
) -> tuple[tuple[int, int], float] | None:
    mono = buf.to_mono()
    sr = buf.sample_rate
    n = len(mono)

    region_start = n // 4
    region_end = 3 * n // 4
    if region_end - region_start < sr // 4:
        return None

    best_quality = 0.0
    best_pair: tuple[int, int] | None = None

    # Phase 1: tempo-based candidates
    if tempo_bpm is not None:
        candidates = _tempo_candidates(sr, tempo_bpm, region_start, region_end)
        result = _best_from_candidates(mono, candidates)
        if result is not None:
            best_pair, best_quality = result

    # Phase 2: envelope autocorrelation fallback
    if best_quality < quality_threshold:
        t_macro = _detect_macro_period(mono, sr)
        if t_macro is not None:
            candidates = _macro_period_candidates(sr, t_macro, region_start, region_end)
            result = _best_from_candidates(mono, candidates)
            if result is not None and result[1] > best_quality:
                best_pair, best_quality = result

    # Phase 3: zero-crossing fallback
    if best_quality < quality_threshold:
        segment = mono[region_start:region_end]
        signs = np.sign(segment)
        crossings = np.where(np.diff(signs) != 0)[0] + region_start
        if len(crossings) >= 2:
            if len(crossings) > _MAX_CANDIDATES:
                idx = np.round(np.linspace(0, len(crossings) - 1, _MAX_CANDIDATES)).astype(int)
                crossings = crossings[idx]
            min_gap = int(_MIN_GAP_S * sr)
            max_gap = int(_MAX_GAP_S * sr)
            for start in crossings:
                for end in crossings:
                    gap = end - start
                    if gap < min_gap or gap > max_gap:
                        continue
                    if end >= n - _FRAME:
                        continue
                    score = _boundary_score(mono, int(start), int(end))
                    if score > best_quality:
                        best_quality = score
                        best_pair = (int(start), int(end))

    if best_pair is None or best_quality < quality_threshold:
        return None

    return best_pair, best_quality

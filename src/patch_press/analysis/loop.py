import logging

import librosa
import numpy as np

from ..model.audio import AudioBuffer
from .envelope import EnvelopeResult

log = logging.getLogger(__name__)

_FRAME = 2048
_MIN_GAP_S = 1.0
_MAX_GAP_S = 8.0

_ENV_HOP = 512
_SLOPE_WINDOW = 16
_ZC_SNAP_RADIUS = 512
_CHROMA_WIN = 4096

# Coarse chroma grid for complex-pad loop discovery
_CHROMA_GRID_HOP_S = 0.5   # seconds between fingerprint samples
_CHROMA_GRID_MIN_SIM = 0.85

# RMS fraction below which we consider the sustain ended (start of release)
_SUSTAIN_THRESHOLD = 0.4

# Splice validation thresholds (fraction of peak amplitude).
# Chroma is the main quality gate; amplitude and derivative prevent audible clicks.
# Zero-crossing snap means amplitude at splice is legitimately ~10–20% of peak.
_AMP_DISC_THRESHOLD = 0.25
# Derivative check uses a 16-sample window normalized by 2×peak. Threshold 0.5
# catches opposite-slope splices (rising vs falling) while passing matched slopes.
_DERIV_DISC_THRESHOLD = 0.50

# Quarter-note multipliers covering common tempo-synced delay/LFO rates
_TEMPO_SUBDIVISIONS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 8.0, 16.0]


def _rms_envelope(mono: np.ndarray, hop: int) -> np.ndarray:
    n_hops = len(mono) // hop
    frames = mono[: n_hops * hop].reshape(n_hops, hop)
    return np.sqrt(np.mean(frames**2, axis=1))


def _detect_sustain_region(mono: np.ndarray, sr: int) -> tuple[int, int]:
    """Return (start_sample, end_sample) covering the estimated sustain plateau."""
    n = len(mono)
    env = _rms_envelope(mono, _ENV_HOP)

    if len(env) < 4:
        return n // 4, 3 * n // 4

    peak_frame = int(np.argmax(env))
    peak_val = float(env[peak_frame])
    if peak_val < 1e-6:
        return n // 4, 3 * n // 4

    region_start = peak_frame * _ENV_HOP

    threshold = _SUSTAIN_THRESHOLD * peak_val
    after_peak = env[peak_frame:]
    drop_frames = np.where(after_peak < threshold)[0]
    if len(drop_frames) > 0:
        region_end = min((peak_frame + int(drop_frames[0])) * _ENV_HOP, n)
    else:
        region_end = n

    if region_end - region_start < int(_MIN_GAP_S * 2 * sr):
        return n // 4, 3 * n // 4

    return region_start, region_end


def _snap_to_slope_zero_crossing(mono: np.ndarray, frame: int, rising: bool) -> int:
    """Return nearest zero crossing to frame that matches the requested slope direction.

    Returns Z such that the sign change is between mono[Z] and mono[Z+1].
    mono[Z] is the last sample on one side of the crossing (small value, near zero).
    """
    lo = max(1, frame - _ZC_SNAP_RADIUS)
    hi = min(len(mono) - 2, frame + _ZC_SNAP_RADIUS)
    segment = mono[lo : hi + 1]
    diffs = np.diff(np.sign(segment))
    crossings = np.where(diffs > 0)[0] if rising else np.where(diffs < 0)[0]
    if len(crossings) == 0:
        crossings = np.where(diffs != 0)[0]
    if len(crossings) == 0:
        return frame
    nearest = int(crossings[np.argmin(np.abs(crossings - (frame - lo)))])
    return lo + nearest


def _midi_to_period(sr: int, midi_note: int) -> int:
    freq = 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
    return max(1, int(round(sr / freq)))


def _detect_waveform_period(
    mono: np.ndarray,
    sr: int,
    region_start: int,
    region_end: int,
    hint_period: int | None = None,
) -> int | None:
    """Detect fundamental period via waveform autocorrelation on a sustain chunk.

    hint_period narrows the search to ±3 semitones around the expected period
    and is used as a direct fallback if autocorrelation finds no clear peak.
    """
    chunk_len = min(region_end - region_start, int(0.5 * sr))
    mid = (region_start + region_end) // 2
    chunk = mono[mid - chunk_len // 2 : mid + chunk_len // 2]
    if len(chunk) < 64:
        return hint_period
    chunk = chunk - chunk.mean()
    n = len(chunk)
    fft_c = np.fft.rfft(chunk, n=2 * n)
    autocorr = np.fft.irfft(np.abs(fft_c) ** 2)[:n]
    autocorr /= autocorr[0] + 1e-12

    # ±3 semitones ≈ ×0.84 / ×1.19 in period (inverse of frequency)
    if hint_period is not None:
        min_lag = max(1, int(hint_period * 0.84))
        max_lag = min(n // 2, int(hint_period * 1.19))
    else:
        min_lag = max(1, int(sr / 4000))
        max_lag = min(n // 2, int(sr / 20))

    if min_lag >= max_lag:
        return hint_period
    search = autocorr[min_lag:max_lag]
    if search.max() < 0.3:
        return hint_period
    return min_lag + int(np.argmax(search))


def _chroma_score(mono: np.ndarray, sr: int, start: int, end: int) -> float:
    """Cosine similarity of mean chroma vectors on either side of the splice boundary.

    Compares the harmonic content just before loop_end to just after loop_start.
    end is treated as exclusive (Deluge convention), so pre-window ends at end-1.
    """
    pre = mono[max(0, end - _CHROMA_WIN) : end]
    post = mono[start : min(len(mono), start + _CHROMA_WIN)]
    if len(pre) < _CHROMA_WIN // 4 or len(post) < _CHROMA_WIN // 4:
        return 0.0
    n_fft_a = min(2048, len(pre))
    n_fft_b = min(2048, len(post))
    ca = librosa.feature.chroma_stft(y=pre, sr=sr, n_fft=n_fft_a, tuning=0.0).mean(axis=1)
    cb = librosa.feature.chroma_stft(y=post, sr=sr, n_fft=n_fft_b, tuning=0.0).mean(axis=1)
    denom = np.linalg.norm(ca) * np.linalg.norm(cb)
    return float(np.dot(ca, cb) / denom) if denom > 0 else 0.0


def _boundary_score(
    mono: np.ndarray,
    sr: int,
    start: int,
    end: int,
    pre_chroma_score: float = 0.0,
) -> float:
    """Combined score: chroma + amplitude match + slope match."""
    n = len(mono)

    xs = float(mono[start])
    xe = float(mono[end])
    max_amp = max(abs(xs), abs(xe), 1e-9)
    amp_score = 1.0 - min(abs(xs - xe) / max_amp, 1.0)

    w = _SLOPE_WINDOW
    s_start = float(mono[min(start + w, n - 1)] - mono[max(start - w, 0)])
    s_end = float(mono[min(end + w, n - 1)] - mono[max(end - w, 0)])
    max_slope = max(abs(s_start), abs(s_end), 1e-9)
    slope_score = 1.0 - min(abs(s_start - s_end) / max_slope, 1.0)

    chroma = pre_chroma_score if pre_chroma_score > 0 else _chroma_score(mono, sr, start, end)

    return 0.6 * chroma + 0.25 * amp_score + 0.15 * slope_score


def validate_splice_reason(mono: np.ndarray, sr: int, start: int, end: int) -> str:
    """Return empty string if the splice is clean, else a description of the failing check.

    end is treated as exclusive: the loop plays [start, end), so the last sample
    before jumping back is mono[end-1]. After zero-crossing snap with the +1 shift
    applied in find_loop_points, mono[end-1] is at the crossing itself (small value).
    """
    if start < 1 or end < 2 or end >= len(mono):
        return "out of bounds"
    n = len(mono)
    peak = float(np.abs(mono).max()) or 1.0
    amp_disc = abs(float(mono[end - 1]) - float(mono[start])) / peak
    w = _SLOPE_WINDOW
    slope_end = float(mono[end - 1]) - float(mono[max(end - 1 - w, 0)])
    slope_start = float(mono[min(start + w, n - 1)]) - float(mono[start])
    deriv_disc = abs(slope_end - slope_start) / (2.0 * peak)
    if amp_disc >= _AMP_DISC_THRESHOLD:
        return f"amp_disc={amp_disc:.3f} (threshold {_AMP_DISC_THRESHOLD})"
    if deriv_disc >= _DERIV_DISC_THRESHOLD:
        return f"deriv_disc={deriv_disc:.3f} (threshold {_DERIV_DISC_THRESHOLD})"
    return ""


def _chroma_grid_candidates(
    mono: np.ndarray,
    sr: int,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int, float]]:
    """Find loop candidates by comparing coarse chroma fingerprints across the sustain region.

    Samples chroma every _CHROMA_GRID_HOP_S seconds, finds all pairs whose loop length
    is in [_MIN_GAP_S, _MAX_GAP_S] with similarity >= _CHROMA_GRID_MIN_SIM.
    Returns (start, end, similarity) — similarity is passed as pre_chroma_score to avoid
    recomputing chroma at boundary-score time.
    """
    hop = int(_CHROMA_GRID_HOP_S * sr)
    cap = min(region_end, region_start + int(_MAX_GAP_S * sr))
    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)

    fingerprints: list[tuple[int, np.ndarray]] = []
    for pos in range(region_start, cap - _CHROMA_WIN, hop):
        chunk = mono[pos : pos + _CHROMA_WIN]
        if len(chunk) < _CHROMA_WIN // 4:
            break
        n_fft = min(2048, len(chunk))
        c = librosa.feature.chroma_stft(y=chunk, sr=sr, n_fft=n_fft, tuning=0.0).mean(axis=1)
        fingerprints.append((pos, c))

    pairs: list[tuple[int, int, float]] = []
    for i, (p1, c1) in enumerate(fingerprints):
        for p2, c2 in fingerprints[i + 1 :]:
            loop_len = p2 - p1
            if loop_len < min_gap or loop_len > max_gap:
                continue
            denom = np.linalg.norm(c1) * np.linalg.norm(c2)
            if denom == 0:
                continue
            sim = float(np.dot(c1, c2) / denom)
            if sim >= _CHROMA_GRID_MIN_SIM:
                pairs.append((p1, p2, sim))
    return pairs


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
        for start in range(region_start, region_end - loop_len, sr):
            pairs.append((start, start + loop_len))
    return pairs


def _waveform_period_candidates(
    sr: int,
    t_period: int,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int]]:
    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)
    target_secs = [1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0]
    seen: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for d in target_secs:
        N = max(1, round(d * sr / t_period))
        loop_len = N * t_period
        if loop_len < min_gap or loop_len > max_gap or loop_len in seen:
            continue
        seen.add(loop_len)
        available = region_end - region_start - loop_len
        if available <= 0:
            continue
        for frac in [0.25, 0.5, 0.75]:
            start = region_start + int(frac * available)
            pairs.append((start, start + loop_len))
    return pairs


def find_loop_points(
    buf: AudioBuffer,
    quality_threshold: float = 0.8,
    tempo_bpm: float | None = None,
    envelope: EnvelopeResult | None = None,
    midi_note: int | None = None,
) -> tuple[tuple[int, int] | None, float]:
    mono = buf.to_mono()
    sr = buf.sample_rate
    n = len(mono)

    # Pluck sounds don't loop
    if envelope is not None and envelope.classification == "pluck":
        return None, 0.0

    # Use envelope's sustain region if available, else detect it
    if envelope is not None:
        region_start = envelope.sustain_start
        region_end = envelope.sustain_end
    else:
        region_start, region_end = _detect_sustain_region(mono, sr)

    if region_end - region_start < sr // 4:
        return None, 0.0

    t_mod = envelope.modulation_period_samples if envelope is not None else None

    hint_period = _midi_to_period(sr, midi_note) if midi_note is not None else None

    # --- Gather candidates from all sources ---
    # (start, end, pre_chroma_score) where pre_chroma_score=0.0 means compute at scoring time
    candidates: list[tuple[int, int, float]] = []

    # Chroma grid: finds loop points for complex pads with no simple waveform period
    candidates.extend(_chroma_grid_candidates(mono, sr, region_start, region_end))

    # Waveform period: integer multiples of fundamental — reliable for smooth periodic pads
    # hint_period focuses autocorrelation around the expected pitch; falls back to it if weak
    t_wave = _detect_waveform_period(mono, sr, region_start, region_end, hint_period)
    if t_wave is not None:
        for s, e in _waveform_period_candidates(sr, t_wave, region_start, region_end):
            candidates.append((s, e, 0.0))

    # Tempo subdivisions: for BPM-synced modulation (LFO delays, arps)
    if tempo_bpm is not None:
        for s, e in _tempo_candidates(sr, tempo_bpm, region_start, region_end):
            candidates.append((s, e, 0.0))

    # Constrain loop lengths to integer multiples of the modulation period
    if t_mod and candidates:
        candidates = [
            (s, e, sc) for s, e, sc in candidates
            if (e - s) % t_mod < t_mod * 0.1
        ]

    # Score all candidates; chroma grid entries reuse their pre-computed similarity
    best_quality = 0.0
    best_pair: tuple[int, int] | None = None
    for s, e, pre_score in candidates:
        if s < 0 or e >= n - _FRAME:
            continue
        score = _boundary_score(mono, sr, s, e, pre_chroma_score=pre_score)
        if score > best_quality:
            best_quality = score
            best_pair = (s, e)

    if best_pair is None or best_quality < quality_threshold:
        return None, best_quality

    # Zero-crossing snap: for start, land ON the crossing (mono[s] ≈ 0).
    # For end, land ONE PAST the crossing (end = Z+1) so the last played
    # sample is mono[end-1] = mono[Z], which is at the crossing (≈ 0).
    # This ensures both edges of the splice are at small amplitude values.
    s, e = best_pair
    rising = float(mono[min(s + 4, n - 1)]) > float(mono[max(s - 4, 0)])
    s = _snap_to_slope_zero_crossing(mono, s, rising)
    e = _snap_to_slope_zero_crossing(mono, e, rising) + 1

    if s >= e or e - s < int(_MIN_GAP_S * sr) or e >= n:
        return None, best_quality

    return (s, e), best_quality


def bake_loop_crossfade(
    buf: AudioBuffer,
    loop_start: int,
    loop_end: int,
    fade_ms: float,
) -> AudioBuffer:
    sr = buf.sample_rate
    fade_len = int(sr * fade_ms / 1000.0)
    fade_len = min(fade_len, (loop_end - loop_start) // 4)
    if fade_len < 8:
        return buf
    out = AudioBuffer(data=buf.data.copy(), sample_rate=sr)
    t = np.linspace(0.0, 1.0, fade_len, dtype=np.float32)
    tail = buf.data[:, loop_end - fade_len : loop_end]
    head = buf.data[:, loop_start : loop_start + fade_len]
    out.data[:, loop_end - fade_len : loop_end] = tail * np.sqrt(1.0 - t) + head * np.sqrt(t)
    return out

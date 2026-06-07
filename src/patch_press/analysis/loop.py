import json
import logging
import os

import librosa
import numpy as np

from ..model.audio import AudioBuffer
from .envelope import (
    EnvelopeResult,
    _MIN_PLATEAU_S,
    _PLATEAU_FLATNESS,
    _PLATEAU_FLOOR,
)

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

# Candidate ranking. These do NOT change which candidates pass the quality gate (that
# stays the base seam score in _boundary_components) — they only reorder the passing
# candidates so the caller's first pick favours a longer loop sitting on a stable part of
# the envelope, instead of the shortest seam-clean window. Length is measured in time, not
# period count: a high note can span hundreds of cycles in a few milliseconds.
_LOOP_LEN_TARGET_S = 1.5   # loops at/above this length earn the full length reward
_W_LENGTH = 0.15           # weight of the length reward
_W_PLACEMENT = 0.15        # penalty for amplitude drift between loop start and end (loop on a slope)


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

    # Extend-only plateau check — mirrors analyze_envelope (Fix #2). Only reached when no
    # EnvelopeResult is supplied; keeps this fallback consistent with the main path.
    if region_end < n:
        end_frame = region_end // _ENV_HOP
        tail = env[end_frame:]
        min_plateau_frames = max(int(_MIN_PLATEAU_S * sr / _ENV_HOP), 2)
        if len(tail) >= min_plateau_frames:
            settle = tail[len(tail) // 2:]
            settle_mean = float(settle.mean())
            settle_cv = float(settle.std() / settle_mean) if settle_mean > 1e-6 else float("inf")
            if settle_mean >= _PLATEAU_FLOOR * peak_val and settle_cv <= _PLATEAU_FLATNESS:
                above = np.where(tail >= _PLATEAU_FLOOR * peak_val)[0]
                if len(above) > 0:
                    plateau_end = min((end_frame + int(above[-1]) + 1) * _ENV_HOP, n)
                    region_end = max(region_end, plateau_end)

    if region_end - region_start < int(_MIN_GAP_S * 2 * sr):
        return n // 4, 3 * n // 4

    return region_start, region_end


def _snap_to_slope_zero_crossing(
    mono: np.ndarray, frame: int, rising: bool, radius: int = _ZC_SNAP_RADIUS
) -> int:
    """Return nearest zero crossing to frame that matches the requested slope direction.

    Returns Z such that the sign change is between mono[Z] and mono[Z+1].
    mono[Z] is the last sample on one side of the crossing (small value, near zero).
    radius should be at least half the fundamental period so bass notes can always
    find a matching zero crossing.
    """
    lo = max(1, frame - radius)
    hi = min(len(mono) - 2, frame + radius)
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


def _boundary_components(
    mono: np.ndarray,
    sr: int,
    start: int,
    end: int,
) -> dict:
    """Chroma/amp/slope components and the weighted score for the seam mono[end-1] → mono[start].

    end is exclusive (loop plays [start, end)). Both amplitude and slope are evaluated at
    the actual seam samples, matching validate_splice_reason. Chroma is computed at the
    (post-snap) endpoints so it reflects the real end→start seam, not the grid fingerprint.
    """
    n = len(mono)

    xs = float(mono[start])
    xe = float(mono[end - 1])   # last sample before the jump, not first after
    max_amp = max(abs(xs), abs(xe), 1e-9)
    amp_score = 1.0 - min(abs(xs - xe) / max_amp, 1.0)

    w = _SLOPE_WINDOW
    s_start = float(mono[min(start + w, n - 1)]) - float(mono[start])
    s_end = float(mono[end - 1]) - float(mono[max(end - 1 - w, 0)])
    max_slope = max(abs(s_start), abs(s_end), 1e-9)
    slope_score = 1.0 - min(abs(s_start - s_end) / max_slope, 1.0)

    chroma = _chroma_score(mono, sr, start, end)

    score = 0.6 * chroma + 0.25 * amp_score + 0.15 * slope_score
    return {"chroma": chroma, "amp_score": amp_score, "slope_score": slope_score, "score": score}


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


def _chroma_fingerprints(
    mono: np.ndarray, sr: int, start: int, end: int, hop: int
) -> list[tuple[int, np.ndarray]]:
    """Sample mean-chroma fingerprints every `hop` samples across [start, end)."""
    fps: list[tuple[int, np.ndarray]] = []
    for pos in range(start, end - _CHROMA_WIN, hop):
        chunk = mono[pos : pos + _CHROMA_WIN]
        if len(chunk) < _CHROMA_WIN // 4:
            break
        n_fft = min(2048, len(chunk))
        c = librosa.feature.chroma_stft(y=chunk, sr=sr, n_fft=n_fft, tuning=0.0).mean(axis=1)
        fps.append((pos, c))
    return fps


def _chroma_grid_candidates(
    mono: np.ndarray,
    sr: int,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int, float]]:
    """Find loop candidates by comparing coarse chroma fingerprints across the sustain region.

    Samples chroma every _CHROMA_GRID_HOP_S seconds, finds all pairs whose loop length
    is in [_MIN_GAP_S, _MAX_GAP_S] with similarity >= _CHROMA_GRID_MIN_SIM.
    Returns (start, end, similarity). The similarity is the forward/forward fingerprint
    cosine used only as the generation gate (and a diagnostic) — it is NOT the seam
    score: the real end→start seam chroma is recomputed in _boundary_components.
    """
    hop = int(_CHROMA_GRID_HOP_S * sr)
    cap = min(region_end, region_start + int(_MAX_GAP_S * sr))
    min_gap = int(_MIN_GAP_S * sr)
    max_gap = int(_MAX_GAP_S * sr)

    fingerprints = _chroma_fingerprints(mono, sr, region_start, cap, hop)

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


def _refine_period_candidate(
    mono: np.ndarray, start: int, loop_len: int, half_period: int, window: int = 64
) -> tuple[int, int]:
    """Slide ±half_period around start to find the sub-period offset with lowest splice SSD.

    Compares context-after-start to context-before-end: both should look identical for
    a perfectly period-aligned loop. Returns (best_start, best_start + loop_len).
    """
    window = min(window, loop_len // 4)
    if window < 4:
        return start, start + loop_len
    lo = max(window, start - half_period)
    hi = min(len(mono) - loop_len - window, start + half_period)
    if lo >= hi:
        return start, start + loop_len

    starts = np.arange(lo, hi + 1)
    w = np.arange(window)
    # context just after start: mono[s : s+window]
    s_idx = starts[:, None] + w[None, :]
    # context just before end: mono[s+loop_len-window : s+loop_len]
    e_idx = (starts + loop_len - window)[:, None] + w[None, :]

    if s_idx.max() >= len(mono) or e_idx.max() >= len(mono) or e_idx.min() < 0:
        return start, start + loop_len

    ssds = np.sum((mono[s_idx] - mono[e_idx]) ** 2, axis=1)
    best_s = int(starts[np.argmin(ssds)])
    return best_s, best_s + loop_len


def _waveform_period_candidates(
    sr: int,
    t_period: int,
    region_start: int,
    region_end: int,
) -> list[tuple[int, int]]:
    # Period-based floor: 50 ms or 4 periods, whichever is larger.
    # Avoids forcing high-pitched notes into 1s loops where drift accumulates.
    min_gap = max(int(0.05 * sr), 4 * t_period)
    max_gap = int(_MAX_GAP_S * sr)
    target_secs = [0.05, 0.1, 0.2, 0.5, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0]
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


# ── Diagnostics (env-gated; zero effect on output when PATCHPRESS_LOOP_DEBUG is unset) ──

_loop_debug_seq = 0


def _loop_debug_path() -> str | None:
    """Base path for per-note loop diagnostics, or None when disabled.

    Enabled by PATCHPRESS_LOOP_DEBUG in {1,true,yes}. Each process appends to a
    PID-suffixed file so parallel ProcessPoolExecutor workers never interleave writes.
    """
    if os.environ.get("PATCHPRESS_LOOP_DEBUG", "").lower() not in ("1", "true", "yes"):
        return None
    return os.environ.get("PATCHPRESS_LOOP_DEBUG_PATH") or "loop_debug.jsonl"


def _json_default(o):
    """Coerce numpy scalars/arrays to plain Python types for the debug JSONL writer."""
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.ndarray):
        return o.tolist()
    return str(o)


def _loop_debug_emit(record: dict) -> None:
    base = _loop_debug_path()
    if not base:
        return
    path = f"{base}.{os.getpid()}.jsonl"
    try:
        with open(path, "a") as f:
            f.write(json.dumps(record, default=_json_default) + "\n")
    except Exception:
        # Diagnostics must never break a render — log and carry on.
        log.warning("loop debug emit failed for %s", path, exc_info=True)


def _chroma_movement(fingerprints: list[tuple[int, np.ndarray]]) -> float:
    """Mean cosine distance of the per-0.5s chroma vectors to their centroid.

    ~0 means harmonically static (Dexed/Odin2); higher means the timbre/pitch moves
    across the sustain. This is the sensor the RMS-based modulation gate in envelope.py
    lacks: a filter sweep moves chroma but barely moves RMS.
    """
    if len(fingerprints) < 2:
        return 0.0
    vecs = np.stack([c / (np.linalg.norm(c) + 1e-12) for _, c in fingerprints])
    centroid = vecs.mean(axis=0)
    centroid /= np.linalg.norm(centroid) + 1e-12
    return float(np.mean(1.0 - vecs @ centroid))


def _centroid_movement(mono: np.ndarray, sr: int, start: int, end: int) -> float:
    """Coefficient of variation of the spectral centroid across [start, end)."""
    seg = mono[start:end]
    if len(seg) < 2048:
        return 0.0
    sc = librosa.feature.spectral_centroid(y=seg, sr=sr)[0]
    m = float(sc.mean())
    return float(sc.std() / m) if m > 0 else 0.0


def _rms_depth_region(mono: np.ndarray, start: int, end: int) -> float:
    """(max-min)/mean of the RMS envelope over [start, end) — mirrors envelope.py's 0.40 gate."""
    env = _rms_envelope(mono, _ENV_HOP)
    s = start // _ENV_HOP
    e = min(end // _ENV_HOP, len(env))
    seg = env[s:e]
    if len(seg) == 0:
        return 0.0
    m = float(seg.mean())
    return float((seg.max() - seg.min()) / m) if m > 1e-6 else 0.0


def _seam_disc(mono: np.ndarray, start: int, end: int) -> tuple[float, float]:
    """Raw amp/deriv discontinuity at the seam — mirrors validate_splice_reason."""
    n = len(mono)
    peak = float(np.abs(mono).max()) or 1.0
    amp_disc = abs(float(mono[end - 1]) - float(mono[start])) / peak
    w = _SLOPE_WINDOW
    slope_end = float(mono[end - 1]) - float(mono[max(end - 1 - w, 0)])
    slope_start = float(mono[min(start + w, n - 1)]) - float(mono[start])
    deriv_disc = abs(slope_end - slope_start) / (2.0 * peak)
    return amp_disc, deriv_disc


def find_loop_candidates(
    buf: AudioBuffer,
    quality_threshold: float = 0.8,
    tempo_bpm: float | None = None,
    envelope: EnvelopeResult | None = None,
    midi_note: int | None = None,
    max_candidates: int = 5,
) -> list[tuple[tuple[int, int], float]]:
    """Return up to max_candidates loop points sorted best-first, all above quality_threshold.

    Each entry is ((loop_start, loop_end), quality_score). The caller should try each
    in order, applying crossfade and splice validation, and use the first that passes.
    """
    mono = buf.to_mono()
    sr = buf.sample_rate
    n = len(mono)
    dbg = _loop_debug_path() is not None
    cls = envelope.classification if envelope is not None else None

    def _emit(reason: str, region: tuple[int, int] | None, extra: dict) -> None:
        global _loop_debug_seq
        if not dbg:
            return
        _loop_debug_seq += 1
        rec = {
            "seq": _loop_debug_seq,
            "midi_note": midi_note,
            "sr": sr,
            "n_samples": n,
            "classification": cls,
            "tempo_bpm": tempo_bpm,
            "t_mod": (envelope.modulation_period_samples if envelope is not None else None),
            "sustain_start": region[0] if region else None,
            "sustain_end": region[1] if region else None,
            "quality_threshold": quality_threshold,
            "reason": reason,
        }
        rec.update(extra)
        _loop_debug_emit(rec)

    if envelope is not None and envelope.classification == "pluck":
        _emit("pluck", (envelope.sustain_start, envelope.sustain_end), {"candidates": []})
        return []

    if envelope is not None:
        region_start = envelope.sustain_start
        region_end = envelope.sustain_end
    else:
        region_start, region_end = _detect_sustain_region(mono, sr)

    if region_end - region_start < sr // 4:
        _emit("region_too_short", (region_start, region_end), {"candidates": []})
        return []

    t_mod = envelope.modulation_period_samples if envelope is not None else None
    hint_period = _midi_to_period(sr, midi_note) if midi_note is not None else None

    # Gather candidates from all sources, tagged by generator for diagnostics.
    raw: list[tuple[int, int, float, str]] = []
    raw.extend(
        (s, e, sim, "chroma_grid")
        for s, e, sim in _chroma_grid_candidates(mono, sr, region_start, region_end)
    )

    t_wave = _detect_waveform_period(mono, sr, region_start, region_end, hint_period)
    if t_wave is not None:
        for s, e in _waveform_period_candidates(sr, t_wave, region_start, region_end):
            s_r, e_r = _refine_period_candidate(mono, s, e - s, t_wave // 2)
            raw.append((s_r, e_r, 0.0, "waveform"))

    if tempo_bpm is not None:
        for s, e in _tempo_candidates(sr, tempo_bpm, region_start, region_end):
            raw.append((s, e, 0.0, "tempo"))

    # Constrain to modulation period multiples; fall back to unconstrained if it wipes all.
    # Two-sided tolerance: a length just *below* a multiple (e.g. 1.95·t_mod) is as valid
    # as one just above, so test distance to the nearest multiple, not just the remainder.
    if t_mod and raw:
        def _near_multiple(length: int) -> bool:
            r = length % t_mod
            return min(r, t_mod - r) < t_mod * 0.1
        constrained = [(s, e, sc, src) for s, e, sc, src in raw if _near_multiple(e - s)]
        candidates = constrained if constrained else raw
    else:
        candidates = raw

    # Zero-crossing snap radius: at least half the fundamental period so that
    # bass notes (long period, widely-spaced zero crossings) always find a match.
    zc_radius = max(_ZC_SNAP_RADIUS, (t_wave // 2) if t_wave else 0)

    # Minimum loop after snapping: period-aware floor so short pitch-locked loops
    # (e.g. 50 ms at A5) are not discarded by the 1s _MIN_GAP_S floor.
    min_loop = max(int(0.05 * sr), (4 * t_wave) if t_wave else int(_MIN_GAP_S * sr))

    # Pass 1: snap all candidates to same-direction zero crossings, deduplicate.
    seen_snap: set[tuple[int, int]] = set()
    snap_pool: list[tuple[int, int, float, str]] = []
    for s, e, pre_chroma, src in candidates:
        if s < 0 or e >= n - _FRAME:
            continue
        rising = float(mono[min(s + 4, n - 1)]) > float(mono[max(s - 4, 0)])
        s_snap = _snap_to_slope_zero_crossing(mono, s, rising, zc_radius)
        e_snap = _snap_to_slope_zero_crossing(mono, e, rising, zc_radius) + 1
        if s_snap >= e_snap or e_snap - s_snap < min_loop or e_snap >= n:
            continue
        key = (s_snap, e_snap)
        if key not in seen_snap:
            seen_snap.add(key)
            snap_pool.append((s_snap, e_snap, pre_chroma, src))

    # Pass 2: score at the snapped endpoints so the score reflects the actual seam.
    scored: list[tuple[float, int, int, str]] = []
    dbg_cands: list[dict] = []
    for s, e, _sim, src in snap_pool:
        comps = _boundary_components(mono, sr, s, e)
        score = comps["score"]
        if score >= quality_threshold:
            scored.append((score, s, e, src))
        if dbg:
            amp_disc, deriv_disc = _seam_disc(mono, s, e)
            dbg_cands.append({
                "start": s,
                "end": e,
                "len": e - s,
                "len_periods": round((e - s) / t_wave, 2) if t_wave else None,
                "src": src,
                "score": round(score, 4),
                "chroma": round(comps["chroma"], 4),
                "amp_score": round(comps["amp_score"], 4),
                "slope_score": round(comps["slope_score"], 4),
                "amp_disc": round(amp_disc, 4),
                "deriv_disc": round(deriv_disc, 4),
                "passed": score >= quality_threshold,
            })

    # Rank passing candidates by seam quality plus a length reward and a placement penalty,
    # so the first pick is a longer loop on a stable (flat) part of the envelope rather than
    # the shortest seam-clean window. The returned score stays the base quality score.
    rank_env = _rms_envelope(mono, _ENV_HOP)

    def _rank(score: float, s: int, e: int) -> float:
        length_reward = min(((e - s) / sr) / _LOOP_LEN_TARGET_S, 1.0)
        fs = min(s // _ENV_HOP, len(rank_env) - 1)
        fe = min(e // _ENV_HOP, len(rank_env) - 1)
        ls, le = float(rank_env[fs]), float(rank_env[fe])
        level_drop = abs(ls - le) / max(ls, le, 1e-9)
        return score + _W_LENGTH * length_reward - _W_PLACEMENT * level_drop

    scored.sort(key=lambda t: (_rank(t[0], t[1], t[2]), t[0], t[1], t[2]), reverse=True)
    result = [((s, e), sc) for sc, s, e, _ in scored[:max_candidates]]

    if dbg:
        hop = int(_CHROMA_GRID_HOP_S * sr)
        fps = _chroma_fingerprints(mono, sr, region_start, region_end, hop)
        _emit(
            "ok" if result else "none_passed",
            (region_start, region_end),
            {
                "t_wave": t_wave,
                "hint_period": hint_period,
                "n_raw": len(raw),
                "n_snap": len(snap_pool),
                "n_passed": len(scored),
                "best": (
                    {"start": scored[0][1], "end": scored[0][2], "score": round(scored[0][0], 4)}
                    if scored else None
                ),
                "chroma_movement": round(_chroma_movement(fps), 4),
                "centroid_cv": round(_centroid_movement(mono, sr, region_start, region_end), 4),
                "rms_depth": round(_rms_depth_region(mono, region_start, region_end), 4),
                "candidates": dbg_cands,
            },
        )

    return result


def find_loop_points(
    buf: AudioBuffer,
    quality_threshold: float = 0.8,
    tempo_bpm: float | None = None,
    envelope: EnvelopeResult | None = None,
    midi_note: int | None = None,
) -> tuple[tuple[int, int] | None, float]:
    """Return the single best loop point and its quality score.

    Thin wrapper around find_loop_candidates for callers that don't need fallback iteration.
    """
    cands = find_loop_candidates(buf, quality_threshold, tempo_bpm, envelope, midi_note, max_candidates=1)
    if cands:
        return cands[0][0], cands[0][1]
    return None, 0.0


def bake_loop_crossfade(
    buf: AudioBuffer,
    loop_start: int,
    loop_end: int,
    loop_fade_ms: float,
    release_fade_ms: float | None = None,
) -> AudioBuffer:
    """Bake a backward crossfade at loop_end and a release crossfade just after it.

    The Deluge hard-loop seam is loop_end-1 → loop_start on every wrap. For seamless
    continuity we need both value and slope to match at that boundary.

    Loop crossfade — region [LE-N, LE):
      Blend from the existing loop tail (fade out) to a copy of the region just before
      loop_start (fade in). At t=1 the output sample equals audio[LS-1], so the wrap
      LE-1 → LS lands on naturally consecutive samples.

    Release crossfade — region [LE, LE+M):
      On note-off the playhead exits the loop at LE. Blend from a copy of the region
      just after loop_start (fade out) to the existing decay tail (fade in). The first
      released sample sounds like audio[LS], giving a smooth phantom continuation before
      easing into the real tail.

    release_fade_ms defaults to loop_fade_ms / 2 — shorter so the natural decay arrives
    quickly. Both fades use equal-gain (linear) curves, appropriate for blending near-
    copies of the same periodic signal.
    """
    sr = buf.sample_rate
    n = buf.data.shape[1]

    if release_fade_ms is None:
        release_fade_ms = loop_fade_ms / 2.0

    loop_body = loop_end - loop_start
    loop_fade_len = int(sr * loop_fade_ms / 1000.0)
    loop_fade_len = min(loop_fade_len, loop_start, loop_body // 4)

    release_fade_len = int(sr * release_fade_ms / 1000.0)
    release_fade_len = min(release_fade_len, n - loop_end, loop_body // 4)

    if loop_fade_len < 2 and release_fade_len < 2:
        return buf

    out = AudioBuffer(data=buf.data.copy(), sample_rate=sr)

    if loop_fade_len >= 2:
        t = np.linspace(0.0, 1.0, loop_fade_len, dtype=np.float32)
        existing = buf.data[:, loop_end - loop_fade_len : loop_end]
        pre_ls = buf.data[:, loop_start - loop_fade_len : loop_start]
        out.data[:, loop_end - loop_fade_len : loop_end] = existing * (1.0 - t) + pre_ls * t

    if release_fade_len >= 2:
        t = np.linspace(0.0, 1.0, release_fade_len, dtype=np.float32)
        existing_tail = buf.data[:, loop_end : loop_end + release_fade_len]
        post_ls = buf.data[:, loop_start : loop_start + release_fade_len]
        out.data[:, loop_end : loop_end + release_fade_len] = post_ls * (1.0 - t) + existing_tail * t

    return out

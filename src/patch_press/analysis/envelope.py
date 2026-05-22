import dataclasses
import logging
from dataclasses import dataclass

import librosa
import numpy as np

from ..model.audio import AudioBuffer

log = logging.getLogger(__name__)

_HOP = 512
_SUSTAIN_THRESHOLD = 0.4  # RMS fraction below which sustain has ended
_PLUCK_WINDOW_START_S = 1.5  # start of averaging window for pluck check
_PLUCK_WINDOW_END_S = 3.0   # end of averaging window for pluck check
_PLUCK_RMS_THRESHOLD = 0.08  # if average RMS in window falls below this fraction of peak → pluck
_AUTOCORR_MIN_S = 0.35  # minimum modulation period to detect (excludes sub-quarter FM beating)
_AUTOCORR_MAX_S = 8.0   # maximum modulation period to detect
_AUTOCORR_THRESHOLD = 0.35  # minimum normalised autocorrelation peak to accept period
_RMS_MODULATION_DEPTH = 0.40  # (max-min)/mean of sustain RMS required before running autocorr
_BPM_SNAP_TOLERANCE = 0.12  # accept period if within this fraction of a musical subdivision


def _to_db(v: float) -> float:
    return 20 * np.log10(v) if v > 0 else -float("inf")


@dataclass
class EnvelopeResult:
    peak_db: float
    rms_db: float
    attack_s: float
    attack_end: int  # sample index where attack settles
    sustain_start: int  # sample index, steady-state begins
    sustain_end: int  # sample index, release begins (or EOF)
    classification: str  # "pluck" | "sustained" | "sustained_with_modulation"
    modulation_period_samples: int | None  # LFO/tremolo period, or None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def _detect_modulation_period(rms: np.ndarray, sr: int, start_frame: int, end_frame: int) -> int | None:
    """Autocorrelation-based modulation period detection on the RMS envelope slice."""
    segment = rms[start_frame:end_frame]
    if len(segment) < 8:
        return None
    seg = segment - segment.mean()
    n = len(seg)
    fft_seg = np.fft.rfft(seg, n=2 * n)
    autocorr = np.fft.irfft(np.abs(fft_seg) ** 2)[:n]
    autocorr /= autocorr[0] + 1e-12

    min_lag = max(1, int(_AUTOCORR_MIN_S * sr / _HOP))
    max_lag = min(n - 1, int(_AUTOCORR_MAX_S * sr / _HOP))
    if min_lag >= max_lag:
        return None

    search = autocorr[min_lag:max_lag]

    # Require a genuine local maximum — a monotone-decreasing autocorr (no periodicity)
    # has no local maxima and must not be mistaken for a periodic signal.
    if len(search) < 3:
        return None
    is_peak = (search[1:-1] > search[:-2]) & (search[1:-1] > search[2:])
    peak_offsets = np.where(is_peak)[0] + 1  # +1 to account for slice offset
    if len(peak_offsets) == 0:
        return None
    best_offset = peak_offsets[np.argmax(search[peak_offsets])]
    if best_offset * _HOP < 0.022 * sr:  # noise bump within 22ms of min_lag, not a real period
        return None
    if search[best_offset] < _AUTOCORR_THRESHOLD:
        return None

    peak_frame = min_lag + best_offset
    return peak_frame * _HOP


def _snap_to_bpm_subdivision(period_s: float, bpm: float) -> float | None:
    """Return the nearest musical subdivision period in seconds, or None if no close match."""
    quarter_s = 60.0 / bpm
    subdivisions = [
        quarter_s * 4,  # whole note
        quarter_s * 2,  # half note
        quarter_s,  # quarter note
        quarter_s * 1.5,  # dotted quarter
        quarter_s * 0.75,  # dotted eighth
        quarter_s / 2,  # eighth note
        quarter_s / 3,  # triplet quarter
        quarter_s / 4,  # sixteenth note
        quarter_s / 6,  # triplet eighth
    ]
    best_sub = min(subdivisions, key=lambda s: abs(period_s - s))
    if abs(period_s - best_sub) / best_sub <= _BPM_SNAP_TOLERANCE:
        return best_sub
    return None


def analyze_envelope(buf: AudioBuffer, bpm: float | None = None) -> EnvelopeResult:
    mono = buf.data.mean(axis=0).astype(np.float32)
    sr = buf.sample_rate
    n = len(mono)

    if n == 0:
        return EnvelopeResult(
            peak_db=-float("inf"),
            rms_db=-float("inf"),
            attack_s=0.0,
            attack_end=0,
            sustain_start=0,
            sustain_end=0,
            classification="pluck",
            modulation_period_samples=None,
        )

    peak = float(np.abs(mono).max())
    rms_val = float(np.sqrt(np.mean(mono**2)))
    peak_db = round(_to_db(peak), 2)
    rms_db = round(_to_db(rms_val), 2)

    rms = librosa.feature.rms(y=mono, hop_length=_HOP)[0]

    if len(rms) < 4 or rms.max() < 1e-6:
        # Silent or too short — treat as pluck, use quarter-points
        q = n // 4
        return EnvelopeResult(
            peak_db=peak_db,
            rms_db=rms_db,
            attack_s=0.0,
            attack_end=q,
            sustain_start=q,
            sustain_end=3 * q,
            classification="pluck",
            modulation_period_samples=None,
        )

    peak_frame = int(np.argmax(rms))
    peak_rms = float(rms[peak_frame])
    attack_end = sustain_start = peak_frame * _HOP
    attack_s = round(attack_end / sr, 4)

    # Walk forward from peak to find where RMS drops below sustain threshold
    threshold = _SUSTAIN_THRESHOLD * peak_rms
    after_peak = rms[peak_frame:]
    drop_frames = np.where(after_peak < threshold)[0]
    if len(drop_frames) > 0:
        sustain_end = min((peak_frame + int(drop_frames[0])) * _HOP, n)
    else:
        sustain_end = n

    # Fallback: too short a sustain region — use quarter-points
    min_region = int(1.0 * sr)
    if sustain_end - sustain_start < min_region:
        q = n // 4
        attack_end = sustain_start = q
        sustain_end = 3 * q

    # Classify: pluck vs sustained — average RMS over a window to smooth out attack transients
    start_frame = min(peak_frame + int(_PLUCK_WINDOW_START_S * sr / _HOP), len(rms) - 1)
    end_frame = min(peak_frame + int(_PLUCK_WINDOW_END_S * sr / _HOP), len(rms))
    avg_rms = rms[start_frame:end_frame].mean() if end_frame > start_frame else rms[start_frame]
    if avg_rms < _PLUCK_RMS_THRESHOLD * peak_rms:
        return EnvelopeResult(
            peak_db=peak_db,
            rms_db=rms_db,
            attack_s=attack_s,
            attack_end=attack_end,
            sustain_start=sustain_start,
            sustain_end=sustain_end,
            classification="pluck",
            modulation_period_samples=None,
        )

    # Sustained — check for modulation, but only if RMS varies enough to suggest a real LFO/delay
    sustain_start_frame = sustain_start // _HOP
    sustain_end_frame = min(sustain_end // _HOP, len(rms))
    sustain_rms = rms[sustain_start_frame:sustain_end_frame]
    rms_mean = sustain_rms.mean() if len(sustain_rms) > 0 else 0.0
    rms_depth = (sustain_rms.max() - sustain_rms.min()) / rms_mean if rms_mean > 1e-6 else 0.0
    log.debug(f"  rms_depth={rms_depth:.3f} (gate={_RMS_MODULATION_DEPTH})")
    if rms_depth >= _RMS_MODULATION_DEPTH:
        # Use full audio from sustain_start to end — longer window gives more pattern repetitions,
        # which is critical for slow gating patterns (whole-note arpeggiator etc.)
        period_samples = _detect_modulation_period(rms, sr, sustain_start_frame, len(rms))
        log.debug(f"  autocorr period={period_samples and round(period_samples / sr, 3)}s")
    else:
        period_samples = None

    if period_samples is not None and bpm is not None:
        period_s = period_samples / sr
        snapped_s = _snap_to_bpm_subdivision(period_s, bpm)
        if snapped_s is not None:
            period_samples = int(round(snapped_s * sr))
        else:
            log.debug(f"  period {period_s:.3f}s rejected — no BPM subdivision match at {bpm} BPM")
            period_samples = None

    if period_samples is not None:
        classification = "sustained_with_modulation"
    else:
        classification = "sustained"

    return EnvelopeResult(
        peak_db=peak_db,
        rms_db=rms_db,
        attack_s=attack_s,
        attack_end=attack_end,
        sustain_start=sustain_start,
        sustain_end=sustain_end,
        classification=classification,
        modulation_period_samples=period_samples,
    )

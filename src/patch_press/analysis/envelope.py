import dataclasses
import logging
from dataclasses import dataclass

import librosa
import numpy as np

from ..model.audio import AudioBuffer

log = logging.getLogger(__name__)

_HOP = 512
_SUSTAIN_THRESHOLD = 0.4  # RMS fraction below which sustain has ended
_PLATEAU_LEVEL = 0.9  # RMS fraction of peak that marks the end of attack / start of the plateau
_MIN_BACKWARD_PLATEAU_S = 2.0  # min flat near-peak run before the RMS max to reclaim as sustain_start
_PLUCK_WINDOW_START_S = 1.5  # start of averaging window for pluck check
_PLUCK_WINDOW_END_S = 3.0   # end of averaging window for pluck check
_PLUCK_RMS_THRESHOLD = 0.08  # if average RMS in window falls below this fraction of peak → pluck
_AUTOCORR_MIN_S = 0.35  # minimum modulation period to detect (excludes sub-quarter FM beating)
_AUTOCORR_MAX_S = 8.0   # maximum modulation period to detect
_AUTOCORR_THRESHOLD = 0.35  # minimum normalised autocorrelation peak to accept period
_RMS_MODULATION_DEPTH = 0.40  # (max-min)/mean of sustain RMS required before running autocorr
_BPM_SNAP_TOLERANCE = 0.12  # accept period if within this fraction of a musical subdivision
# Plateau extension (Fix #2): a sound that decays to a steady plateau *below* the 0.4 sustain
# line still holds a loopable region until note-off. These gate an extend-only widening of
# sustain_end so that plateau is not excluded.
_PLATEAU_FLOOR = 0.15      # RMS fraction of peak still counted as "held" (vs released to silence)
_PLATEAU_FLATNESS = 0.25   # max coefficient of variation of the settled tail to count as flat
_MIN_PLATEAU_S = 0.5       # minimum tail length (s) before a plateau extension is considered


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
    threshold = _SUSTAIN_THRESHOLD * peak_rms

    # sustain_start: normally the peak frame, but extended back over a genuine flat plateau when
    # the RMS max drifted late. On a flat-topped held note the global max can land anywhere along
    # the plateau (often a swell just before note-off); anchoring sustain_start there discards the
    # whole held plateau and collapses the loopable region to a sliver straddling the release,
    # forcing the loop finder to pick a loop_end in the decay. Walk back from the peak while RMS
    # stays within _PLATEAU_LEVEL of it, and only adopt that earlier start when the run is at least
    # _MIN_BACKWARD_PLATEAU_S long. The length guard keeps ordinary attacks, onset transients and
    # short decays (only a brief near-peak run before the max) anchored at the peak as before, so
    # the fix fires solely for the long-flat-plateau pathology it targets.
    before_peak = rms[: peak_frame + 1]
    below_before = np.where(before_peak < _PLATEAU_LEVEL * peak_rms)[0]
    plateau_start = int(below_before[-1]) + 1 if len(below_before) > 0 else 0
    if (peak_frame - plateau_start) * _HOP >= _MIN_BACKWARD_PLATEAU_S * sr:
        sustain_start = plateau_start * _HOP
    else:
        sustain_start = peak_frame * _HOP
    attack_end = sustain_start
    attack_s = round(attack_end / sr, 4)

    # Walk forward from peak to find where RMS drops below sustain threshold
    after_peak = rms[peak_frame:]
    drop_frames = np.where(after_peak < threshold)[0]
    if len(drop_frames) > 0:
        sustain_end = min((peak_frame + int(drop_frames[0])) * _HOP, n)
    else:
        sustain_end = n

    # Extend-only plateau check: the 0.4·peak threshold ends the region at the *first* dip
    # below 40% of peak, so a sound that decays to a steady plateau below that line (held
    # until note-off) loses its loopable plateau. If the tail past the drop SETTLES (flat and
    # well above silence) rather than decaying out, extend sustain_end forward over it. This
    # can only move sustain_end later, so a control that decays straight to silence (near-
    # silent tail) never triggers it.
    if sustain_end < n:
        end_frame = sustain_end // _HOP
        tail = rms[end_frame:]
        min_plateau_frames = max(int(_MIN_PLATEAU_S * sr / _HOP), 2)
        if len(tail) >= min_plateau_frames:
            settle = tail[len(tail) // 2:]
            settle_mean = float(settle.mean())
            settle_cv = float(settle.std() / settle_mean) if settle_mean > 1e-6 else float("inf")
            if settle_mean >= _PLATEAU_FLOOR * peak_rms and settle_cv <= _PLATEAU_FLATNESS:
                above = np.where(tail >= _PLATEAU_FLOOR * peak_rms)[0]
                if len(above) > 0:
                    plateau_end = min((end_frame + int(above[-1]) + 1) * _HOP, n)
                    sustain_end = max(sustain_end, plateau_end)

    # Fallback: too short a sustain region — use quarter-points
    min_region = int(1.0 * sr)
    if sustain_end - sustain_start < min_region:
        q = n // 4
        attack_end = sustain_start = q
        sustain_end = 3 * q

    # Classify: pluck vs sustained — average RMS over a window to smooth out attack
    # transients. The window is anchored to the sustained region, not a fixed offset past
    # the raw peak: on short samples whose loudest RMS frame lands late (amplitude wobble
    # on a hardware recording), peak+3s overruns the note's release into silence and a
    # clearly-sustained note reads as a pluck. Clamp to [sustain_start, sustain_end] and,
    # if the peak sits so late that the window collapses, measure the tail of the sustain.
    sustain_start_frame = sustain_start // _HOP
    sustain_end_frame = min(sustain_end // _HOP, len(rms))
    win_lo = int(_PLUCK_WINDOW_START_S * sr / _HOP)
    win_hi = int(_PLUCK_WINDOW_END_S * sr / _HOP)
    start_frame = max(sustain_start_frame, min(peak_frame + win_lo, sustain_end_frame - 1))
    end_frame = min(peak_frame + win_hi, sustain_end_frame)
    if end_frame <= start_frame:
        end_frame = sustain_end_frame
        start_frame = max(sustain_start_frame, end_frame - win_lo)
    avg_rms = rms[start_frame:end_frame].mean() if end_frame > start_frame else rms[max(0, end_frame - 1)]
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
    # (sustain_start_frame / sustain_end_frame computed above for the pluck window).
    sustain_rms = rms[sustain_start_frame:sustain_end_frame]
    rms_mean = sustain_rms.mean() if len(sustain_rms) > 0 else 0.0
    rms_depth = (sustain_rms.max() - sustain_rms.min()) / rms_mean if rms_mean > 1e-6 else 0.0
    log.debug(f"  rms_depth={rms_depth:.3f} (gate={_RMS_MODULATION_DEPTH})")
    if rms_depth >= _RMS_MODULATION_DEPTH:
        # Use full audio from sustain_start to end — longer window gives more pattern repetitions,
        # which is critical for slow gating patterns (whole-note arpeggiator etc.)
        period_samples = _detect_modulation_period(rms, sr, sustain_start_frame, len(rms))
        log.debug(f"  autocorr period={f'{round(period_samples / sr, 3)}s' if period_samples is not None else 'None'}")
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

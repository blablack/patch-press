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
from .pitch import midi_to_hz

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

# Adaptive crossfade duration (see adaptive_crossfade_ms). A fixed millisecond fade is
# wrong across the register: shorter than one fundamental cycle on bass (can't mask a
# phase step → clicks), and many cycles on treble (needlessly smears movement). So the
# fade is measured in fundamental *periods*, scaled by how much residual discontinuity is
# actually left at the seam (_seam_disc), then bounded. Range deliberately narrow — a big
# fade is not the fix for a bad seam (that's loop-point selection / the non-loopable
# gate); it only covers the leftover mismatch. Tune by ear like the other loop constants.
_XFADE_BASE_PERIODS = 1.5    # fundamental cycles when the seam is already clean (d≈0)
_XFADE_DISC_PERIODS = 2.5    # extra cycles added as d rises 0→1 (→ 4.0 cycles at threshold)
_XFADE_FLOOR_MS = 3.0        # never shorter than this (guards treble, where a cycle is <1 ms)
_XFADE_CAP_MS = 60.0         # never longer than this (guards bass smear; bake also caps to loop_body/4)
_XFADE_FALLBACK_MIN_DISC = 0.5  # central-region fallback loops aren't phase-validated → floor d here

# Quarter-note multipliers covering common tempo-synced delay/LFO rates
_TEMPO_SUBDIVISIONS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 8.0, 16.0]

# Candidate ranking. These do NOT change which candidates pass the quality gate (that
# stays the base seam score in _boundary_components) — they only reorder the passing
# candidates so the caller's first pick favours a longer loop sitting on a stable part of
# the envelope, instead of the shortest seam-clean window. Length is measured in time, not
# period count: a high note can span hundreds of cycles in a few milliseconds.
_LOOP_LEN_TARGET_S = 1.5   # loops at/above this length earn the full length reward
_W_LENGTH = 0.15           # weight of the length reward
# Ranking length floor. A short pitch-locked loop trivially matches its own seam (near-perfect
# base score), which the additive length reward (max +_W_LENGTH) cannot overcome, so a 0.1s
# loop can outrank a 6.5s one on a multi-second sustain. Gate out sub-floor loops when any
# longer loop survives the other gates; never wipe — a genuinely short sustain keeps its short
# loops and the length reward still picks the longest available.
_MIN_LOOP_RANK_S = 1.0
# Whole-loop drift control (replaces the old RMS-only placement term). The seam can match
# perfectly while the tone still drifts ACROSS the loop, so each wrap jumps — a per-loop
# wobble. amp_drift compares the positive AND negative peak of the post-wrap window to the
# pre-wrap window (RMS is blind to a constant-RMS asymmetry drift; peaks are not).
#
# For STEADY sounds amp_drift is a GATE, not a weak additive penalty: drift magnitudes are small
# (a few percent), so at any sane additive weight the length reward swamps them. Like _SEAM_GATE
# it filters the pool so the length reward then picks the longest *stationary* loop; self-targeting
# (a static sound drifts ~0 at every length, so all survive and a long loop still wins) and never
# wipes.
_DRIFT_GATE = 0.025        # max peak/asymmetry drift across the wrap before a loop is gated out
# The drift gate only engages for sounds that are SUPPOSED to be steady. If the peak envelope
# swings more than this across the sustain (a swell/decay/reverse lead), the sound genuinely
# evolves: no sub-loop is representative, and gating to the flattest one collapses it to a tiny
# loop that sounds worse than a long one. Above this, stand the gate down and keep prior length
# behaviour — bounds this change's blast radius to the steady-tone wobble it targets.
_DRIFT_GATE_MAX_MOVE = 0.25
# timbre is the same end→start chroma already computed for the seam, reused for free; a light
# additive penalty (the endpoint form keeps a legitimate whole-cycle pad loop unpenalised).
_W_TIMBRE_DRIFT = 0.10     # weight of the timbre (chroma) drift penalty in ranking
# For EVOLVING sounds the steady drift gate stands down (no sub-loop is flat), so it cannot help.
# But the same amp_drift still tells phase-aligned from mis-aligned long loops: a loop whose
# length is ~k*(modulation period) is envelope-continuous at the wrap (low amp_drift), a
# mis-aligned one pumps once per loop. Here an ADDITIVE penalty IS viable — the length reward
# saturates at _LOOP_LEN_TARGET_S, so above that all candidates tie on length and there is nothing
# left to swamp the penalty; it only arbitrates AMONG long loops, picking the period-aligned one
# without collapsing to a short flat loop. Applied only when not steady (steady keeps the gate).
_W_AMP_DRIFT = 0.20        # weight of the seam amp-drift penalty in the evolving branch
_AMP_DRIFT_NORM = 0.15     # amp_drift (fraction of peak) that saturates the penalty
_SEAM_GATE = 0.20          # max waveform mismatch (normalised RMS) between the loop start and
                           # end windows. Self-targeting: static sounds match at any length so
                           # every candidate passes and the length reward still picks a long loop;
                           # only loops that wrap across a real phase/timbre change (the cause of
                           # the mid-crossfade dip) are gated out, forcing a shorter clean loop.
                           # Never wipes the pool — if all exceed it, the cleanest seam is kept.

# Timbre-travel gate. The chroma seam score (_chroma_score) is pitch-class — BLIND to FM
# brightness / spectral-envelope motion. So a loop can have a waveform-clean, chroma-perfect seam
# while the timbre still travels ACROSS the loop body and audibly snaps back on every wrap
# (ELECTRON 1 / C5: chroma wander ~0, but MFCC start→end jump the worst in the bank). MFCC sees
# that motion. This gate drops candidates whose end-vs-start timbre differs by more than the
# threshold, so the length reward then picks the longest *timbrally-stationary* loop. Self-
# targeting like _SEAM_GATE: a genuinely static tone matches at any length (every candidate passes,
# a long loop still wins); only the traveling ones are filtered. Never wipes — if every candidate
# travels (a globally-evolving sound, no stationary window), keep them all and let length decide.
_MFCC_WIN = 8192           # ~186 ms at 44.1k — long enough for a stable MFCC timbre estimate
_MFCC_SEAM_GATE = 0.06     # max end→start MFCC cosine distance before a loop is judged to travel


# Direct period-aligned loop. The chroma/tempo candidate sources propose beat-synced lengths
# that wrap mid-modulation (audible pumping even with a clean seam crossfade). The fix is to
# lock the loop length to the signal's TRUE repeat period, found by minimising the raw seam
# mismatch (waveform continuity with NO crossfade) over candidate lengths. Verified by ear on
# Dexed FM pads (BANKS, T.): period-aligned loops score raw mismatch ~0.04–0.10 vs ~0.33–0.41
# for the beat-synced ones, and need no crossfade.
_PERIOD_ALIGN_MAX_MISMATCH = 0.15   # raw seam mismatch below which an aligned loop is "clean"
_PERIOD_TARGET_MAX_S = 6.0          # prefer the longest clean period-multiple up to this length
_PERIOD_SEARCH_MIN_S = 0.30         # shortest loop period to consider
_PERIOD_SEARCH_MAX_S = 3.00         # longest base period to consider (multiples extend it)


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


def _snap_to_flat(mono: np.ndarray, frame: int, radius: int, win: int) -> int:
    """Return the flattest (lowest local range) point near frame.

    A loop boundary placed on a *flat* part of the cycle has near-identical neighbouring
    samples, so the wrap reproduces a natural low-amplitude step regardless of waveform.
    Zero crossings are the opposite for steep waves: a square crosses zero on its *edge*,
    the worst place to splice, which makes a clean phase-locked loop read as a click to the
    amplitude/derivative checks. Snapping the start here keeps the seam off the edge.
    """
    win = max(4, win)
    lo = max(0, frame - radius)
    hi = min(len(mono) - win, frame + radius)
    if hi <= lo:
        return frame
    seg = mono[lo:hi + win]
    if len(seg) < win + 1:
        return frame
    sw = np.lib.stride_tricks.sliding_window_view(seg, win)
    rng = sw.max(axis=1) - sw.min(axis=1)
    return lo + int(np.argmin(rng)) + win // 2


def _seam_match(mono: np.ndarray, start: int, end: int, win: int) -> float:
    """Normalised RMS difference between the windows the loop crossfade blends.

    The loop crossfade fades mono[end-win:end] into mono[start-win:start], so those are the
    windows that must match for the equal-gain blend not to cancel (the mid-crossfade dip).
    0 = identical (phase-locked, no timbre change across the wrap); higher = the loop wraps
    across a phase or timbre change. The time-domain sensor the chroma/amp/slope score misses.
    """
    win = min(win, (end - start) // 2, start, end)
    if win < 8:
        return 0.0
    a = mono[start - win:start]
    b = mono[end - win:end]
    denom = float(np.sqrt(np.mean(b ** 2))) + 1e-9
    return float(np.sqrt(np.mean((a - b) ** 2)) / denom)


def _amp_drift(mono: np.ndarray, start: int, end: int, win: int, peak: float) -> float:
    """Peak/asymmetry drift across the loop wrap, as a fraction of peak amplitude.

    The wrap plays mono[end-1] then mono[start], so the window just before end (pre) and the
    window just after start (post) are what abut at the seam. A loop whose seam is phase-clean
    can still have these two windows sit at different points of a slow tremolo/beating, so the
    amplitude jumps once per loop. Track the positive and negative peak SEPARATELY: a constant-
    RMS asymmetry drift (positive peak sweeps while the negative stays flat) moves only one of
    them, and is invisible to an RMS or max-abs measure. 0 = the loop sits on a flat span (or
    spans a whole modulation cycle); higher = it wraps across a drift.
    """
    win = min(win, (end - start) // 2, start, len(mono) - end + win)
    if win < 8 or start + win > len(mono) or end - win < 0:
        return 0.0
    post = mono[start:start + win]
    pre = mono[end - win:end]
    d_hi = abs(float(post.max()) - float(pre.max()))
    d_lo = abs(float(post.min()) - float(pre.min()))
    return (d_hi + d_lo) / (2.0 * (peak or 1.0))


def _peak_env_movement(mono: np.ndarray, start: int, end: int, chunks: int = 12) -> float:
    """How much the absolute-peak envelope swings across [start, end), as a fraction of its max.

    Splits the region into `chunks` blocks and takes each block's peak. ~0 means the sound holds
    a steady level (a flat tone, possibly with a subtle beat — the drift gate's target); high
    means it swells/decays/evolves dramatically, where no sub-loop is representative and forcing
    a flat one loses the character, so the drift gate should stand down and let length decide.
    """
    seg = np.abs(mono[start:end])
    if len(seg) < chunks * 4:
        return 0.0
    step = len(seg) // chunks
    peaks = [float(seg[i * step:(i + 1) * step].max()) for i in range(chunks)]
    mx = max(peaks)
    return (mx - min(peaks)) / mx if mx > 1e-9 else 0.0


def _phase_lock_end(mono: np.ndarray, start: int, end_approx: int, period: int, win: int) -> int:
    """Return the loop end whose pre-wrap window matches the loop start's.

    Finds e near end_approx minimising SSD(mono[e-win:e], mono[start-win:start]) — i.e. the
    two windows the loop crossfade actually blends (mono[end-win:end] faded into
    mono[start-win:start]). Aligning *these* keeps the equal-gain blend from cancelling (the
    mid-crossfade dip) and the wrap in phase, on rich/steep waveforms (e.g. a square).

    Independent zero-crossing snapping cannot guarantee this: a harmonically rich wave has
    several same-slope zero crossings per period, so the two endpoints can snap to different
    sub-phases, knocking the loop off integer periods. Only used when a fundamental period
    is known; falls back to the caller's zero-crossing snap otherwise.
    """
    win = min(win, (end_approx - start) // 2)
    if win < 8 or start - win < 0:
        return end_approx
    lo = max(start + period, win, end_approx - period)
    hi = min(len(mono), end_approx + period)
    if lo >= hi:
        return end_approx
    cand = np.arange(lo, hi)
    w = np.arange(-win, 0)            # window ENDING at each candidate e: mono[e-win:e]
    ref = mono[start - win:start]     # the pre-loop-start window the crossfade fades in
    seg = mono[cand[:, None] + w[None, :]]
    ssd = np.sum((seg - ref[None, :]) ** 2, axis=1)
    return int(cand[int(np.argmin(ssd))])


def _exact_sub_period(
    mono: np.ndarray,
    sr: int,
    region_start: int,
    region_end: int,
    midi_note: int | None,
    hint_period: int | None,
) -> float | None:
    """Exact sub-octave repeat period (float samples) to quantize the loop length to, or None.

    Returns a value only when a sub-octave is detected (_detect_sub_multiple) AND the pitch is known.
    The sub period is then that whole-number octave multiple of the EXACT fundamental derived from the
    MIDI note. Using the pitch-derived period rather than the autocorr peak matters: the sub sits at an
    exact musical octave below the root, and a loop spanning hundreds of cycles quantized to the exact
    float stays phase-locked, where a sub-sample period error would drift the wrap back off-phase.
    """
    if midi_note is None or not hint_period:
        return None
    sub_mult = _detect_sub_multiple(mono, sr, region_start, region_end, hint_period)
    if sub_mult is None:
        return None
    f0_period = sr / midi_to_hz(midi_note)
    return sub_mult * f0_period


def _snap_end_to_sub(mono: np.ndarray, start: int, end: int, sub_period: float) -> int:
    """Move `end` to a sub-aligned length whose seam is also amplitude-continuous, or leave it.

    Every start + round(k·sub_period) shares start's sub AND fundamental phase, so any k gives a
    phase-clean wrap — but the AMPLITUDE envelope drifts across a long sustain, so the k nearest the
    requested length can land where the level does not match (an audible step). Search a few k either
    side and take the one with the smallest value discontinuity at the wrap.

    Crucially, only ADOPT it when that discontinuity is below the click threshold: for some start
    positions no nearby sub-multiple is level-continuous, and forcing one would trade the (subtle)
    sub-octave reset for a (worse) click. In that case return `end` unchanged — the loop stays at its
    original fundamental-aligned placement, no better but no worse than before this fix engaged.
    """
    n = len(mono)
    peak = float(np.abs(mono).max()) or 1.0
    k0 = max(1, round((end - start) / sub_period))
    best_e, best_disc = None, None
    for k in range(max(1, k0 - 4), k0 + 5):
        e = start + int(round(k * sub_period))
        if e <= start or e >= n:
            continue
        disc = abs(float(mono[e - 1]) - float(mono[start]))
        if best_disc is None or disc < best_disc:
            best_e, best_disc = e, disc
    if best_e is not None and best_disc / peak < _AMP_DISC_THRESHOLD:
        return best_e
    return end


# Sub-octave (undertone) detection. Many analog-style patches stack an oscillator one or two
# octaves BELOW the root (Mini From Mars: Grand Square, Hairy Dog, Haus Baby). The signal then only
# truly repeats every 2× (or 4×) the fundamental period, so a loop length snapped to the FUNDAMENTAL
# can be an ODD multiple of the sub period and wrap mid-sub-cycle — audible as a "slower cycle" that
# resets on every loop (user's ear, confirmed by loop-length-vs-sub-period alignment: OK notes land
# on integer sub-multiples, flagged notes on half-integers). The autocorr signature is unambiguous:
# a plain tone correlates equally at every multiple of its period, but a sub-octave correlates far
# WORSE at the fundamental lag than at 2×/4× it (Grand Square C4: ac@T≈0.77 vs ac@2T≈0.999). So the
# true repeat period is the SMALLEST k·t0 (k∈1..4) whose autocorr is within a whisker of the best —
# k=1 for a plain tone, 2 or 4 for a sub-octave. This drives ONLY the final loop-length quantization
# (_exact_sub_period → _detect_sub_multiple): the general period estimate (_detect_waveform_period)
# stays on the fundamental, so candidate generation/ranking are byte-identical and a plain tone (k=1,
# no sub) is entirely untouched — only a real sub-octave note has its loop length nudged onto a whole
# sub-cycle.
_SUBHARM_MAX_K = 4      # search undertone periods up to 4× the fundamental
_SUBHARM_FRAC = 0.95    # k·t0 counts as the true period if its autocorr ≥ this fraction of the best
_SUBHARM_MIN_AC = 0.5   # skip the check when even the best multiple is this weak (aperiodic/noisy)


def _midi_to_period(sr: int, midi_note: int) -> int:
    return max(1, int(round(sr / midi_to_hz(midi_note))))


def _true_repeat_period(autocorr: np.ndarray, t0: int) -> float:
    """Return the true repeat period (sub-sample float) given the fundamental period t0.

    Scans the autocorrelation at the fundamental lag and its 2×/3×/4× multiples (with a local
    interpolated peak search at each, to absorb a slightly detuned sub-oscillator). Returns the
    SMALLEST multiple whose correlation is within _SUBHARM_FRAC of the strongest — the fundamental
    for a plain tone (all multiples correlate equally), a sub-octave when the fundamental lag
    correlates markedly worse than 2×/4× it. See the block comment above _midi_to_period.
    """
    n = len(autocorr)

    def peak_near(center: int) -> tuple[float, float] | None:
        w = max(2, int(0.15 * center))
        a, b = max(1, center - w), min(n - 2, center + w)
        if b <= a:
            return None
        i = a + int(np.argmax(autocorr[a:b]))
        ya, yb, yc = autocorr[i - 1], autocorr[i], autocorr[i + 1]
        d = ya - 2.0 * yb + yc
        frac = 0.5 * (ya - yc) / d if d != 0 else 0.0
        return i + float(frac), float(yb)

    cands: list[tuple[float, float]] = []
    for k in range(1, _SUBHARM_MAX_K + 1):
        center = int(round(k * t0))
        if center >= n - 2:
            break
        pk = peak_near(center)
        if pk is not None:
            cands.append(pk)
    if not cands:
        return float(t0)
    vmax = max(v for _, v in cands)
    if vmax < _SUBHARM_MIN_AC:
        return float(t0)
    for period, v in cands:
        if v >= _SUBHARM_FRAC * vmax:
            return max(1.0, period)
    return float(t0)


def _detect_sub_multiple(
    mono: np.ndarray,
    sr: int,
    region_start: int,
    region_end: int,
    hint_period: int,
) -> int | None:
    """Return the sub-octave multiple (2, 3 or 4) when the patch stacks an oscillator below the root.

    Autocorrelates a sustain chunk and compares the fundamental lag with its 2×/3×/4× multiples. A
    plain tone correlates equally at every multiple → None (no sub). A sub-octave correlates markedly
    WORSE at the fundamental lag than at the multiple where it truly repeats (Grand Square C4:
    ac@T≈0.77 vs ac@2T≈0.99, Combo Organ: ac@T≈−0.8) → that multiple. Returns None below
    _SUBHARM_MIN_AC (aperiodic). This ONLY drives the loop-length quantization (see _exact_sub_period);
    it deliberately does not feed the general period estimate, so nothing else in the pipeline moves.
    """
    chunk_len = min(region_end - region_start, max(int(0.5 * sr), 12 * hint_period))
    mid = (region_start + region_end) // 2
    chunk = mono[mid - chunk_len // 2 : mid + chunk_len // 2]
    if len(chunk) < 64:
        return None
    chunk = chunk - chunk.mean()
    n = len(chunk)
    fft_c = np.fft.rfft(chunk, n=2 * n)
    autocorr = np.fft.irfft(np.abs(fft_c) ** 2)[:n]
    autocorr /= autocorr[0] + 1e-12

    period = _true_repeat_period(autocorr, hint_period)
    mult = int(round(period / hint_period))
    if mult < 2:
        return None
    if float(autocorr[min(int(round(period)), n - 1)]) < 0.3:
        return None
    return mult


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


def _mfcc_seam_distance(mono: np.ndarray, sr: int, start: int, end: int) -> float:
    """Cosine distance of mean MFCC (timbre) just before loop_end vs just after loop_start.

    The counterpart to _chroma_score for the dimension chroma can't see. Chroma is pitch-class, so
    it reports a perfect seam even when the FM brightness / spectral envelope has travelled across
    the loop body — which is exactly what makes such a loop audibly reset on every wrap. MFCC
    captures that. 0 = same timbre across the wrap; higher = the body travelled in timbre. The 0th
    coefficient (overall energy) is dropped so this measures timbre SHAPE, not level (level is
    already handled by amp_score / amp_drift). end is exclusive (Deluge convention).
    """
    win = min(_MFCC_WIN, end - start)
    if win < 512:
        return 0.0
    pre = mono[max(0, end - win):end]
    post = mono[start:min(len(mono), start + win)]
    if len(pre) < 512 or len(post) < 512:
        return 0.0
    ma = librosa.feature.mfcc(y=pre, sr=sr, n_mfcc=20).mean(axis=1)[1:]
    mb = librosa.feature.mfcc(y=post, sr=sr, n_mfcc=20).mean(axis=1)[1:]
    denom = float(np.linalg.norm(ma) * np.linalg.norm(mb))
    return float(1.0 - np.dot(ma, mb) / denom) if denom > 0 else 0.0


def _seam_slopes(mono: np.ndarray, start: int, end: int, w: int) -> tuple[float, float]:
    """Forward slopes over `w` samples just after loop_start and just after loop_end.

    Both are FORWARD (same direction) and one loop-continuation apart: mono[end:end+w] is the
    natural next period after the loop body, which equals mono[start:start+w] for a clean
    wrap, so a phase-aligned loop yields matching slopes (~0 difference). Comparing the
    forward slope at start to the *backward* slope arriving at end instead measures the
    waveform's own curvature at the splice — and _snap_to_flat deliberately puts the loop
    start on an apex (a flat extremum), where the slope reverses sign. That reads as a large
    false discontinuity, and it scales with pitch (a fixed window spans more of a shorter
    period), so high notes get wrongly rejected and forced onto the unaligned central-region
    fallback — whose crossfade then phase-cancels.
    """
    n = len(mono)
    s_start = float(mono[min(start + w, n - 1)]) - float(mono[start])
    s_end = float(mono[min(end + w, n - 1)]) - float(mono[min(end, n - 1)])
    return s_start, s_end


def _boundary_components(
    mono: np.ndarray,
    sr: int,
    start: int,
    end: int,
) -> dict:
    """Chroma/amp/slope components and the weighted score for the seam mono[end-1] → mono[start].

    end is exclusive (loop plays [start, end)). Chroma is computed at the (post-snap)
    endpoints so it reflects the real end→start seam, not the grid fingerprint.

    NOTE: slope_score here is a ranking heuristic, deliberately distinct from the same-direction
    correctness gate in validate_splice_reason. It compares the forward slope after loop_start
    to the backward slope arriving at loop_end and normalises by the local slope magnitude, so
    short loops spliced on a waveform apex score low. That down-weights them relative to longer
    loops, working WITH the length reward in find_loop_candidates — switching it to the
    validation deriv inflates short apex loops and lets them out-rank longer ones.
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


def _seam_disc(mono: np.ndarray, start: int, end: int) -> tuple[float, float]:
    """Raw amp/deriv discontinuity at the seam mono[end-1] -> mono[start], on RAW pre-crossfade audio.

    The loop crossfade manufactures a smooth seam by construction, so checking the baked audio
    passes anything — this must run on the raw signal. amp_disc is the residual value step at
    the wrap; deriv_disc uses _seam_slopes (same-direction) so a phase-aligned loop spliced on a
    waveform apex is not mistaken for a click. end is exclusive (Deluge convention): the loop
    plays [start, end), so the last sample before jumping back is mono[end-1].
    """
    peak = float(np.abs(mono).max()) or 1.0
    amp_disc = abs(float(mono[end - 1]) - float(mono[start])) / peak
    slope_start, slope_end = _seam_slopes(mono, start, end, _SLOPE_WINDOW)
    deriv_disc = abs(slope_end - slope_start) / (2.0 * peak)
    return amp_disc, deriv_disc


def validate_splice_reason(mono: np.ndarray, start: int, end: int) -> str:
    """Return empty string if the splice is clean, else a description of the failing check.

    end is treated as exclusive: the loop plays [start, end), so the last sample
    before jumping back is mono[end-1]. After zero-crossing snap with the +1 shift
    applied in find_loop_points, mono[end-1] is at the crossing itself (small value).
    """
    if start < 1 or end < 2 or end >= len(mono):
        return "out of bounds"
    amp_disc, deriv_disc = _seam_disc(mono, start, end)
    if amp_disc >= _AMP_DISC_THRESHOLD:
        return f"amp_disc={amp_disc:.3f} (threshold {_AMP_DISC_THRESHOLD})"
    if deriv_disc >= _DERIV_DISC_THRESHOLD:
        return f"deriv_disc={deriv_disc:.3f} (threshold {_DERIV_DISC_THRESHOLD})"
    return ""


def adaptive_crossfade_ms(
    audio: AudioBuffer, note: int, loop_points: tuple[int, int], min_disc: float = 0.0
) -> float:
    """Crossfade length (ms) for a *detected* loop, from the note's period and its raw seam.

    Two signals (see the _XFADE_* constants):
      * The fundamental period (from `note`) sets the unit — a crossfade must span at least
        ~one cycle to mask a phase step, and a cycle is 25 ms on a low bass note but <1 ms on
        a treble one, so a fixed millisecond value is wrong at one end or the other.
      * The residual seam discontinuity `_seam_disc` (peak-normalized amp/derivative step on
        the RAW pre-crossfade audio) says how much is actually left to hide after loop-point
        selection. A clean, phase-aligned splice needs ~`_XFADE_BASE_PERIODS` cycles; a rough
        one scales up toward `+ _XFADE_DISC_PERIODS` cycles.

    `min_disc` floors the discontinuity term for loops we know aren't phase-validated (the
    central-region fallback). The result is clamped to [_XFADE_FLOOR_MS, _XFADE_CAP_MS];
    bake_loop_crossfade further caps it to a quarter of the loop body and the room before
    loop_start, so an over-long request can never eat the loop.
    """
    start, end = loop_points
    mono = audio.to_mono()
    if start < 1 or end < 2 or end > len(mono):
        return _XFADE_FLOOR_MS
    period_ms = 1000.0 / midi_to_hz(note)
    amp_disc, deriv_disc = _seam_disc(mono, start, end)
    d = min(1.0, max(min_disc, amp_disc / _AMP_DISC_THRESHOLD, deriv_disc / _DERIV_DISC_THRESHOLD))
    periods = _XFADE_BASE_PERIODS + _XFADE_DISC_PERIODS * d
    ms = periods * period_ms
    return max(_XFADE_FLOOR_MS, min(ms, _XFADE_CAP_MS))


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


def _period_aligned_loop(
    mono: np.ndarray, sr: int, region_start: int, region_end: int, carrier: int | None
) -> tuple[int, int, float] | None:
    """Loop whose length is an integer multiple of the signal's true repeat period.

    Found by minimising the raw seam mismatch (no crossfade) over candidate lengths, locked to
    the carrier. Returns (loop_start, loop_end, raw_mismatch) for the LONGEST clean multiple up
    to _PERIOD_TARGET_MAX_S, or None when no clean loop exists (genuinely aperiodic notes — they
    fall through to the existing candidate machinery).

    The raw mismatch compares the W samples before loop_end with the W before loop_start: a
    period-aligned length matches (envelope-continuous wrap), a beat-synced one does not. W ≈ two
    carrier periods so the measure reflects waveform continuity at the note's pitch.
    """
    n = len(mono)
    t_wave = carrier if (carrier and carrier > 0) else int(sr / 440)
    W = max(512, 2 * t_wave)
    lo = int(_PERIOD_SEARCH_MIN_S * sr)
    target_max = int(_PERIOD_TARGET_MAX_S * sr)

    def _search(ls: int, off_lo: int, off_hi: int) -> tuple[int, float] | None:
        """Full-resolution min raw mismatch for loop_end in [ls+off_lo, ls+off_hi].

        The mismatch minimum is needle-sharp (it needs sample-accurate carrier-phase alignment),
        so a strided search steps over it. Compute the whole curve cheaply via cross-correlation:
        ||x[e-W:e]-pre||^2 = E_win(e) - 2·(x⋆pre)(e) + E_pre, with E_win from a cumulative sum.
        """
        lo_i, hi_i = ls + off_lo, ls + off_hi
        if hi_i <= lo_i or lo_i - W < 0 or hi_i > n:
            return None
        pre = mono[ls - W : ls]
        pre_e = float(np.dot(pre, pre))
        if pre_e <= 0:
            return None
        seg = mono[lo_i - W : hi_i]
        cross = np.correlate(seg, pre, mode="valid")
        csq = np.concatenate(([0.0], np.cumsum(seg * seg)))
        win_e = csq[W:] - csq[:-W]
        m = min(len(cross), len(win_e))
        dist = np.sqrt(np.maximum(win_e[:m] - 2.0 * cross[:m] + pre_e, 0.0)) / np.sqrt(pre_e)
        bi = int(np.argmin(dist))
        return lo_i + bi, float(dist[bi])

    def _aligned_at(ls: int) -> tuple[int, int, float] | None:
        """Best period-aligned loop anchored at a given loop_start."""
        base = _search(ls, lo, min(int(_PERIOD_SEARCH_MAX_S * sr), region_end - W - ls))
        if base is None:
            return None
        base_end, base_mm = base
        period = base_end - ls
        if period <= 0:
            return None
        # Among in-phase multiples up to the target length, take the longest that stays clean
        # (a couple of cycles sound less mechanical than one; a multiple drifted out of phase —
        # the high notes — has rising mismatch and is rejected back to the base).
        tol = max(1.5 * base_mm, base_mm + 0.05)
        best_end, best_mm = base_end, base_mm
        for k in range(2, 6):
            if k * period > target_max:
                break
            band = max(W, period // 8)
            sub = _search(ls, k * period - band, k * period + band)
            if sub and sub[1] <= tol and sub[0] < region_end - W:
                best_end, best_mm = sub
        return ls, best_end, best_mm

    # Scan several anchors through the sustain: sustain_start sits right after the attack where the
    # tone is still settling and never aligns; a few % in it locks cleanly. Each anchor is snapped
    # to a same-direction zero crossing so the wrap value matches.
    results: list[tuple[int, int, float]] = []
    for frac in (0.10, 0.15, 0.20, 0.30, 0.40):
        anchor = max(region_start + int(frac * (region_end - region_start)), W + 1)
        rising = float(mono[min(anchor + 4, n - 1)]) > float(mono[max(anchor - 4, 0)])
        ls = _snap_to_slope_zero_crossing(mono, anchor, rising, max(_ZC_SNAP_RADIUS, t_wave // 2))
        r = _aligned_at(ls)
        if r is not None:
            results.append(r)

    clean = [r for r in results if r[2] <= _PERIOD_ALIGN_MAX_MISMATCH]
    if not clean:
        return None
    # Cleanest wins; break near-ties (within tolerance) toward the longer loop.
    best_mm = min(r[2] for r in clean)
    near = [r for r in clean if r[2] <= max(best_mm + 0.05, best_mm * 1.5)]
    return max(near, key=lambda r: r[1] - r[0])


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

    # When the patch stacks a sub-octave, the loop length must be a whole number of SUB periods or it
    # wraps mid-sub-cycle (audible reset). sub_period_exact is that length unit — the exact octave
    # multiple of the fundamental from the known pitch — or None for plain tones. It re-quantizes the
    # snapped length below; the rest of the pipeline (ranking, phase lock) stays on the fundamental
    # t_wave, so non-sub sounds are byte-identical to before.
    sub_period_exact = _exact_sub_period(mono, sr, region_start, region_end, midi_note, hint_period)
    if sub_period_exact:
        # Seed the pool with sub-period-length candidates at several starts. Re-quantizing a single
        # fundamental-aligned pick to the sub grid can fail (no nearby sub multiple is level-continuous
        # at that start); offering sub-aligned lengths at multiple starts lets the seam scorer find one
        # that is both sub-locked and clean, instead of falling back to a fundamental-misaligned loop.
        for s, e in _waveform_period_candidates(sr, int(round(sub_period_exact)), region_start, region_end):
            raw.append((s, e, 0.0, "sub"))

    if tempo_bpm is not None:
        for s, e in _tempo_candidates(sr, tempo_bpm, region_start, region_end):
            raw.append((s, e, 0.0, "tempo"))

    # Direct period-aligned loop — the lever for the audible loop pumping. Computed up front so a
    # clean aligned loop can be offered ahead of the beat-synced candidates below (prepended at
    # result construction). None for aperiodic notes, which keep the existing behaviour.
    aligned = _period_aligned_loop(mono, sr, region_start, region_end, t_wave or hint_period)

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

    # Window for phase-lock / seam-match: a couple of fundamental periods. Locking the wrap
    # endpoints to matching phase keeps the whole blended span aligned through periodicity, so
    # this need not grow with the (now note-adaptive) crossfade length — see adaptive_crossfade_ms.
    lock_win = max(2 * t_wave, int(0.012 * sr)) if t_wave else 0

    # Window for the peak/asymmetry drift sensor: a few fundamental periods so the peak is
    # stable, wider than the seam-match window. Pitch-free sounds use a fixed ~30 ms.
    drift_win = max(lock_win, 4 * t_wave) if t_wave else int(0.03 * sr)
    peak = float(np.abs(mono).max()) or 1.0

    # Pass 1: snap all candidates to same-direction zero crossings, deduplicate.
    seen_snap: set[tuple[int, int]] = set()
    snap_pool: list[tuple[int, int, float, str]] = []
    for s, e, pre_chroma, src in candidates:
        if s < 0 or e >= n - _FRAME:
            continue
        if t_wave:
            # Pitched: put the start on a flat part of the cycle (not the steep zero
            # crossing) and phase-lock the end to it. Independent zero-crossing snapping
            # breaks period alignment on rich waveforms and lands the seam on the edge,
            # causing the mid-crossfade dip and a false click reading.
            s_snap = _snap_to_flat(mono, s, zc_radius, max(4, t_wave // 8))
            e_snap = _phase_lock_end(mono, s_snap, s_snap + (e - s), t_wave, lock_win)
            if sub_period_exact:
                # Sub-octave patch: snap to a whole number of exact sub-cycles. _phase_lock_end
                # matches the dominant fundamental and can settle an ODD count of fundamental periods
                # off (half a sub-cycle → audible reset). _snap_end_to_sub picks the sub-aligned
                # length whose wrap is also level-continuous.
                e_snap = _snap_end_to_sub(mono, s_snap, e_snap, sub_period_exact)
        else:
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
    scored: list[tuple[float, int, int, str, float, float, float]] = []
    dbg_cands: list[dict] = []
    for s, e, _sim, src in snap_pool:
        comps = _boundary_components(mono, sr, s, e)
        score = comps["score"]
        amp_drift = _amp_drift(mono, s, e, drift_win, peak)
        passed = score >= quality_threshold
        # MFCC is only needed for ranking the passing candidates (or for the debug dump); skip it
        # for the many that fail the base seam score to avoid a per-candidate STFT.
        mfcc_dist = _mfcc_seam_distance(mono, sr, s, e) if (passed or dbg) else 0.0
        if passed:
            scored.append((score, s, e, src, comps["chroma"], amp_drift, mfcc_dist))
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
                "amp_drift": round(amp_drift, 4),
                "timbre_drift": round(1.0 - comps["chroma"], 4),
                "mfcc_dist": round(mfcc_dist, 4),
                "seam_match": round(_seam_match(mono, s, e, lock_win), 4) if t_wave else 0.0,
                "passed": score >= quality_threshold,
            })

    # Rank passing candidates by a length reward and the timbre-drift penalty, so the first pick
    # is a long loop on a timbrally-stable span. amp_drift is applied as a gate below, not here.
    # The returned score stays the base quality score.
    def _rank(score: float, chroma: float, length: int, amp_drift: float, w_drift: float) -> float:
        length_reward = min((length / sr) / _LOOP_LEN_TARGET_S, 1.0)
        timbre_drift = 1.0 - chroma
        drift_penalty = w_drift * min(amp_drift / _AMP_DRIFT_NORM, 1.0)
        return score + _W_LENGTH * length_reward - _W_TIMBRE_DRIFT * timbre_drift - drift_penalty

    # Seam gate (pitched only): drop loops that wrap across too much phase/timbre change — the
    # cause of the mid-crossfade dip — so the length reward picks the longest *clean* loop
    # rather than the longest loop. Never wipe the pool: if every candidate exceeds the gate
    # (a strongly evolving sound), keep the cleanest seam instead of falling through to the
    # central-region fallback.
    seam_of = (lambda s, e: _seam_match(mono, s, e, lock_win)) if t_wave else (lambda s, e: 0.0)
    clean = [t for t in scored if seam_of(t[1], t[2]) <= _SEAM_GATE]
    # The drift gate only engages for steady sounds; a dramatically-evolving sound keeps prior
    # length behaviour (see _DRIFT_GATE_MAX_MOVE).
    peak_move = _peak_env_movement(mono, region_start, region_end)
    steady = peak_move <= _DRIFT_GATE_MAX_MOVE
    # Steady sounds use the drift GATE above (so the ranking penalty would be redundant — kept at
    # 0 to leave that tuned path unchanged). Evolving sounds skip the gate; there the additive
    # amp_drift penalty arbitrates among the long candidates, preferring the phase-aligned one.
    w_drift = 0.0 if steady else _W_AMP_DRIFT
    if clean:
        # Drift gate within the seam-clean pool: drop loops that wrap across a peak/asymmetry
        # drift (the per-loop wobble) so the length reward picks the longest *stationary* loop,
        # not just the longest one. Never wipe — if every seam-clean loop drifts, keep them all
        # and let length/timbre decide (no worse than before the gate). t[5] is amp_drift.
        low_drift = [t for t in clean if t[5] <= _DRIFT_GATE] if steady else []
        pool = low_drift if low_drift else clean
        # Timbre-travel gate (never wipe): drop loops whose body travels in timbre (MFCC, t[6]).
        # Chroma is blind to FM brightness motion, so a clean-seam, low-drift loop can still reset
        # its timbre on every wrap (ELECTRON 1 / C5). Filtering these lets the length reward pick
        # the longest *timbrally-stationary* loop. If every candidate travels, keep them all.
        low_timbre = [t for t in pool if t[6] <= _MFCC_SEAM_GATE]
        pool = low_timbre if low_timbre else pool
        # Length gate (never wipe): once we have clean, low-drift loops, a sub-floor micro-loop
        # must not win on its trivially-perfect self-seam. Drop them when any longer loop remains.
        long_enough = [t for t in pool if (t[2] - t[1]) >= _MIN_LOOP_RANK_S * sr]
        pool = long_enough if long_enough else pool
        pool.sort(key=lambda t: (_rank(t[0], t[4], t[2] - t[1], t[5], w_drift), t[1], t[2]), reverse=True)
        ranked = pool
    else:
        # No candidate has a clean raw seam (e.g. an FM high note whose carrier can't phase-lock
        # across the timbre the loop spans — every seam_match ≈ 1.0). The baked crossfade has to
        # mask the seam either way. Keep the existing cleanest-seam pick UNLESS it travels in timbre
        # (the reset the crossfade can't hide, ELECTRON 1 / C5): only then switch — to the LONGEST
        # timbrally-stationary loop (the seam is crossfade-masked, long loops are wanted). Leaving
        # the pick alone when it is already stationary keeps every clean preset's loops untouched.
        long_enough = [t for t in scored if (t[2] - t[1]) >= _MIN_LOOP_RANK_S * sr]
        base_pool = long_enough if long_enough else scored
        seam_first = sorted(base_pool, key=lambda t: (-seam_of(t[1], t[2]), _rank(t[0], t[4], t[2] - t[1], t[5], w_drift)), reverse=True)
        low_timbre = [t for t in base_pool if t[6] <= _MFCC_SEAM_GATE]
        if low_timbre and seam_first[0][6] > _MFCC_SEAM_GATE:
            low_drift = [t for t in low_timbre if t[5] <= _DRIFT_GATE] if steady else []
            pool = low_drift if low_drift else low_timbre
            ranked = sorted(pool, key=lambda t: ((t[2] - t[1]), -seam_of(t[1], t[2])), reverse=True)
        else:
            ranked = seam_first
    result = [((s, e), sc) for sc, s, e, *_ in ranked[:max_candidates]]
    # A strong slow timbre-LFO (t_mod from the spectral detector) makes any sub-cycle carrier-
    # aligned loop wrap mid-sweep: it is raw-seam-clean yet audibly pumps on every wrap.
    # _period_aligned_loop caps its length at _PERIOD_TARGET_MAX_S, so when the LFO is longer than
    # that the aligned loop can never span a full cycle — suppress it and let the ranker pick the
    # longest whole-cycle loop instead. (Faster modulations, where an aligned loop can still span a
    # cycle, are unaffected.)
    slow_lfo = bool(t_mod) and t_mod > int(_PERIOD_TARGET_MAX_S * sr)
    if aligned is not None and not slow_lfo and _mfcc_seam_distance(mono, sr, aligned[0], aligned[1]) <= _MFCC_SEAM_GATE:
        a_s, a_e, a_mm = aligned
        # Offer the period-aligned loop first: the pipeline validates candidates in order and takes
        # the first that passes splice validation, so a clean aligned loop wins over the ranked
        # seam picks. Score maps the raw mismatch to the quality scale (≥ threshold so it is kept).
        # Skipped when the aligned loop travels in timbre (rare — a clean carrier seam usually means
        # a stationary tone) so the MFCC-gated ranked pool can supply a stationary loop instead.
        a_score = max(quality_threshold, round(1.0 - a_mm, 3))
        result = [((a_s, a_e), a_score)] + [r for r in result if r[0] != (a_s, a_e)]
        result = result[:max_candidates]
    scored = ranked  # keep debug ('best', n_passed) consistent with the returned order

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
                "result0": (
                    {"start": result[0][0][0], "end": result[0][0][1],
                     "len_s": round((result[0][0][1] - result[0][0][0]) / sr, 3)}
                    if result else None
                ),
                "aligned": (
                    {"start": aligned[0], "end": aligned[1], "mm": round(aligned[2], 4),
                     "len_s": round((aligned[1] - aligned[0]) / sr, 3),
                     "mfcc": round(_mfcc_seam_distance(mono, sr, aligned[0], aligned[1]), 4)}
                    if aligned is not None else None
                ),
                "chroma_movement": round(_chroma_movement(fps), 4),
                "centroid_cv": round(_centroid_movement(mono, sr, region_start, region_end), 4),
                "rms_depth": round(_rms_depth_region(mono, region_start, region_end), 4),
                "peak_movement": round(peak_move, 4),
                "drift_gated": bool(steady),
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


def central_fallback_loop(
    buf: AudioBuffer,
    sustain_start: int,
    sustain_end: int,
    midi_note: int | None = None,
) -> tuple[int, int] | None:
    """Last-resort loop over the central ~50% of the sustain when no scored candidate passed.

    Sustained notes must all get a loop (a partial multisample is unplayable). When a
    fundamental period is detectable, snap the length to an integer number of periods and
    phase-lock the end window to the start, so even this fallback blends in-phase windows and
    cannot phase-cancel in the crossfade. Without a period (e.g. dual-oscillator/octave
    patches) fall back to the raw central window. Returns (start, end) or None if the sustain
    region is too short to loop.
    """
    mono = buf.to_mono()
    sr = buf.sample_rate
    n = len(mono)
    region = sustain_end - sustain_start
    loop_len = region // 2
    if loop_len < max(int(0.1 * sr), 4):
        return None
    start = sustain_start + region // 4
    hint = _midi_to_period(sr, midi_note) if midi_note is not None else None
    t_wave = _detect_waveform_period(mono, sr, sustain_start, sustain_end, hint)
    if t_wave:
        loop_len = max(1, round(loop_len / t_wave)) * t_wave
        start = _snap_to_flat(mono, start, max(_ZC_SNAP_RADIUS, t_wave // 2), max(4, t_wave // 8))
        lock_win = max(2 * t_wave, int(0.012 * sr))
        end = _phase_lock_end(mono, start, start + loop_len, t_wave, lock_win)
        # Sub-octave patches: force the loop onto a whole number of exact sub-cycles so the
        # fundamental-matching phase lock can't settle half a sub-cycle off (audible reset). No-op
        # for plain tones (no sub detected), so only sub-octave notes are affected.
        sub_exact = _exact_sub_period(mono, sr, sustain_start, sustain_end, midi_note, hint)
        if sub_exact:
            end = _snap_end_to_sub(mono, start, end, sub_exact)
    else:
        end = start + loop_len
    if start < 1 or end <= start or end >= n:
        return None
    return start, end


# ── Loop crossfade ──────────────────────────────────────────────────────────────────────────

# Crossfade curve selection. An equal-gain (linear) fade holds a constant SUM, so it keeps the
# level flat only when the two blended windows are CORRELATED — the phase-locked primary loop,
# where the loop tail equals the pre-start copy sample-for-sample. An equal-power (sin/cos) fade
# holds a constant sum of SQUARES, flat for UNcorrelated windows — the central fallback, or a loop
# wrapping across a modulation/timbre change. Using the wrong curve makes the OPPOSITE artifact:
# equal-gain dips ~3 dB on uncorrelated material, equal-power bumps ~3 dB on correlated material.
# The midpoint-RMS crossover sits at Pearson r = 1/3 (RMS² ∝ 0.5·(1+r) for linear vs (1+r) for
# equal-power at the blend midpoint; linear is flatter above r=1/3, equal-power below). "auto"
# measures r at each seam and picks the flatter curve, so the phase-locked path (r≈1) keeps the
# existing linear fade and only the fallback/evolving seams switch to equal-power.
_XFADE_COHERENCE_THRESHOLD = 1.0 / 3.0


def _crossfade_gains(n: int, equal_power: bool) -> tuple[np.ndarray, np.ndarray]:
    """Return (fade_out, fade_in) gain ramps of length n for the chosen curve."""
    t = np.linspace(0.0, 1.0, n, dtype=np.float32)
    if equal_power:
        return np.cos(t * (np.pi / 2.0)).astype(np.float32), np.sin(t * (np.pi / 2.0)).astype(np.float32)
    return (1.0 - t).astype(np.float32), t


def _seam_needs_equal_power(curve: str, fade_out_win: np.ndarray, fade_in_win: np.ndarray) -> bool:
    """Resolve the crossfade curve for a single seam.

    curve "equal_gain"/"equal_power" force the choice. "auto" measures the Pearson correlation of
    the two (mono) windows the fade blends and returns True (equal-power) when they are uncorrelated
    enough that a linear fade would dip more than an equal-power one (r <= _XFADE_COHERENCE_THRESHOLD).
    """
    if curve == "equal_power":
        return True
    if curve == "equal_gain":
        return False
    if curve != "auto":
        raise ValueError(f"unknown crossfade curve: {curve!r}")
    a = fade_out_win - fade_out_win.mean()
    b = fade_in_win - fade_in_win.mean()
    denom = float(np.sqrt(float(np.dot(a, a)) * float(np.dot(b, b))))
    if denom <= 1e-12:
        return False  # a (near-)silent window can't audibly dip; linear avoids the equal-power bump
    r = float(np.dot(a, b) / denom)
    return r <= _XFADE_COHERENCE_THRESHOLD


def bake_loop_crossfade(
    buf: AudioBuffer,
    loop_start: int,
    loop_end: int,
    loop_fade_ms: float,
    release_fade_ms: float | None = None,
    curve: str = "auto",
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
    quickly. `curve` picks the fade shape per seam: "auto" (default) measures whether the
    blended windows are phase-coherent and uses equal-gain (linear) for correlated windows —
    the phase-locked primary loop, where linear holds the level flat — or equal-power (sin/cos)
    for uncorrelated ones — the central fallback or a loop wrapping a timbre change, where linear
    would dip ~3 dB. "equal_gain"/"equal_power" force one curve (see _XFADE_COHERENCE_THRESHOLD).
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
        existing = buf.data[:, loop_end - loop_fade_len : loop_end]
        pre_ls = buf.data[:, loop_start - loop_fade_len : loop_start]
        equal_power = _seam_needs_equal_power(curve, existing.mean(axis=0), pre_ls.mean(axis=0))
        fade_out, fade_in = _crossfade_gains(loop_fade_len, equal_power)
        out.data[:, loop_end - loop_fade_len : loop_end] = existing * fade_out + pre_ls * fade_in

    if release_fade_len >= 2:
        existing_tail = buf.data[:, loop_end : loop_end + release_fade_len]
        post_ls = buf.data[:, loop_start : loop_start + release_fade_len]
        equal_power = _seam_needs_equal_power(curve, post_ls.mean(axis=0), existing_tail.mean(axis=0))
        fade_out, fade_in = _crossfade_gains(release_fade_len, equal_power)
        out.data[:, loop_end : loop_end + release_fade_len] = post_ls * fade_out + existing_tail * fade_in

    return out

"""Spectral analysis → archetype classification for Serum-format wavetables.

See docs/inputs/wavetables.md: rather than one generic XML template for every
wavetable, analyse each file's own spectral content and let it pick the patch's
archetype (envelope/filter shape) plus a few continuous parameters (WT scan start
position, LFO2→position depth, filter cutoff) driven by that file's own material.

Two things this deliberately does NOT do, both learned from the previous revision:

*No analysis window.* A wavetable frame is exactly one cycle by format convention,
so FFT bin k IS harmonic k and a periodic-extension window has nothing to fix. The
old code applied a Hanning window, which smeared every harmonic into its neighbours:
a square wave (no even harmonics at all) measured its 2nd harmonic at 0.67x the
fundamental instead of 0, and a sawtooth's defining wrap discontinuity was tapered
away so it read *darker* than a triangle. Brightness, hollowness and tonality were
all corrupted by it. Rectangular is correct here, and exact.

*No inferring an envelope from timbre where the data doesn't support it.* Timbre and
playing style are independent axes — a bright hollow wave is equally a clav, a reed
lead or a pad, and nothing in a single-cycle table says which. Only the two
archetypes that ARE table properties (does it move? is it pitched?) are detected;
`percussive` exists as a template but is reachable only via `--archetype`. The one
continuous feature that might have split it (`hollowness`) is unimodal across the
701-file reference corpus, so any threshold on it would be arbitrary.

Thresholds are quantiles of that corpus (Liam Wavetables + Polyend Wavetables) and
are named below with the quantile they came from. Still a first pass to tune by ear —
same status as e.g. _LOOP_TIMBRE_SEAM_MAX in runner/scan.py — but a file near any
boundary it actually used is flagged REVIEW rather than silently committed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..model.audio import AudioBuffer

FRAME_SIZE = 2048

# How many of the strongest bins the tonality measure counts. A pitched wave — even a
# very bright one — puts nearly all its energy in a few dozen harmonics; noise spreads
# it across all 1024. 32 separates a square (0.99) from white noise (0.14) cleanly,
# where spectral flatness put the square at 0.39 and called it a drone.
_TONALITY_BINS = 32

# Corpus quantiles. Brightness and movement are spectral centroids measured in
# HARMONICS (bin index), not normalised to Nyquist — "centroid sits on the 139th
# harmonic" is a number you can reason about, and it feeds the filter cutoff directly.
_TONALITY_MIN = 0.65    # ~p10: below this the frames are noise/texture, not a pitch
_MOVE_HI = 150.0        # ~p75 of frame-to-frame centroid range, in harmonics
_BRIGHT_LO = 60.0       # ~p15, for the timbre tag only
_BRIGHT_HI = 227.0      # ~p75, for the timbre tag only

# A file whose deciding feature lands within this relative distance of its threshold
# gets a REVIEW comment: the classification is a coin flip and wants an ear on it.
_MARGIN = 0.05

# Brightness (in harmonics) that maps to a fully open filter, before the archetype
# bias. Set so the corpus spreads across the range instead of bunching at the ceiling:
# ~p95 of measured brightness, leaving only the genuinely brightest tables wide open.
_CUTOFF_HARMONICS = 600.0

# Envelope (attack/decay/sustain/release as 0..1 fractions, spread across the Deluge's
# full Q31 range later by the exporter's _q31()) + filter type + a cutoff bias layered
# on top of the file's own brightness-driven cutoff.
_ARCHETYPES: dict[str, dict] = {
    # The default, ~72% of the reference corpus. Responsive rather than swelling: this
    # is the starting point for a table nothing else is known about, and a patch that
    # speaks when you press the key is a better starting point than one that fades in.
    "sustaining": dict(attack=0.10, decay=0.35, sustain=0.90, release=0.35,
                       filter_type="lpf", cutoff_bias=0.0),
    # Movement or noise in the table itself — give it room to happen.
    "evolving": dict(attack=0.75, decay=0.60, sustain=1.00, release=0.85,
                     filter_type="lpf", cutoff_bias=-0.05),
    # Not auto-detected (see module docstring) — `scan-wavetables --archetype percussive`.
    "percussive": dict(attack=0.00, decay=0.25, sustain=0.00, release=0.15,
                       filter_type="lpf", cutoff_bias=0.10),
}

AUTO_ARCHETYPES = ("sustaining", "evolving")

# Timbre tags, kept deliberately separate from the envelope archetype: brightness and
# harmonic character genuinely describe how a table SOUNDS, which is a fair thing to
# put in a browser tag even though it says nothing about how it should be played. The
# Bento exporter feeds these into derive_tags as if they were words in the preset name
# (bento.py:_tag_name), where "pad"/"bass"/"lead" and "drone"/"evolving" are already
# vocabulary -> Pad/Bass/Lead/Atmosphere.
_TAGS = ("drone", "evolving", "bass", "lead", "pad")


@dataclass
class WavetableAnalysis:
    archetype: str
    wt_position: float
    lfo2_rate: float
    lfo2_depth: float
    filter_cutoff: float
    attack: float
    decay: float
    sustain: float
    release: float
    filter_type: str
    tag_hint: str = "pad"
    flags: list[str] = field(default_factory=list)


def _frame_features(audio: AudioBuffer, frame_size: int = FRAME_SIZE) -> dict | None:
    """Per-2048-sample-frame spectral features, or None if shorter than one frame.

    A trailing partial frame is analysed out rather than rejected. Polyend's own
    stock wavetables ship a few samples shy of a whole window and the Tracker
    floors to whole windows when loading them, so a non-multiple length means a
    partial tail — not a file that missed the frame grid.
    """
    mono = audio.to_mono().astype(np.float64)
    n = len(mono)
    if n < frame_size:
        return None

    n_frames = n // frame_size
    remainder = n - n_frames * frame_size
    frames = mono[:n_frames * frame_size].reshape(n_frames, frame_size)

    centroids: list[float] = []
    tonalities: list[float] = []
    hollownesses: list[float] = []
    for frame in frames:
        # No window: the frame is exactly one period, so bin k is harmonic k exactly.
        spectrum = np.abs(np.fft.rfft(frame))
        spectrum[0] = 0.0  # drop DC so it can't skew the centroid
        total = spectrum.sum()
        if total < 1e-9:
            centroids.append(0.0)
            tonalities.append(1.0)
            hollownesses.append(0.5)
            continue

        bins = np.arange(len(spectrum))
        centroids.append(float((bins * spectrum).sum() / total))

        power = spectrum * spectrum
        strongest = np.sort(power)[::-1][:_TONALITY_BINS].sum()
        tonalities.append(float(strongest / power.sum()))

        # Hollowness: odd harmonics from the 3rd up, against the even ones. The
        # fundamental is excluded deliberately — it is almost always the loudest
        # partial, and counting it (as the previous revision's odd/even ratio did)
        # made the measure track "how much fundamental is there" instead of harmonic
        # character, ranking a sawtooth as more hollow than a square.
        odd = spectrum[3::2].sum()
        even = spectrum[2::2].sum()
        upper = odd + even
        # A near-pure sine has no upper harmonics to weigh, so hollowness is undefined
        # rather than 0 or 1 — say "balanced" and let other features decide.
        hollownesses.append(0.5 if upper < 0.02 * spectrum[1] else float(odd / upper))

    return {
        "centroids": centroids,
        "tonalities": tonalities,
        "hollownesses": hollownesses,
        "n_frames": n_frames,
        "remainder": remainder,
    }


def _near(value: float, threshold: float) -> bool:
    """True if `value` sits within _MARGIN (relative) of a decision threshold."""
    return abs(value - threshold) <= _MARGIN * abs(threshold)


def _timbre_tag(bright: float, move: float, tonality: float, hollow: float) -> str:
    if tonality < _TONALITY_MIN:
        return "drone"
    if move > _MOVE_HI:
        return "evolving"
    if bright < _BRIGHT_LO:
        return "bass"
    if bright > _BRIGHT_HI and hollow >= 0.5:
        return "lead"
    return "pad"


def _result(archetype: str, wt_position: float, lfo2_rate: float, lfo2_depth: float,
            filter_cutoff: float, tag_hint: str, flags: list[str]) -> WavetableAnalysis:
    t = _ARCHETYPES[archetype]
    return WavetableAnalysis(
        archetype=archetype,
        wt_position=wt_position,
        lfo2_rate=lfo2_rate,
        lfo2_depth=lfo2_depth,
        filter_cutoff=filter_cutoff,
        attack=t["attack"],
        decay=t["decay"],
        sustain=t["sustain"],
        release=t["release"],
        filter_type=t["filter_type"],
        tag_hint=tag_hint,
        flags=flags,
    )


def classify_archetype(audio: AudioBuffer) -> WavetableAnalysis:
    """Classify a wavetable file into an archetype and derive its patch parameters."""
    feats = _frame_features(audio)
    if feats is None:
        return _result(
            "sustaining", wt_position=0.0, lfo2_rate=0.3, lfo2_depth=0.3,
            filter_cutoff=0.55, tag_hint="pad",
            flags=[f"audio is shorter than one {FRAME_SIZE}-sample wavetable frame — "
                   "defaulted to sustaining, verify manually"],
        )

    flags: list[str] = []
    if feats["remainder"]:
        flags.append(f"{feats['remainder']} trailing samples ignored — file is not a whole "
                     f"number of {FRAME_SIZE}-sample windows")

    centroids = feats["centroids"]
    n_frames = feats["n_frames"]
    bright = float(np.mean(centroids))                                   # in harmonics
    move = float(np.max(centroids) - np.min(centroids))                  # in harmonics
    tonality = float(np.mean(feats["tonalities"]))
    hollow = float(np.mean(feats["hollownesses"]))

    # Two questions the table itself can actually answer: is it pitched, and does it
    # move? Everything else about how it should be played is the player's call.
    if tonality < _TONALITY_MIN:
        archetype = "evolving"
        if _near(tonality, _TONALITY_MIN):
            flags.append(f"tonality {tonality:.3f} sits on the {_TONALITY_MIN} "
                         "noise/pitch boundary — evolving vs sustaining is marginal")
    elif move > _MOVE_HI:
        archetype = "evolving"
        if _near(move, _MOVE_HI):
            flags.append(f"frame movement {move:.1f} harmonics sits on the {_MOVE_HI} "
                         "boundary — evolving vs sustaining is marginal")
    else:
        archetype = "sustaining"
        if _near(tonality, _TONALITY_MIN) or _near(move, _MOVE_HI):
            flags.append(f"tonality {tonality:.3f} / movement {move:.1f} sit near the "
                         "evolving boundary — sustaining is marginal")

    tag_hint = _timbre_tag(bright, move, tonality, hollow)

    # WT start position: the brightest frame for percussive (all of its character is in
    # the attack), the darkest for sustaining (leaves somewhere for the LFO to go), the
    # start of the table for evolving since LFO2 sweeps the whole thing anyway.
    if archetype == "percussive":
        idx = int(np.argmax(centroids))
    elif archetype == "sustaining":
        idx = int(np.argmin(centroids))
    else:
        idx = 0
    wt_position = idx / max(1, n_frames - 1)

    # A table that already moves gets a slow, wide sweep to expose it; a static one gets
    # a faster, deeper sweep to manufacture some.
    if move > _MOVE_HI:
        lfo2_rate, lfo2_depth = 0.15, 0.45
    elif move > _MOVE_HI / 3:
        lfo2_rate, lfo2_depth = 0.30, 0.60
    else:
        lfo2_rate, lfo2_depth = 0.55, 0.75

    # Position and depth are chosen independently above, so clamp the sweep to the table
    # it actually has left to travel through. Without this a start position at the last
    # frame still got a 0.6-depth sweep, which just pins against the end of the table.
    lfo2_depth = min(lfo2_depth, 1.0 - wt_position)

    # Cutoff straight off the measured brightness, which is in harmonics: the table's
    # centroid says where its energy actually is, so open the filter to match and let
    # the archetype bias nudge it. (The previous revision normalised brightness by
    # Nyquist, which made it far too small to use as an openness fraction and clamped
    # almost every file to a nearly-closed filter, silencing the oscillator.)
    cutoff_bias = _ARCHETYPES[archetype]["cutoff_bias"]
    filter_cutoff = max(0.05, min(0.95, 0.35 + bright / _CUTOFF_HARMONICS + cutoff_bias))

    return _result(archetype, wt_position, lfo2_rate, lfo2_depth, filter_cutoff,
                   tag_hint, flags)

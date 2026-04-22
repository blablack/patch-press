from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..model.audio import AudioBuffer

_HOP = 512
_SILENCE_DB = -40.0
_QUIET_DB = -20.0
_LOOP_THRESHOLD = 0.7


@dataclass
class ProbeResult:
    duration_s: float
    release_tail_s: float
    loop: bool
    loop_quality: float | None
    sustains: bool
    confidence: str  # "high" | "medium" | "low"
    flags: list[str] = field(default_factory=list)


def _rms_envelope(mono: np.ndarray) -> np.ndarray:
    n = (len(mono) + _HOP - 1) // _HOP
    out = np.zeros(n)
    for i in range(n):
        chunk = mono[i * _HOP: (i + 1) * _HOP]
        out[i] = float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0
    return out


def probe(audio: AudioBuffer, note_off_s: float) -> ProbeResult:
    sr = audio.sample_rate
    note_off_hop = int(note_off_s * sr) // _HOP
    mono = audio.data.mean(axis=0)
    rms = _rms_envelope(mono)

    peak = rms.max()
    if peak < 1e-7:
        return ProbeResult(
            duration_s=2.0, release_tail_s=0.5, loop=False,
            loop_quality=None, sustains=False,
            confidence="low", flags=["silent signal — check preset"],
        )

    silence_thr = peak * (10 ** (_SILENCE_DB / 20))
    quiet_thr = peak * (10 ** (_QUIET_DB / 20))

    hold_rms = rms[:note_off_hop]
    release_rms = rms[note_off_hop:]

    # Sustaining: still audible in the 2nd half of the hold period?
    mid = max(1, len(hold_rms) // 2)
    sustains = bool(np.any(hold_rms[mid:] > silence_thr))

    # Effective duration
    audible_hold = np.where(hold_rms > silence_thr)[0]
    if sustains:
        duration_s = note_off_s
    elif len(audible_hold) > 0:
        duration_s = min((int(audible_hold[-1]) + 1) * _HOP / sr + 0.3, note_off_s)
    else:
        duration_s = 1.0

    # Release tail: last audible hop after note-off
    audible_release = np.where(release_rms > silence_thr)[0]
    release_tail_s = (
        (int(audible_release[-1]) + 1) * _HOP / sr + 0.3
        if len(audible_release) > 0
        else 0.3
    )
    release_tail_s = max(0.3, min(release_tail_s, 8.0))

    duration_s = round(duration_s, 1)
    release_tail_s = round(release_tail_s, 1)

    # Loop detection on the hold region (sustaining sounds only)
    loop = False
    loop_quality: float | None = None
    if sustains:
        from .loop import find_loop_points
        hold_audio = AudioBuffer(
            data=audio.data[:, : int(note_off_s * sr)],
            sample_rate=sr,
        )
        found = find_loop_points(hold_audio, _LOOP_THRESHOLD)
        if found:
            _, q = found
            loop_quality = round(float(q), 3)
            loop = loop_quality >= _LOOP_THRESHOLD

    flags: list[str] = []
    if peak < quiet_thr:
        flags.append("quiet signal, check preset level")
    if loop and loop_quality is not None and loop_quality < 0.8:
        flags.append(f"borderline loop quality ({loop_quality:.2f})")

    confidence = "high" if not flags else ("medium" if len(flags) == 1 else "low")

    return ProbeResult(
        duration_s=duration_s,
        release_tail_s=release_tail_s,
        loop=loop,
        loop_quality=loop_quality,
        sustains=sustains,
        confidence=confidence,
        flags=flags,
    )

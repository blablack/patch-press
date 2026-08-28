"""`preview.wav` — the clip the Bento's patch browser plays when you audition a patch.

The device makes these itself, but only ever on its own save path: saving or importing
a patch on the hardware triggers a note and records the output in real time (the 1.3
release notes call it out as a known issue — "you are still hearing the sound triggered
to create the preview file"). A patch folder written by a build tool never passes
through that path, so it never gets one, and its row in the browser is silent. All 236
factory patches ship a preview; nothing this exporter wrote had one until now.

The format is not guessed. It is invariant across all 236 factory previews and is
reproduced exactly here:

- RIFF PCM, **stereo, 48 kHz, 24-bit**, no exceptions in the whole factory bank.
- Chunks `fmt `(16) + `JUNK`(460 zero bytes) + `data`, which puts the data payload at
  byte 512 — a sector-aligned write, the fingerprint of the device's own streaming
  recorder (factory *sample* WAVs are plain `fmt`+`data` at 0x2c, so the previews come
  from a different writer). The padding is cosmetic for playback but costs 460 bytes,
  and matching the factory layout byte-for-byte removes a variable.
- Exactly 4.000 s for the three roots this exporter writes (SampInst, OneShots,
  Wavetable). The factory bank uses 10 s for Slicer and 16 s for Loops, neither of
  which we produce.
- Referenced by nothing. Not `patch.xml`, not `patchindex.xml` — the loader finds it by
  name, next to `patch.xml`.

What goes *in* the clip follows what the factory previews contain, measured the same
way: a multisample or wavetable preview is one note held for the full four seconds
(single onset, sound from 0.00 to 4.00), and a kit preview is a handful of pad hits
(3-13 across the factory kits). So that is what each one renders.

Fidelity differs by type, and it is worth being plain about it. A multisample or kit
preview is built from the very audio that ships in the patch folder, so it is what the
device will play, minus its filter/envelope/FX. A wavetable preview has no such
shortcut: the sound is made by the device's own oscillator, so this synthesises an
approximation from the same `WavetableAnalysis` the patch was written from (two cells
detuned +/-6 cents, position swept by an LFO, one lowpass, one ADSR). It is meant to be
recognisable in a browser list, not to match the hardware sample for sample. The
frac-to-Hz/seconds mappings that entails are marked below: they are plausible ranges,
not derived ones, in the same spirit as the archetype thresholds in analysis/wavetable.py.
"""

import logging
import struct
from pathlib import Path

import numpy as np

from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

log = logging.getLogger(__name__)

PREVIEW_NAME = "preview.wav"

# The factory format, verified across all 236 factory previews (see the module docstring).
_SR = 48000
_CHANNELS = 2
_BITS = 24
_DURATION_S = 4.0
_FRAMES = int(_SR * _DURATION_S)
# 460 zero bytes, which lands the `data` payload on byte 512.
_JUNK_BYTES = 460

# The note a melodic preview auditions. The device picks one when it records; middle C
# is the obvious choice for a clip whose job is "what does this sound like".
_PREVIEW_NOTE = 60

# Edge shaping. The factory clips are recordings and simply stop at 4.000 s; a rendered
# one has no room tone to hide a discontinuity, so the tail is faded. Short enough to be
# inaudible, long enough to kill the click.
_FADE_IN_S = 0.003
_FADE_OUT_S = 0.10

# Previews are levelled rather than shipped at source gain. The factory clips are not
# normalised (peaks run 0.43 to 1.0) because they are recordings of a device whose output
# gain is already set; a corpus of thousands of library and rendered presets has no such
# common reference, and a preview too quiet to hear is a preview that does not work.
_PEAK = 0.89


# --- wavetable synthesis knobs ------------------------------------------------------
# All four are plausible ranges, NOT derived: the Bento's own parameter scales are 0..1000
# integers with no units in the firmware, so there is nothing to read them off. They only
# shape an approximation of a wavetable patch for browsing (see the module docstring).
_ENV_MAX_S = 3.0            # attack/decay/release fraction -> seconds, squared
_LFO_HZ = (0.05, 8.0)       # lfo2_rate fraction -> position sweep speed
_CUTOFF_HZ = (60.0, 18000.0)  # filter_cutoff fraction -> lowpass corner
# How long the note is held before release, so the clip sustains nearly the whole 4 s
# like the factory ones do.
_NOTE_HOLD_S = 3.2
# The exporter's own +/-60 millisemitone detune between the two wavetable cells.
_WT_DETUNE_SEMITONES = 0.06
_WT_WINDOW = 2048


def _fade(audio: np.ndarray, sr: int) -> np.ndarray:
    n = audio.shape[1]
    n_in = min(int(_FADE_IN_S * sr), n)
    n_out = min(int(_FADE_OUT_S * sr), n)
    if n_in:
        audio[:, :n_in] *= np.linspace(0.0, 1.0, n_in, dtype=np.float32)
    if n_out:
        audio[:, n - n_out:] *= np.linspace(1.0, 0.0, n_out, dtype=np.float32)
    return audio


def _normalise(audio: np.ndarray) -> np.ndarray:
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    return audio * (_PEAK / peak) if peak > 0 else audio


def _resample(data: np.ndarray, sample_rate: int) -> np.ndarray:
    """(2, N) float32 at sample_rate -> (2, M) float32 at 48 kHz."""
    if sample_rate == _SR:
        return data
    import soxr  # deferred: pulls in its C extension, like the .pti exporter's use of it

    out = soxr.resample(data.T, sample_rate, _SR, quality="HQ")
    return np.ascontiguousarray(out.T, dtype=np.float32)


def _fit(audio: np.ndarray) -> np.ndarray:
    """Trim or zero-pad to exactly the fixed preview length."""
    n = audio.shape[1]
    if n > _FRAMES:
        return audio[:, :_FRAMES]
    if n < _FRAMES:
        return np.pad(audio, ((0, 0), (0, _FRAMES - n)))
    return audio


def _to_int24(audio: np.ndarray) -> bytes:
    """(2, N) float -> interleaved little-endian 24-bit PCM."""
    inter = np.ascontiguousarray(np.clip(audio.T, -1.0, 1.0))
    q = np.round(inter * 8388607.0).astype("<i4")
    # Low three bytes of each little-endian int32 are the 24-bit two's-complement sample.
    return np.ascontiguousarray(q).view(np.uint8).reshape(-1, 4)[:, :3].tobytes()


def _riff(pcm: bytes) -> bytes:
    fmt = struct.pack(
        "<HHIIHH", 1, _CHANNELS, _SR, _SR * _CHANNELS * _BITS // 8,
        _CHANNELS * _BITS // 8, _BITS,
    )
    body = (
        b"fmt " + len(fmt).to_bytes(4, "little") + fmt
        + b"JUNK" + _JUNK_BYTES.to_bytes(4, "little") + bytes(_JUNK_BYTES)
        + b"data" + len(pcm).to_bytes(4, "little") + pcm
    )
    return b"RIFF" + (4 + len(body)).to_bytes(4, "little") + b"WAVE" + body


def _write(audio: np.ndarray, sample_rate: int, dest: Path) -> None:
    # Copy before shaping: the multisample path can hand this a *view* into a Sample's
    # own audio (a sample already at 48 kHz and long enough is passed through untouched),
    # and _fade edits in place. Rendering a preview must never alter the audio the patch
    # ships.
    shaped = np.array(_fit(_resample(np.asarray(audio, dtype=np.float32), sample_rate)),
                      dtype=np.float32, copy=True)
    dest.write_bytes(_riff(_to_int24(_fade(_normalise(shaped), _SR))))


# --- multisample --------------------------------------------------------------------
def _pick_note_sample(sset: SampleSet) -> Sample:
    """The one note a melodic preview plays: nearest middle C, velocity nearest 100.

    The same collapse `_build_multisample` uses to choose which velocity layer ships,
    so the preview auditions a sample the patch actually contains, at its own root
    pitch — exactly what the device plays when that key is pressed.
    """
    return min(
        sset.samples,
        key=lambda s: (abs(s.note - _PREVIEW_NOTE), abs(s.velocity - 100), s.round_robin),
    )


def _sustain(sample: Sample, target: int) -> np.ndarray:
    """The sample stretched to `target` frames by repeating its loop, if it has one.

    An unlooped sample is left to end where it ends and the clip runs out into silence,
    which is what the device does with a one-shot too. A looped one is wrapped at its
    own loop points — no crossfade, because the seam here is the same seam the device
    will play, and smoothing it would make the preview flatter than the patch.
    """
    data = sample.audio.data
    n = data.shape[1]
    if n >= target:
        return data[:, :target]

    loop = sample.loop_points
    if not loop:
        return data
    start, end = max(0, min(loop[0], n)), max(0, min(loop[1], n))
    if end - start < 2:
        return data

    body = data[:, start:end]
    reps = int(np.ceil((target - start) / body.shape[1]))
    return np.concatenate([data[:, :start], np.tile(body, (1, reps))], axis=1)[:, :target]


def _render_multisample(sset: SampleSet) -> tuple[np.ndarray, int]:
    s = _pick_note_sample(sset)
    sr = s.audio.sample_rate
    return _sustain(s, int(_DURATION_S * sr)), sr


# --- kit ----------------------------------------------------------------------------
def _render_kit(samples: list[Sample]) -> tuple[np.ndarray, int]:
    """The kit's pads struck in order, spread evenly across the clip.

    Even spacing is what the factory kit previews sound like — 3 hits in one, 13 in
    another, each filling the four seconds — because the device is playing whatever
    pads the kit has. Hits are mixed rather than truncated, so a long tail rings on
    under the next pad.

    Mixed at the preview's own 48 kHz rather than at some pad's rate: `assemble-kits`
    builds a kit by picking one file per instrument category, and nothing says those
    files came from the same vendor at the same rate. Resampling each hit as it lands
    means the mix never depends on the pads agreeing.
    """
    total = _FRAMES
    step = total / len(samples)
    out = np.zeros((2, total), dtype=np.float32)
    for i, s in enumerate(samples):
        at = int(i * step)
        hit = _resample(s.audio.data, s.audio.sample_rate)[:, : total - at]
        out[:, at:at + hit.shape[1]] += hit
    return out, _SR


# --- wavetable ----------------------------------------------------------------------
def _adsr(attack: float, decay: float, sustain: float, release: float, n: int) -> np.ndarray:
    """Linear ADSR over the clip, note released at _NOTE_HOLD_S."""
    a = max(1, int(attack * _SR))
    d = max(1, int(decay * _SR))
    hold = min(n, int(_NOTE_HOLD_S * _SR))
    env = np.zeros(n, dtype=np.float32)

    a = min(a, hold)
    env[:a] = np.linspace(0.0, 1.0, a, dtype=np.float32)
    d = min(d, hold - a)
    if d > 0:
        env[a:a + d] = np.linspace(1.0, sustain, d, dtype=np.float32)
    env[a + d:hold] = sustain

    r = min(max(1, int(release * _SR)), n - hold)
    if r > 0:
        env[hold:hold + r] = np.linspace(env[hold - 1] if hold else 0.0, 0.0, r, dtype=np.float32)
    return env


def _osc(table: np.ndarray, freq: float, position: np.ndarray, n: int) -> np.ndarray:
    """One wavetable oscillator: bilinear read across window position and phase."""
    n_win = table.shape[0]
    phase = (np.arange(n, dtype=np.float64) * (freq / _SR)) % 1.0

    x = phase * _WT_WINDOW
    i0 = x.astype(np.int64) % _WT_WINDOW
    i1 = (i0 + 1) % _WT_WINDOW
    fx = x - np.floor(x)

    w = np.clip(position, 0.0, 1.0) * (n_win - 1)
    w0 = np.clip(w.astype(np.int64), 0, n_win - 1)
    w1 = np.clip(w0 + 1, 0, n_win - 1)
    fw = w - w0

    lo = table[w0, i0] * (1.0 - fx) + table[w0, i1] * fx
    hi = table[w1, i0] * (1.0 - fx) + table[w1, i1] * fx
    return lo * (1.0 - fw) + hi * fw


def _lowpass(sig: np.ndarray, cutoff_hz: float) -> np.ndarray:
    from scipy.signal import butter, sosfilt  # deferred: scipy import is not free

    corner = min(max(cutoff_hz, 20.0), _SR * 0.45)
    sos = butter(2, corner, btype="low", fs=_SR, output="sos")
    return sosfilt(sos, sig)


def _render_wavetable(sset: SampleSet, table_wav: Path) -> tuple[np.ndarray, int]:
    """Approximate what the Bento's oscillator will do with this table and these params.

    Mirrors `_build_wavetable`: two oscillators detuned +/-6 cents, both reading the one
    table, position swept by an LFO that is inverted on the second, then one lowpass and
    the archetype's envelope. Read from the WAV that was just written into the patch
    folder rather than the source file — that copy is mono and window-aligned, which is
    the audio the device will actually load.
    """
    wt = sset.source_metadata["wavetable"]
    mono = AudioBuffer.from_file(table_wav).to_mono().astype(np.float64)
    n_win = len(mono) // _WT_WINDOW
    if n_win < 1:
        raise ValueError(f"{table_wav.name}: shorter than one {_WT_WINDOW}-sample window")
    table = mono[: n_win * _WT_WINDOW].reshape(n_win, _WT_WINDOW)

    n = _FRAMES
    t = np.arange(n, dtype=np.float64) / _SR
    rate = _LFO_HZ[0] * (_LFO_HZ[1] / _LFO_HZ[0]) ** float(np.clip(wt.lfo2_rate, 0.0, 1.0))
    lfo = np.sin(2.0 * np.pi * rate * t) * float(np.clip(wt.lfo2_depth, 0.0, 1.0))
    base = float(np.clip(wt.wt_position, 0.0, 1.0))

    freq = 440.0 * 2.0 ** ((_PREVIEW_NOTE - 69) / 12.0)
    voices = [
        _osc(table, freq * 2.0 ** (-_WT_DETUNE_SEMITONES / 12.0), base + lfo, n),
        _osc(table, freq * 2.0 ** (_WT_DETUNE_SEMITONES / 12.0), base - lfo, n),
    ]

    cut = _CUTOFF_HZ[0] * (_CUTOFF_HZ[1] / _CUTOFF_HZ[0]) ** float(np.clip(wt.filter_cutoff, 0.0, 1.0))
    env = _adsr(
        _ENV_MAX_S * wt.attack ** 2, _ENV_MAX_S * wt.decay ** 2,
        float(np.clip(wt.sustain, 0.0, 1.0)), _ENV_MAX_S * wt.release ** 2, n,
    )
    # The two detuned voices are the only width there is, so the pair is spread rather
    # than summed to a single mono signal played twice.
    left = (voices[0] * 0.7 + voices[1] * 0.3) / 0.5
    right = (voices[0] * 0.3 + voices[1] * 0.7) / 0.5
    stereo = np.stack([_lowpass(left, cut), _lowpass(right, cut)]) * env
    return stereo.astype(np.float32), _SR


# --- entry point --------------------------------------------------------------------
def write_preview(
    sset: SampleSet,
    patch_dir: Path,
    *,
    kit_samples: list[Sample] | None = None,
    table_wav: Path | None = None,
) -> Path:
    """Render this patch's `preview.wav` into its folder and return the path.

    `kit_samples` is the pad list the kit patch actually shipped (post-16-pad thinning),
    and `table_wav` the wavetable copy written beside `patch.xml`; each is required for
    its own category so the preview auditions the patch as exported, not as scanned.
    """
    if sset.category == Category.DRUM:
        if not kit_samples:
            raise ValueError("kit preview needs the exported pad list")
        audio, sr = _render_kit(kit_samples)
    elif sset.category == Category.WAVETABLE:
        if table_wav is None:
            raise ValueError("wavetable preview needs the exported table WAV")
        audio, sr = _render_wavetable(sset, table_wav)
    else:
        audio, sr = _render_multisample(sset)

    dest = patch_dir / PREVIEW_NAME
    _write(audio, sr, dest)
    return dest

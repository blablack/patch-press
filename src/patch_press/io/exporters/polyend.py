"""Polyend Tracker .pti exporter.

A .pti is self-contained — a fixed 392-byte little-endian header followed by the
16-bit PCM itself — so unlike the Deluge exporter there is no XML/SAMPLES split;
each preset becomes exactly one file at <path>/<subfolder>/<name>.pti.

Format per the reverse-engineered spec (github.com/jaap3/pti-file-format) cross-
checked against Polyend's official tracker-lib (github.com/polyend/tracker-lib):
- PCM is 44.1 kHz int16. Stereo (Tracker+/Mini only) is stored PLANAR — all left
  samples then all right — and there is no channel-count header field: the device
  infers stereo from data size = frames*4. True-mono sources are written mono.
- Playback/loop/slice positions are uint16 fractions of the sample length
  (value/65535*frames), not frame indices.
- The Tracker has no multisample keyzones: a synth preset ships the capture
  nearest MIDI 60 and relies on the device repitching it chromatically. A drum
  kit ships as one concatenated sample in beat-slice mode (48 slice max, mode 5
  — plain "Slice" mode 4 instead plays one selected slice repitched across the
  keyboard, which is wrong for a kit), which is the idiomatic Tracker kit.
  Serum wavetables map onto the native wavetable playback mode (2048-frame
  windows) unmodified, stereo ones included — the length field counts frames per
  channel, so the window count is independent of the channel count.
"""

import logging
import struct
import zlib
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from ...config.schema import OutputConfig
from ...model.sample import Category, Sample, SampleSet
from ._common import safe_component, subfolder_parts

log = logging.getLogger(__name__)

_TARGET_SR = 44100  # hardcoded in Polyend's tracker-lib; the header has no rate field
_MAX_SLICES = 48
_NAME_LEN = 31
_HEADER_LEN = 392
_WT_WINDOW = 2048

# Fixed magic + reserved bytes, offsets 0-19: byte-identical to every one of the
# 139 device-saved instruments in the jaap3/pti-file-format test corpus (firmware
# 1.5.0b2) — all of them share this exact prefix.
_HEADER_PREFIX = bytes([
    0x54, 0x49,              # "TI"
    1, 0, 1, 5, 0, 1,
    9, 9, 9, 9,
    116, 1, 0x66, 0x66,
    1, 0, 0, 0,
])

# Playback modes (header byte 76)
_MODE_ONESHOT = 0
_MODE_FWD_LOOP = 1
_MODE_BEAT_SLICE = 5
_MODE_WAVETABLE = 6

# Automation is two bytes at env block +18/+19: (type, enabled). The spec's
# "00/01/11" notation is one digit per byte, not hex — confirmed against
# device-saved files (test/31-42 in the jaap3 corpus).
_AUTO_OFF = (0, 0)
_AUTO_ENVELOPE = (0, 1)
_AUTO_LFO = (1, 1)

# Envelope/LFO block base offsets, in parameter order: volume, panning, cutoff,
# wavetable position, granular position, finetune.
_ENV_OFFSETS = (92, 112, 132, 152, 172, 192)
_LFO_OFFSETS = (212, 220, 228, 236, 244, 252)
_ENV_VOLUME = 0
_ENV_WT_POSITION = 3
_LFO_MAX_STEPS = 28  # steps enum: 0 = slowest subdivision, 28 = fastest

# The .pti has no keyzones: one sample is repitched chromatically across the whole
# keyboard, so the only lever on repitch artefacts is which capture we root on and how
# far it then has to shift. Rooting on the CENTRE of the captured range (not a hardcoded
# MIDI 60) minimises the worst-case shift distance — a bass captured 24-48 centres on ~36
# instead of rooting on its top edge. Full-range captures (24-96) still centre on ~60, so
# a Jupiter/Juno-style patch is unaffected.
#
# The centre is biased slightly HIGH because repitching is asymmetric: shifting a sample
# DOWN (played below its root) muddies gently, shifting UP thins/chipmunks the formants
# faster and can run out of one-shot tail. Biasing the root up puts more of the keyboard
# below it — reached by the more forgiving downward shift. Small, ear-tunable (first pass).
_ROOT_HIGH_BIAS = 2  # semitones the repitch centre is nudged above the captured midpoint


def _scale16(frame: int, num_frames: int) -> int:
    """Map a frame index onto the header's uint16 position scale."""
    if num_frames <= 0:
        return 0
    return max(0, min(65535, round(frame / num_frames * 65535)))


def _resample_to_target(data: np.ndarray, sample_rate: int) -> np.ndarray:
    """(2, N) float32 at sample_rate -> (2, M) float32 at 44.1 kHz."""
    if sample_rate == _TARGET_SR:
        return data
    import soxr  # deferred: pulls in its C extension, and expected_outputs() callers never need it

    out = soxr.resample(data.T, sample_rate, _TARGET_SR, quality="HQ")
    return np.ascontiguousarray(out.T, dtype=np.float32)


def _is_mono(data: np.ndarray) -> bool:
    # AudioBuffer.__post_init__ duplicates mono sources into (2, N), so an exact
    # channel comparison is a reliable mono test, not a heuristic.
    return bool(np.array_equal(data[0], data[1]))


def _to_int16(data: np.ndarray) -> np.ndarray:
    return (np.clip(data, -1.0, 1.0) * 32767.0).round().astype("<i2")


def _pcm_bytes(data: np.ndarray) -> tuple[bytes, int]:
    """Encode (2, N) float32 as .pti PCM; returns (bytes, num_frames).

    Mono is written as a single channel (the device infers channel count from
    data size, so this halves the file for free); stereo is planar, which is
    exactly the memory layout of a C-contiguous (2, N) array.
    """
    pcm = _to_int16(data)
    if _is_mono(data):
        return pcm[0].tobytes(), pcm.shape[1]
    return np.ascontiguousarray(pcm).tobytes(), pcm.shape[1]


def _pack_env(hdr: bytearray, base: int, *, amount: float = 1.0, attack_ms: int = 3000,
              decay_ms: int = 0, sustain: float = 1.0, release_ms: int = 1000,
              automation: tuple[int, int] = _AUTO_OFF) -> None:
    # Defaults (incl. the odd 3000 ms attack) mirror what the device itself writes
    # for untouched automation envelopes, so unused blocks stay byte-identical to
    # a device-saved instrument.
    struct.pack_into("<f", hdr, base, amount)
    struct.pack_into("<H", hdr, base + 6, attack_ms)
    struct.pack_into("<H", hdr, base + 10, decay_ms)
    struct.pack_into("<f", hdr, base + 12, sustain)
    struct.pack_into("<H", hdr, base + 16, release_ms)
    hdr[base + 18], hdr[base + 19] = automation


def _pack_lfo(hdr: bytearray, base: int, *, type_: int = 2, steps: int = 0,
              amount: float = 0.5) -> None:
    struct.pack_into("<BB", hdr, base, type_, steps)
    struct.pack_into("<f", hdr, base + 4, amount)


def _build_header(
    name: str,
    num_frames: int,
    *,
    playback_mode: int,
    loop: tuple[int, int] | None = None,
    tune: int = 0,
    volume_env: tuple[int, int, float, int] = (0, 0, 1.0, 1000),
    slices: list[int] | None = None,
    wavetable_positions: int = 0,
    wavetable_position: int = 0,
    wt_lfo: tuple[int, float] | None = None,
    cutoff: float = 1.0,
    filter_type: tuple[int, int] = (0, 0),
) -> bytes:
    hdr = bytearray(_HEADER_LEN)
    hdr[: len(_HEADER_PREFIX)] = _HEADER_PREFIX

    hdr[20] = 1 if playback_mode == _MODE_WAVETABLE else 0
    hdr[21 : 21 + _NAME_LEN] = name.encode("ascii", errors="replace")[:_NAME_LEN].ljust(_NAME_LEN, b"\0")

    struct.pack_into("<I", hdr, 60, num_frames)
    # The device writes window size 2048 even on non-wavetable instruments.
    struct.pack_into("<H", hdr, 64, _WT_WINDOW)
    if playback_mode == _MODE_WAVETABLE:
        struct.pack_into("<H", hdr, 68, wavetable_positions)

    hdr[76] = playback_mode
    struct.pack_into("<H", hdr, 78, 0)  # playback start
    if loop is not None:
        struct.pack_into("<H", hdr, 80, max(1, _scale16(loop[0], num_frames)))
        struct.pack_into("<H", hdr, 82, min(65534, _scale16(loop[1], num_frames)))
    else:
        struct.pack_into("<HH", hdr, 80, 1, 65534)
    struct.pack_into("<H", hdr, 84, 65535)  # playback end
    struct.pack_into("<H", hdr, 88, wavetable_position)

    attack_ms, decay_ms, sustain, release_ms = volume_env
    _pack_env(hdr, _ENV_OFFSETS[_ENV_VOLUME], attack_ms=attack_ms, decay_ms=decay_ms,
              sustain=sustain, release_ms=release_ms, automation=_AUTO_ENVELOPE)
    for i, base in enumerate(_ENV_OFFSETS[1:], start=1):
        if i == _ENV_WT_POSITION and wt_lfo is not None:
            _pack_env(hdr, base, automation=_AUTO_LFO)
        else:
            _pack_env(hdr, base)
    for i, base in enumerate(_LFO_OFFSETS):
        if i == _ENV_WT_POSITION and wt_lfo is not None:
            _pack_lfo(hdr, base, steps=wt_lfo[0], amount=wt_lfo[1])
        else:
            _pack_lfo(hdr, base)

    struct.pack_into("<ff", hdr, 260, cutoff, 0.0)  # cutoff, resonance
    # Filter is (type, enabled) bytes like automation: type 0 LP / 1 HP / 2 BP.
    hdr[268], hdr[269] = filter_type
    struct.pack_into("<bb", hdr, 270, tune, 0)  # tune, finetune
    hdr[272] = 50  # volume: 50 = 0 dB
    hdr[276] = 50  # panning: centre
    hdr[278] = 0  # delay send

    if slices:
        for i, pos in enumerate(slices[:_MAX_SLICES]):
            struct.pack_into("<H", hdr, 280 + 2 * i, pos)
        hdr[376] = min(len(slices), _MAX_SLICES)
        hdr[377] = 0  # active slice

    struct.pack_into("<HH", hdr, 378, 441, 0)  # granular length (10 ms), position
    hdr[382] = 0  # granular shape: square
    hdr[383] = 0  # granular loop mode: forward
    hdr[384] = 0  # reverb send
    hdr[385] = 0  # overdrive
    hdr[386] = 16  # bit depth

    struct.pack_into("<I", hdr, 388, zlib.crc32(bytes(hdr[:388])))
    return bytes(hdr)


class PolyendExporter:
    @classmethod
    def expected_outputs(cls, output: OutputConfig, path: Path) -> list[Path]:
        sub = subfolder_parts(output.subfolder)
        return [path.joinpath(*sub, f"{safe_component(output.name)}.pti")]

    @classmethod
    def notes_used(cls, notes: Sequence[int]) -> list[int]:
        """The one note of a melodic capture grid a .pti synth preset ships.

        No keyzones (see module docstring), so every other captured note is dead
        weight. The pipeline calls this *before* capture to render only this note
        instead of the whole grid, which is why it has to stay the single
        definition of the pick — `_export_synth` selects through it too, so the
        two can't drift apart.
        """
        lo, hi = min(notes), max(notes)
        centre = min(max(round((lo + hi) / 2) + _ROOT_HIGH_BIAS, lo), hi)
        # Tie (centre exactly between two captures) resolves to the lower note.
        return [min(notes, key=lambda n: (abs(n - centre), n))]

    def export(self, sset: SampleSet, config: OutputConfig, path: Path) -> Path:
        if not sset.samples:
            raise ValueError(f"{config.name}: sample set is empty")

        if sset.category == Category.DRUM:
            header, pcm = self._export_kit(sset, config.name)
        elif sset.category == Category.WAVETABLE:
            header, pcm = self._export_wavetable(sset, config.name)
        else:
            header, pcm = self._export_synth(sset, config.name)

        dest = self.expected_outputs(config, path)[0]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(header + pcm)
        return dest

    # ------------------------------------------------------------------
    def _export_synth(self, sset: SampleSet, name: str) -> tuple[bytes, bytes]:
        # One sample per preset: the root note notes_used() picks, then velocity closest
        # to 100, then lowest round robin. Going through notes_used() rather than
        # recomputing the centre here is what lets the pipeline pre-narrow the capture
        # to that one note and still get this exact sample.
        root = self.notes_used([x.note for x in sset.samples])[0]
        s = min(
            (x for x in sset.samples if x.note == root),
            key=lambda x: (abs(x.velocity - 100), x.round_robin),
        )

        data = _resample_to_target(s.audio.data, s.audio.sample_rate)
        pcm, num_frames = _pcm_bytes(data)
        if num_frames == 0:
            raise ValueError(f"{name}: sample for note {s.note} has no audio")

        loop = s.loop_points
        if loop is not None and s.audio.sample_rate != _TARGET_SR:
            ratio = _TARGET_SR / s.audio.sample_rate
            loop = (
                min(round(loop[0] * ratio), num_frames - 1),
                min(round(loop[1] * ratio), num_frames - 1),
            )

        mode = _MODE_FWD_LOOP if loop is not None else _MODE_ONESHOT
        if loop is not None and _scale16(loop[0], num_frames) >= min(65534, _scale16(loop[1], num_frames)):
            # The header only has 1/65535-of-file position resolution; a loop
            # shorter than that collapses to zero length, which the device
            # can't play. Ship unlooped rather than degenerate.
            log.warning(f"{name}: loop too short for .pti position resolution, exporting one-shot")
            loop, mode = None, _MODE_ONESHOT

        tune = max(-24, min(24, 60 - s.note))
        if tune != 60 - s.note:
            log.warning(f"{name}: root note {s.note} beyond tune range, clamped to {tune:+d} semitones")

        # No attack: the capture already contains the source patch's own attack
        # (an 8 s Zebra swell is 8 s of recorded audio), so re-applying attack_s
        # as a volume-envelope ramp stacks the same swell twice. Envelope stays
        # flat — attack 0, decay 0, full sustain — and only release shapes
        # note-off.
        header = _build_header(
            name,
            num_frames,
            playback_mode=mode,
            loop=loop,
            tune=tune,
            volume_env=(0, 0, 1.0, 1000),
        )
        return header, pcm

    # ------------------------------------------------------------------
    def _export_kit(self, sset: SampleSet, name: str) -> tuple[bytes, bytes]:
        # Same pad selection as the Deluge kit exporter: lowest RR per note, in
        # note order — for drumkits `note` is the canonical kick->snare->hats...
        # index assigned at load time, so essential pads come first.
        by_note: dict[int, list[Sample]] = defaultdict(list)
        for s in sset.samples:
            by_note[s.note].append(s)
        pads = [min(by_note[note], key=lambda s: s.round_robin) for note in sorted(by_note)]
        if len(pads) > _MAX_SLICES:
            log.warning(f"{name}: {len(pads)} pads, keeping the first {_MAX_SLICES}")
            pads = pads[:_MAX_SLICES]

        chunks = [_resample_to_target(p.audio.data, p.audio.sample_rate) for p in pads]
        offsets = []
        pos = 0
        for c in chunks:
            offsets.append(pos)
            pos += c.shape[1]
        data = np.concatenate(chunks, axis=1)
        pcm, num_frames = _pcm_bytes(data)
        if num_frames == 0:
            raise ValueError(f"{name}: kit has no audio")

        # Floor the scaled positions so a marker can never land after its
        # transient (worst case it starts ~num_frames/65535 frames early).
        slices = [min(65535, int(off / num_frames * 65535)) for off in offsets]
        # Beat-slice (mode 5), not plain slice (mode 4): plain Slice mode plays
        # only the *currently selected* slice, repitched per key across the
        # scale (manual §6.9 — "pads play the selected slice melodically").
        # Beat Slice maps each pad to its own slice at original pitch, which is
        # what a multi-drum kit needs.
        header = _build_header(name, num_frames, playback_mode=_MODE_BEAT_SLICE, slices=slices)
        return header, pcm

    # ------------------------------------------------------------------
    def _export_wavetable(self, sset: SampleSet, name: str) -> tuple[bytes, bytes]:
        s = sset.samples[0]
        wt = sset.source_metadata["wavetable"]

        # Wavetables are never resampled: the 2048-frame window grid is the
        # content, the nominal rate is irrelevant.
        #
        # A trailing partial window is dropped rather than rejected — the device
        # itself floors to whole windows, and Polyend's own stock wavetables ship
        # a few samples shy of one (Touchy.pti, a device-saved factory wavetable
        # instrument, has 524266 frames with a window count of 255 = 522240).
        # Only a file too short for even one window is unusable.
        num_frames = s.audio.num_frames
        positions = num_frames // _WT_WINDOW
        if positions == 0:
            raise ValueError(
                f"{name}: wavetable length {num_frames} is shorter than one {_WT_WINDOW}-sample window")
        usable = positions * _WT_WINDOW
        if usable != num_frames:
            log.warning(f"{name}: dropping {num_frames - usable} trailing samples "
                        f"(partial wavetable window), keeping {positions} windows")

        wt_lfo = None
        if wt.lfo2_depth > 0:
            # Mirror of the Deluge lfo2 -> oscAWavetablePosition patch cable.
            # steps indexes the Tracker's slow->fast LFO subdivision enum.
            wt_lfo = (round(wt.lfo2_rate * _LFO_MAX_STEPS), wt.lfo2_depth)

        # Stereo wavetables are kept stereo (Tracker Mini / Tracker+ only): the
        # header's length field counts frames PER CHANNEL, so the window count is
        # unaffected by the channel count, and the PCM is planar exactly like a
        # stereo sample instrument. Polyend's own "Stereo Wavetables" bank has
        # channels as low as -0.32 correlated, so downmixing would not merely
        # narrow them — it would cancel content out. True-mono input still ships
        # as one channel (_pcm_bytes collapses it).
        pcm, _ = _pcm_bytes(s.audio.data[:, :usable])

        header = _build_header(
            name,
            usable,
            playback_mode=_MODE_WAVETABLE,
            wavetable_positions=positions,
            # Assumed to be a window index (not a 0..65535 fraction) — flagged
            # for on-device verification.
            wavetable_position=round(wt.wt_position * max(positions - 1, 0)),
            wt_lfo=wt_lfo,
            # WavetableConfig times are 0..1 fractions; spread linearly over the
            # .pti envelope's 0-10 s range (the _q31 analog on the Deluge side).
            volume_env=(
                round(wt.attack * 10000),
                round(wt.decay * 10000),
                wt.sustain,
                round(wt.release * 10000),
            ),
            cutoff=wt.filter_cutoff,
            filter_type=(0, 1) if wt.filter_cutoff < 0.999 else (0, 0),  # LP, enabled
        )
        return header, pcm

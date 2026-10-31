"""Shared engine for sources whose key mapping is authoritative metadata.

A Bitwig `.multisample`, an SFZ and a Kontakt NKI all say the same things about a
sampled instrument: which WAV plays on which key, at which root note and velocity, and
(for sustaining sounds) where it loops. None of that has to be guessed from filenames
(LibraryAdapter) or detected from the audio (the analysis pipeline). Each format only
differs in how it *finds* that metadata and where the WAV bytes live, so the format
adapters (bitwig.py, mapped.py) subclass `ZoneMapAdapter`, which owns the rest:

  * Velocity layering is dropped — hardware sampler presets play one sample per note, and
    both kit-less exporters collapse to one anyway (see io/exporters/deluge.py
    `_export_multisample`). We keep the single layer whose top velocity is nearest
    `config.velocity`, unless the exporter can ship them (`keep_velocity_layers`).
  * Notes are thinned to `note_step` semitones apart (as LibraryAdapter does).
  * Each WAV is sliced to its authored `[sample_start, sample_stop)` play region.
  * A looped note's loop is baked with the source's own crossfade length and marked as an
    authored loop, so the tuned analysis pipeline ships it verbatim (no re-detection, no
    second crossfade — see analysis/pipeline.py `_analyze_one`).
  * A left/right pair of hard-panned mono zones on the same key (how some vendors store a
    stereo sample in formats with no stereo files) is joined back into one stereo sample.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...analysis.loop import bake_loop_crossfade
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

log = logging.getLogger(__name__)

# A zone panned this far (|pan| as a fraction of full) counts as one side of a split
# stereo pair. Vendors pan these to the extremes (-100 / +100 in SFZ).
_HARD_PAN = 0.99


@dataclass
class Zone:
    """One sample's mapping metadata (no audio read yet)."""

    file: str  # zip member (Bitwig) or filesystem path (SFZ / Kontakt)
    root: int
    vel_low: int
    vel_high: int
    track: bool  # key tracking on → repitched across the zone (normal); off → fixed pitch
    gain_db: float
    sample_start: int
    sample_stop: int  # exclusive; 0 = to the end of the file
    reverse: bool
    loop_start: int | None
    loop_stop: int | None  # as a WAV `smpl` chunk writes it (the loop's last frame)
    loop_fade_s: float
    group: int  # orders round robins within a velocity layer
    loop_fade_frames: int = 0  # for formats that state the crossfade in frames, not seconds
    key_low: int = 0
    key_high: int = 127
    pan: float = 0.0  # -1 (left) .. 1 (right)
    tune_cents: float = 0.0
    partner: Zone | None = None  # the other half of a split stereo pair (see join_stereo_pairs)


def thin_notes(roots: list[int], note_step: int) -> list[int]:
    """Greedy low-to-high thinning to at least note_step semitones apart (as _grouped_notes)."""
    if note_step <= 1:
        return roots
    kept: list[int] = []
    for r in roots:
        if not kept or r - kept[-1] >= note_step:
            kept.append(r)
    return kept


def group_layers(zones: list[Zone]) -> dict[tuple[int, int], list[Zone]]:
    """Group one note's zones by velocity layer (vel_high, vel_low), each list sorted by
    group then filename (the round robins within that layer).
    """
    layers: dict[tuple[int, int], list[Zone]] = {}
    for z in zones:
        layers.setdefault((z.vel_high, z.vel_low), []).append(z)
    return {k: sorted(v, key=lambda z: (z.group, z.file)) for k, v in layers.items()}


def pick_layer(layers: dict[tuple[int, int], list[Zone]], target_velocity: int) -> list[Zone]:
    """Keep only the velocity layer nearest `target_velocity`.

    A layer's representative velocity is its top (`vel_high`) — the loudest hit it covers.
    Ties (two layers with the same top) resolve to the higher `vel_low`, i.e. the more
    specific/louder zone.
    """
    best_key = min(layers, key=lambda k: (abs(k[0] - target_velocity), -k[1]))
    return layers[best_key]


def _decode(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes to float32 (channels, frames) and the sample rate."""
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    return data.T, sr


def _to_stereo(data: np.ndarray) -> np.ndarray:
    if data.shape[0] == 1:
        return np.vstack([data, data])
    if data.shape[0] > 2:
        return data[:2]
    return data


def read_region(raw: bytes, start: int, stop: int) -> AudioBuffer:
    """Decode a WAV from bytes and slice to the [start, stop) play region."""
    data, sr = _decode(raw)
    data = _to_stereo(data)
    n = data.shape[1]
    lo = max(0, min(start, n))
    hi = n if stop <= 0 else max(lo, min(stop, n))
    return AudioBuffer(data=np.ascontiguousarray(data[:, lo:hi]), sample_rate=sr)


def join_stereo_pairs(zones: list[Zone]) -> list[Zone]:
    """Fold each hard-left / hard-right zone pair on the same key and velocity into one.

    Some libraries store a stereo sample as two mono files layered on the same keys, one
    panned fully left and one fully right (the SFZ/Kontakt mappings have no stereo-file
    concept). Left unjoined, the two would read as round robins of one note and the preset
    would ship half of the sound. The left zone keeps its own metadata (loop points
    included) and carries the right one as `partner`; the right zone is dropped from the
    list. A zone with no matching partner is left alone.
    """
    lefts: dict[tuple, list[Zone]] = {}
    rights: dict[tuple, list[Zone]] = {}
    for z in zones:
        key = (z.root, z.key_low, z.key_high, z.vel_low, z.vel_high)
        if z.pan <= -_HARD_PAN:
            lefts.setdefault(key, []).append(z)
        elif z.pan >= _HARD_PAN:
            rights.setdefault(key, []).append(z)

    dropped: set[int] = set()
    for key, ls in lefts.items():
        rs = rights.get(key, [])
        for left, right in zip(sorted(ls, key=lambda z: (z.group, z.file)), sorted(rs, key=lambda z: (z.group, z.file))):
            left.partner = right
            left.pan = 0.0
            dropped.add(id(right))
    return [z for z in zones if id(z) not in dropped]


class ZoneMapAdapter:
    """Base adapter. Subclasses provide `_parse_zones` and `_audio_reader`."""

    # True when each zone's `file` is a WAV on disk, so a sample that reaches an exporter
    # untouched can be copied byte-for-byte (see io/exporters/_common.py:write_sample_wav)
    # rather than re-encoded — a vendor's WAV is a finished master. A zip member can't be.
    files_on_disk = False

    def __init__(self, config):
        self._config = config
        self._zones: list[Zone] | None = None

    # ---- what a format supplies -------------------------------------------------
    def _parse_zones(self) -> list[Zone]:
        raise NotImplementedError

    def _audio_reader(self):
        """A context manager yielding a `read(zone) -> bytes` callable (opened once per capture)."""
        raise NotImplementedError

    def _default_name(self) -> str:
        return Path(str(self._config.path)).stem

    # ---- shared -----------------------------------------------------------------
    def _load_zones(self) -> list[Zone]:
        if self._zones is None:
            zones = join_stereo_pairs(self._parse_zones())
            if self.files_on_disk and zones:
                # A level every zone shares (Kontakt's default -6 dB instrument volume, a
                # mixer setting) is not part of the sound: the sampler has its own level
                # control, and shipping the vendor's WAV untouched keeps it a byte copy.
                # Only the balance between zones is kept.
                ref = max(z.gain_db for z in zones)
                for z in zones:
                    for part in (z, z.partner):
                        if part is not None:
                            part.gain_db = 0.0 if abs(part.gain_db - ref) < 0.01 else part.gain_db - ref
            self._zones = zones
        return self._zones

    def _selection(self, max_round_robins: int, note_step: int, keep_velocity_layers: bool = False) -> list[Zone]:
        """Metadata-only: the zones capture() will turn into samples.

        Notes thinned by note_step, round robins within a layer capped at
        max_round_robins. By default, one velocity layer per note (nearest
        config.velocity) — every hardware format except Bento's SampInst plays one
        sample per note regardless of what it's handed, so there's no point capturing
        more. `keep_velocity_layers=True` keeps every layer instead, for a build that
        can actually ship them (see io/exporters/bento.py `keeps_velocity_layers`).
        """
        by_root: dict[int, list[Zone]] = {}
        for z in self._load_zones():
            by_root.setdefault(z.root, []).append(z)

        selected: list[Zone] = []
        for root in thin_notes(sorted(by_root), note_step):
            layers = group_layers(by_root[root])
            kept = layers.values() if keep_velocity_layers else [pick_layer(layers, self._config.velocity)]
            for layer in kept:
                selected.extend(layer[:max_round_robins])
        return selected

    def expected_count(self, max_round_robins: int = 1, note_step: int = 1, keep_velocity_layers: bool = False) -> int:
        return len(self._selection(max_round_robins, note_step, keep_velocity_layers))

    def capture(
        self,
        name: str | None = None,
        max_round_robins: int = 1,
        note_step: int = 1,
        progress=None,
        keep_velocity_layers: bool = False,
    ) -> SampleSet:
        path = self._config.path
        selection = self._selection(max_round_robins, note_step, keep_velocity_layers)

        # Round-robin index per (root, vel_high) so each velocity layer of a note is
        # numbered independently, and several kept RRs of one layer stay distinct in the
        # (note, velocity, round_robin) key the exporters use.
        rr_counter: dict[tuple[int, int], int] = {}
        samples: list[Sample] = []
        with self._audio_reader() as read:
            for zone in selection:
                audio = self._read_zone(zone, read)

                metadata: dict = {"source_file": zone.file}
                if not zone.track:
                    metadata["fixed_pitch"] = True
                if zone.tune_cents:
                    metadata["tune_cents"] = zone.tune_cents
                if zone.partner is not None:
                    metadata["stereo_pair"] = zone.partner.file
                if zone.key_low <= zone.key_high and not zone.key_low <= zone.root <= zone.key_high:
                    # Authored to play away from its own pitch (a mislabelled file, a root
                    # nudged to cover a gap): the exporters key on the root, so keep the
                    # keys the author mapped it to.
                    metadata["key_range"] = (zone.key_low, zone.key_high)

                decoded = audio
                audio, loop_points = self._resolve_loop(audio, zone, metadata)
                if self.files_on_disk and self._is_whole_file(zone) and audio is decoded:
                    metadata["audio_verbatim"] = True

                rr_key = (zone.root, zone.vel_high)
                rr = rr_counter.get(rr_key, 0) + 1
                rr_counter[rr_key] = rr
                samples.append(
                    Sample(
                        note=zone.root,
                        velocity=min(127, max(1, zone.vel_high)),
                        round_robin=rr,
                        audio=audio,
                        loop_points=loop_points,
                        metadata=metadata,
                    )
                )
                if progress is not None:
                    progress.update(1)

        return SampleSet(
            name=name or self._default_name(),
            category=Category.SYNTH,
            samples=samples,
            source_metadata={"path": str(path)},
        )

    @staticmethod
    def _is_whole_file(zone: Zone) -> bool:
        """Does the zone play its file as-is — no slice, level change or stereo join?"""
        return zone.sample_start == 0 and zone.sample_stop == 0 and not zone.gain_db and zone.partner is None

    @staticmethod
    def _gain(audio: AudioBuffer, gain_db: float) -> AudioBuffer:
        if not gain_db:
            return audio
        return AudioBuffer(data=audio.data * (10.0 ** (gain_db / 20.0)), sample_rate=audio.sample_rate)

    def _read_zone(self, zone: Zone, read: Callable[[Zone], bytes]) -> AudioBuffer:
        audio = self._gain(read_region(read(zone), zone.sample_start, zone.sample_stop), zone.gain_db)
        if zone.partner is None:
            return audio
        # Left zone carries the loop and the region; the right half is cut to the same span.
        other = zone.partner
        right = self._gain(read_region(read(other), zone.sample_start, zone.sample_stop), other.gain_db)
        n = min(audio.num_frames, right.num_frames)
        data = np.vstack([audio.data[0, :n], right.data[0, :n]])
        return AudioBuffer(data=np.ascontiguousarray(data), sample_rate=audio.sample_rate)

    def _resolve_loop(self, audio: AudioBuffer, zone: Zone, metadata: dict) -> tuple[AudioBuffer, tuple[int, int] | None]:
        """Rebase, validate and crossfade a zone's authored loop against the sliced audio.

        Loop points reference the raw WAV; the play region started at `sample_start`, so
        shift by that. Bake the source's own crossfade length as a backward loop crossfade
        (the sliced region ends at the loop end, so there is no release tail to blend —
        only the seam crossfade), then flag the points as an authored loop so the
        pipeline ships them verbatim. Returns the (possibly baked) buffer and the loop
        points, or the untouched buffer and None for a one-shot / invalid loop.
        """
        if zone.loop_start is None or zone.loop_stop is None:
            return audio, None
        ls = zone.loop_start - zone.sample_start
        le = zone.loop_stop - zone.sample_start
        n = audio.num_frames
        ls = max(0, min(ls, n))
        le = max(ls, min(le, n))
        if le - ls < 2:
            return audio, None

        fade_ms = zone.loop_fade_s * 1000.0
        if zone.loop_fade_frames:
            fade_ms = max(fade_ms, zone.loop_fade_frames * 1000.0 / audio.sample_rate)
        # A vendor WAV on disk ships untouched (its authored loop is already in the file or
        # the mapping); only a zip member sliced out for us gets the source's fade baked in.
        if fade_ms >= 1.0 and not self.files_on_disk:
            audio = bake_loop_crossfade(audio, ls, le, fade_ms)
        metadata["authored_loop"] = True
        return audio, (ls, le)

"""Adapter for Bitwig Studio `.multisample` archives.

A `.multisample` is a zip holding the sample WAVs plus a `multisample.xml` that maps
each WAV to a key zone, a velocity zone and (optionally) a sustain loop. Everything the
other library sources have to *guess* — the root note, whether a note loops, where the
loop is — is authoritative metadata here, so this adapter reads it straight from the XML
rather than parsing filenames (LibraryAdapter) or detecting loops (the analysis
pipeline). See docs/inputs/bitwig.md.

What the adapter does per note, to turn a full DAW multisample into a playable hardware
preset:
  * Velocity layering is dropped — hardware sampler presets play one sample per note, and
    both exporters collapse to one anyway (see io/exporters/deluge.py `_export_multisample`).
    We keep the single layer whose top velocity is nearest `config.velocity`.
  * Notes are thinned to `note_step` semitones apart (as LibraryAdapter does).
  * Each WAV is sliced to its authored `[sample-start, sample-stop)` play region — for a
    looped note that region ends exactly at the loop end (Bitwig stores extra recording
    past it that it never plays), so slicing keeps the export tight.
  * A looped note's loop is baked with Bitwig's own crossfade `fade` length and marked as
    an authored loop, so the tuned analysis pipeline ships it verbatim (no re-detection,
    no second crossfade — see analysis/pipeline.py `_analyze_one`).
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ...analysis.loop import bake_loop_crossfade
from ...config.schema import BitwigSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

log = logging.getLogger(__name__)


@dataclass
class _Zone:
    """One `<sample>` element's parsed metadata (no audio read yet)."""

    file: str
    root: int
    vel_low: int
    vel_high: int
    track: bool  # key <track> != 0 → repitched across the zone (normal); 0 → fixed pitch
    gain_db: float
    sample_start: int
    sample_stop: int
    reverse: bool
    loop_start: int | None
    loop_stop: int | None
    loop_fade_s: float
    group: int


def _multisample_xml_name(names: list[str]) -> str | None:
    return next((n for n in names if n.lower().endswith("multisample.xml")), None)


def _f2i(s: str | None, default: int = 0) -> int:
    """Parse Bitwig's float-formatted integers ('178489.000') to int."""
    if s is None:
        return default
    return int(round(float(s)))


def parse_multisample_xml(xml_bytes: bytes) -> list[_Zone]:
    """Parse a `multisample.xml` document into a flat list of zones."""
    root = ET.fromstring(xml_bytes)
    zones: list[_Zone] = []
    for s in root.findall("sample"):
        key = s.find("key")
        if key is None or key.get("root") is None:
            continue  # a zone with no root note can't be mapped
        vel = s.find("velocity")
        vlow = _f2i(vel.get("low"), 0) if vel is not None else 0
        vhigh = _f2i(vel.get("high"), 127) if vel is not None else 127

        loop = s.find("loop")
        loop_mode = loop.get("mode") if loop is not None else "off"
        if loop is not None and loop_mode and loop_mode != "off":
            lstart = _f2i(loop.get("start"))
            lstop = _f2i(loop.get("stop"))
            lfade = float(loop.get("fade", "0") or "0")
        else:
            lstart = lstop = None
            lfade = 0.0

        zones.append(
            _Zone(
                file=s.get("file", ""),
                root=_f2i(key.get("root")),
                vel_low=vlow,
                vel_high=vhigh,
                track=float(key.get("track", "1") or "1") != 0.0,
                gain_db=float(s.get("gain", "0") or "0"),
                sample_start=_f2i(s.get("sample-start"), 0),
                sample_stop=_f2i(s.get("sample-stop"), 0),
                reverse=(s.get("reverse", "false") == "true"),
                loop_start=lstart,
                loop_stop=lstop,
                loop_fade_s=lfade,
                group=_f2i(s.get("group"), 0),
            )
        )
    return zones


def _thin_notes(roots: list[int], note_step: int) -> list[int]:
    """Greedy low-to-high thinning to at least note_step semitones apart (as _grouped_notes)."""
    if note_step <= 1:
        return roots
    kept: list[int] = []
    for r in roots:
        if not kept or r - kept[-1] >= note_step:
            kept.append(r)
    return kept


def _pick_layer(zones: list[_Zone], target_velocity: int) -> list[_Zone]:
    """From one note's zones, keep only the velocity layer nearest `target_velocity`.

    A layer's representative velocity is its top (`vel_high`) — the loudest hit it covers.
    Ties (two layers with the same top) resolve to the higher `vel_low`, i.e. the more
    specific/louder zone. All zones of the winning layer are returned (they're the
    round robins for that layer, ordered by group then filename).
    """
    layers: dict[tuple[int, int], list[_Zone]] = {}
    for z in zones:
        layers.setdefault((z.vel_high, z.vel_low), []).append(z)
    best_key = min(layers, key=lambda k: (abs(k[0] - target_velocity), -k[1]))
    return sorted(layers[best_key], key=lambda z: (z.group, z.file))


def _read_region(raw: bytes, start: int, stop: int) -> AudioBuffer:
    """Decode a WAV from bytes and slice to the [start, stop) play region."""
    import soundfile as sf

    data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    data = data.T  # (channels, frames)
    if data.shape[0] == 1:
        data = np.vstack([data, data])
    elif data.shape[0] > 2:
        data = data[:2]
    n = data.shape[1]
    lo = max(0, min(start, n))
    hi = n if stop <= 0 else max(lo, min(stop, n))
    return AudioBuffer(data=np.ascontiguousarray(data[:, lo:hi]), sample_rate=sr)


class BitwigAdapter:
    def __init__(self, config: BitwigSourceConfig):
        self._config = config
        self._zones: list[_Zone] | None = None

    def _load_zones(self) -> list[_Zone]:
        if self._zones is None:
            with zipfile.ZipFile(self._config.path) as z:
                xml_name = _multisample_xml_name(z.namelist())
                if xml_name is None:
                    raise ValueError(f"No multisample.xml in {self._config.path}")
                self._zones = parse_multisample_xml(z.read(xml_name))
        return self._zones

    def _selection(self, max_round_robins: int, note_step: int) -> list[_Zone]:
        """Metadata-only: the zones capture() will turn into samples.

        One velocity layer per note (nearest config.velocity), notes thinned by
        note_step, round robins within the layer capped at max_round_robins.
        """
        by_root: dict[int, list[_Zone]] = {}
        for z in self._load_zones():
            by_root.setdefault(z.root, []).append(z)

        selected: list[_Zone] = []
        for root in _thin_notes(sorted(by_root), note_step):
            layer = _pick_layer(by_root[root], self._config.velocity)
            selected.extend(layer[:max_round_robins])
        return selected

    def expected_count(self, max_round_robins: int = 1, note_step: int = 1) -> int:
        return len(self._selection(max_round_robins, note_step))

    def capture(
        self, name: str | None = None, max_round_robins: int = 1, note_step: int = 1, progress=None
    ) -> SampleSet:
        path = self._config.path
        selection = self._selection(max_round_robins, note_step)

        # Round-robin index per (root) so several kept RRs of the same note stay distinct
        # in the (note, velocity, round_robin) key the exporters use.
        rr_counter: dict[int, int] = {}
        samples: list[Sample] = []
        with zipfile.ZipFile(path) as z:
            for zone in selection:
                raw = z.read(zone.file)
                audio = _read_region(raw, zone.sample_start, zone.sample_stop)
                if zone.gain_db:
                    audio = AudioBuffer(
                        data=audio.data * (10.0 ** (zone.gain_db / 20.0)),
                        sample_rate=audio.sample_rate,
                    )

                metadata = {"source_file": zone.file}
                if not zone.track:
                    metadata["fixed_pitch"] = True

                audio, loop_points = self._resolve_loop(audio, zone, metadata)

                rr = rr_counter.get(zone.root, 0) + 1
                rr_counter[zone.root] = rr
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
            name=name or path.stem,
            category=Category.SYNTH,
            samples=samples,
            source_metadata={"path": str(path)},
        )

    def _resolve_loop(
        self, audio: AudioBuffer, zone: _Zone, metadata: dict
    ) -> tuple[AudioBuffer, tuple[int, int] | None]:
        """Rebase, validate and crossfade a zone's authored loop against the sliced audio.

        Bitwig loop points reference the raw WAV; the play region started at
        `sample_start`, so shift by that. Bake Bitwig's own `fade` length as a backward
        loop crossfade (the sliced region ends at the loop end, so there is no release
        tail to blend — only the seam crossfade), then flag the points as an authored loop
        so the pipeline ships them verbatim. Returns the (possibly baked) buffer and the
        loop points, or the untouched buffer and None for a one-shot / invalid loop.
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
        if fade_ms >= 1.0:
            audio = bake_loop_crossfade(audio, ls, le, fade_ms)
        metadata["authored_loop"] = True
        return audio, (ls, le)

"""Adapter for Bitwig Studio `.multisample` archives.

A `.multisample` is a zip holding the sample WAVs plus a `multisample.xml` that maps
each WAV to a key zone, a velocity zone and (optionally) a sustain loop. Everything the
other library sources have to *guess* — the root note, whether a note loops, where the
loop is — is authoritative metadata here, so this adapter reads it straight from the XML
rather than parsing filenames (LibraryAdapter) or detecting loops (the analysis
pipeline). See docs/inputs/bitwig.md. The per-note selection, region slicing and loop
baking are shared with the SFZ / Kontakt sources — see zonemap.py.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager

from ...config.schema import BitwigSourceConfig
from .zonemap import Zone, ZoneMapAdapter

log = logging.getLogger(__name__)

# Kept under its old name: runner/scan.py imports it.
_Zone = Zone


def _multisample_xml_name(names: list[str]) -> str | None:
    return next((n for n in names if n.lower().endswith("multisample.xml")), None)


def _f2i(s: str | None, default: int = 0) -> int:
    """Parse Bitwig's float-formatted integers ('178489.000') to int."""
    if s is None:
        return default
    return int(round(float(s)))


def parse_multisample_xml(xml_bytes: bytes) -> list[Zone]:
    """Parse a `multisample.xml` document into a flat list of zones."""
    root = ET.fromstring(xml_bytes)
    zones: list[Zone] = []
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
            Zone(
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


class BitwigAdapter(ZoneMapAdapter):
    def __init__(self, config: BitwigSourceConfig):
        super().__init__(config)

    def _parse_zones(self) -> list[Zone]:
        with zipfile.ZipFile(self._config.path) as z:
            xml_name = _multisample_xml_name(z.namelist())
            if xml_name is None:
                raise ValueError(f"No multisample.xml in {self._config.path}")
            return parse_multisample_xml(z.read(xml_name))

    @contextmanager
    def _audio_reader(self):
        with zipfile.ZipFile(self._config.path) as z:
            yield lambda zone: z.read(zone.file)

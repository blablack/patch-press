"""Adapter for SFZ and Kontakt NKI instruments.

Both formats are a small mapping file next to the WAVs it names, so — unlike a Bitwig
`.multisample` — the audio is read from disk. The mapping (root note, velocity zone, loop)
is authoritative; see zonemap.py for what the adapter does with it and io/sfz.py and
io/kontakt.py for how each format is read. See docs/inputs/sfz-kontakt.md.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from ...config.schema import MappedSourceConfig
from ..kontakt import parse_nki
from ..sfz import parse_sfz
from .zonemap import Zone, ZoneMapAdapter

_PARSERS = {"sfz": parse_sfz, "kontakt": parse_nki}


class MappedAdapter(ZoneMapAdapter):
    files_on_disk = True

    def __init__(self, config: MappedSourceConfig):
        super().__init__(config)

    def _parse_zones(self) -> list[Zone]:
        try:
            parse = _PARSERS[self._config.format]
        except KeyError:
            raise ValueError(f"Unknown mapped source format: {self._config.format!r}") from None
        return parse(self._config.path)

    @contextmanager
    def _audio_reader(self):
        yield lambda zone: Path(zone.file).read_bytes()

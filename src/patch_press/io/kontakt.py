"""Reader for Native Instruments Kontakt instruments (`.nki`): the key mapping and loops.

Derived from the NKI format knowledge in ConvertWithMoss (https://github.com/git-moss/
ConvertWithMoss, (c) 2019-2026 Jürgen Moßgraber, LGPL-3.0): the header magic numbers, the
zlib-compressed XML program of Kontakt 2-4.1 files and the `file_ex2` sample-path encoding.
This module is a Python re-implementation of only the part patch-press needs — zones,
loops and sample paths, nothing about modulation or effects — so it is distributed under
the same LGPL-3.0 terms for that part.

Kontakt 5+ instruments (an NI container, see `kontakt5.py`) are read too.

Supported: Kontakt 2 through 4.1 instruments (`.nki` = a binary header, then a zlib stream
holding an XML `K2_Container`). NOT supported, and refused with `UnsupportedNki` so a scan can
say so and move on: Kontakt 1 files, the 4.2 FastLZ chunk format, NI-protected (encrypted)
instruments, NKM multis and monoliths with embedded samples.
"""

from __future__ import annotations

import logging
import os
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

from . import kontakt5
from .adapters.zonemap import Zone

log = logging.getLogger(__name__)

_MAGIC_K2 = {0x1290A87F, 0x7FA89012}  # instrument, little / big endian
_MAGIC_K1 = {0x5EE56EB3, 0xB36EE55E}
_HEADER_K42 = {0x1A6337EA, 0xEA37631A}
_ZLIB_HEADERS = (b"\x78\x01", b"\x78\x5e", b"\x78\x9c", b"\x78\xda")
_PERCENT_RE = re.compile(r"%([0-9A-Fa-f]{2})")


class UnsupportedNki(Exception):
    """The file is a Kontakt format this reader does not decode (yet)."""


def _program_xml(data: bytes) -> bytes:
    if len(data) < 16:
        raise UnsupportedNki("too short to be an NKI")
    magic = struct.unpack("<I", data[:4])[0]
    if data[12:16] == b"hsin":
        raise UnsupportedNki("Kontakt 5+ instruments go through _parse_k5")
    if magic in _MAGIC_K1:
        raise UnsupportedNki("Kontakt 1 instrument")
    if magic not in _MAGIC_K2:
        raise UnsupportedNki(f"unknown NKI magic {magic:#010x}")
    if struct.unpack("<I", data[8:12])[0] in _HEADER_K42 or struct.unpack(">I", data[8:12])[0] in _HEADER_K42:
        raise UnsupportedNki("Kontakt 4.2 (FastLZ) instrument")

    # The XML starts right after the fixed-size header; find it by its zlib header rather
    # than trusting a version-specific offset, and insist the stream really is XML.
    for pos in range(16, min(len(data) - 2, 1024)):
        if data[pos : pos + 2] in _ZLIB_HEADERS:
            try:
                xml = zlib.decompressobj().decompress(data[pos:])
            except zlib.error:
                continue
            if xml.lstrip()[:5] == b"<?xml":
                return xml
    raise UnsupportedNki("no XML program found (monolith or an unknown layout)")


def _values(element: ET.Element | None) -> dict[str, str]:
    """The `<V name=".." value=".."/>` children of an element as a dict."""
    if element is None:
        return {}
    return {v.get("name", ""): v.get("value", "") for v in element.findall("V")}


def _read_length_prefixed(s: str, i: int) -> tuple[str, int]:
    n = int(s[i : i + 3])
    return s[i + 3 : i + 3 + n], i + 3 + n


def decode_sample_path(encoded: str) -> tuple[str, bool]:
    """Decode a `file_ex2` value to (path, absolute).

    A plain value is a path as-is. An `@`-prefixed value is a token stream: `b` = one
    folder up, `d` + 3-digit length + name = a folder, `v` = a volume/drive (absolute),
    and `F`/`f` (+ 11/6 digits of file bookkeeping) introduces the file name itself.
    """
    if not encoded.startswith("@"):
        return encoded.replace("\\", "/"), False
    path, absolute, i = "", False, 1
    while i < len(encoded):
        tok = encoded[i]
        i += 1
        if tok == "b":
            path += "../"
        elif tok in "dm":
            name, i = _read_length_prefixed(encoded, i)
            path += name + "/"
        elif tok == "v":
            name, i = _read_length_prefixed(encoded, i)
            path += ("" if ":" in name else "/") + name + "/"
            absolute = True
        elif tok in "Ff":
            i += 11 if tok == "F" else 6
            name = _PERCENT_RE.sub(lambda m: chr(int(m.group(1), 16)), path + encoded[i:])
            return name, absolute or name.startswith("/")
        else:
            raise ValueError(f"bad NKI sample path {encoded!r}")
    raise ValueError(f"bad NKI sample path {encoded!r}")


def _find_sample(nki: Path, encoded: str, cache: dict[str, dict[str, Path]]) -> Path | None:
    """Locate the WAV an NKI points at, relative to the NKI's own folder."""
    path, absolute = decode_sample_path(encoded)
    return _locate(nki, path, absolute, cache)


def _locate(nki: Path, path: str, absolute: bool, cache: dict[str, dict[str, Path]]) -> Path | None:
    parent = nki.parent
    if not absolute:
        direct = _ci_join(parent, path)
        if direct is not None:
            return direct
    # An absolute path from the author's machine (or a relative one that no longer lines
    # up): try ever-shorter tails of it under the NKI's folder, then its parent.
    parts = [p for p in path.split("/") if p not in ("", "..", ".")]
    for base in (parent, parent.parent):
        for k in range(len(parts)):
            hit = _ci_join(base, "/".join(parts[k:]))
            if hit is not None:
                return hit
    name = parts[-1].lower() if parts else ""
    if not name:
        return None
    # Last resort: the file by name anywhere beside the instrument's folder. Indexed once per
    # root (a library of hundreds of zones would otherwise walk the tree for each), following
    # symlinks because a staged pack links its WAV tree in.
    root = parent.parent if parent.parent != parent else parent
    index = cache.setdefault(str(root), {})
    if not index:
        for dirpath, _, files in os.walk(root, followlinks=True):
            for f in files:
                index.setdefault(f.lower(), Path(dirpath) / f)
    return index.get(name)


def _ci_join(base: Path, rel: str) -> Path | None:
    cur = base
    for part in [p for p in rel.split("/") if p not in ("", ".")]:
        if part == "..":
            cur = cur.parent
            continue
        nxt = cur / part
        if not nxt.exists():
            try:
                nxt = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
            except OSError:
                return None
            if nxt is None:
                return None
        cur = nxt
    return cur if cur.is_file() else None


def parse_nki(path: Path) -> list[Zone]:
    """The instrument's zones, with `file` set to the resolved absolute WAV path.

    Groups that only fire on key release are skipped (they are release noise, not the
    sustain sound), as are zones whose sample cannot be found on disk.
    """
    path = Path(path)
    data = path.read_bytes()
    if data[12:16] == b"hsin":
        return _parse_k5(path, data)
    xml = _program_xml(data)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        root = ET.fromstring(xml.decode("latin-1").encode("utf-8").replace(b'encoding="ISO-8859-1"', b""))

    cache: dict[str, dict[str, Path]] = {}
    zones: list[Zone] = []
    for program in root.iter("K2_Program"):
        prog = _values(program.find("Parameters"))
        groups = {int(g.get("index", "0")): g for g in program.iterfind("Groups/K2_Group")}
        group_params = {i: _values(g.find("Parameters")) for i, g in groups.items()}

        for zi, zone in enumerate(program.iterfind("Zones/K2_Zone")):
            gi = int(zone.get("groupIdx", "0"))
            gp = group_params.get(gi, {})
            if gp.get("releaseTrigger") == "yes":
                continue
            zp = _values(zone.find("Parameters"))
            sample = _values(zone.find("Sample"))
            encoded = sample.get("file_ex2") or sample.get("file")
            if not encoded:
                continue
            wav = _find_sample(path, encoded, cache)
            if wav is None:
                log.warning("%s: sample not found: %s", path.name, decode_sample_path(encoded)[0])
                continue

            # Loops: Kontakt gives a start and a *length*, and the crossfade in frames.
            loop_start = loop_stop = None
            fade_frames = 0
            for loop in zone.iterfind("Loops/Loop"):
                lp = _values(loop)
                if lp.get("mode") in ("until_end", "until_release"):
                    loop_start = int(lp["loopStart"])
                    loop_stop = loop_start + int(lp["loopLength"]) - 1  # inclusive last frame, as in a `smpl` chunk
                    fade_frames = int(float(lp.get("xfadeLength", "0")))
                    break

            volume = float(zp.get("zoneVolume", "1")) * float(gp.get("volume", "1")) * float(prog.get("volume", "1"))
            tune = float(zp.get("zoneTune", "1")) * float(gp.get("tune", "1")) * float(prog.get("tune", "1"))
            start, end = int(zp.get("sampleStart", "0")), int(zp.get("sampleEnd", "0"))

            zones.append(
                Zone(
                    file=str(wav),
                    root=int(zp["rootKey"]),
                    vel_low=int(zp.get("lowVelocity", "1")),
                    vel_high=int(zp.get("highVelocity", "127")),
                    track=gp.get("keyTracking", "yes") != "no",
                    gain_db=20.0 * _log10(volume),
                    sample_start=start if end > start else 0,
                    sample_stop=end if end > start else 0,
                    reverse=gp.get("reverse") == "yes",
                    loop_start=loop_start,
                    loop_stop=loop_stop,
                    loop_fade_s=0.0,
                    loop_fade_frames=fade_frames,
                    group=gi * 1000 + zi,
                    key_low=int(zp.get("lowKey", "0")),
                    key_high=int(zp.get("highKey", "127")),
                    pan=_pan(zp.get("zonePan", "0"), gp.get("pan", "0"), prog.get("pan", "0")),
                    tune_cents=1200.0 * _log2(tune) if tune > 0 else 0.0,
                )
            )
    return zones


def _log10(x: float) -> float:
    import math

    return math.log10(x) if x > 0 else -120.0


def _log2(x: float) -> float:
    import math

    return math.log2(x)


def _pan(*values: str) -> float:
    """Kontakt 2 stores pan as -1..1 for each of zone, group and program; they add."""
    return max(-1.0, min(1.0, sum(float(v) for v in values)))


_ABS_RE = re.compile(r"^(?:/|[A-Za-z]:/)")


def _parse_k5(path: Path, data: bytes) -> list[Zone]:
    """Zones of a Kontakt 5+ instrument; same skip rules as the Kontakt 2-4.1 reader."""
    try:
        programs = kontakt5.read_programs(data)
    except kontakt5.Unsupported as exc:
        raise UnsupportedNki(str(exc)) from exc
    except (struct.error, IndexError, ValueError) as exc:
        raise UnsupportedNki(f"unreadable Kontakt 5+ container ({exc})") from exc

    cache: dict[str, dict[str, Path]] = {}
    zones: list[Zone] = []
    for prog in programs:
        solo = prog.group_solo and any(g.soloed for g in prog.groups)
        for zi, kz in enumerate(prog.zones):
            if not 0 <= kz.group < len(prog.groups) or not 0 <= kz.file_id < len(prog.files):
                continue
            g = prog.groups[kz.group]
            # Release-trigger groups are noise, not the sustain sound; a muted group does not
            # sound, and with any group soloed only the soloed ones do.
            if g.release_trigger or (not g.soloed if solo else g.muted):
                continue
            raw = prog.files[kz.file_id]
            wav = _locate(path, raw, bool(_ABS_RE.match(raw)), cache)
            if wav is None:
                log.warning("%s: sample not found: %s", path.name, raw)
                continue

            loop_start = loop_stop = None
            fade_frames = 0
            for loop in kz.loops:
                if loop.mode in (1, 2):  # until end / until release
                    loop_start = loop.start
                    loop_stop = loop.start + loop.length - 1  # inclusive last frame, as in a `smpl` chunk
                    fade_frames = loop.xfade
                    break

            tune = kz.tune * g.tune * prog.tune
            # `sample_end` counts the frames cut from the end of the file
            stop = kz.num_frames - kz.sample_end if kz.sample_end > 0 and kz.num_frames > kz.sample_end else 0
            zones.append(
                Zone(
                    file=str(wav),
                    root=kz.root,
                    vel_low=kz.low_vel,
                    vel_high=kz.high_vel,
                    track=g.key_tracking,
                    gain_db=20.0 * _log10(kz.volume * g.volume * prog.volume),
                    sample_start=kz.sample_start,
                    sample_stop=stop,
                    reverse=g.reverse,
                    loop_start=loop_start,
                    loop_stop=loop_stop,
                    loop_fade_s=0.0,
                    loop_fade_frames=fade_frames,
                    group=kz.group * 1000 + zi,
                    key_low=kz.low_key,
                    key_high=kz.high_key,
                    pan=max(-1.0, min(1.0, kz.pan + g.pan + prog.pan)),
                    tune_cents=1200.0 * _log2(tune) if tune > 0 else 0.0,
                )
            )
    return zones

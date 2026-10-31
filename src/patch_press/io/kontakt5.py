"""Reader for the Kontakt 5+ instrument container (`.nki` with `hsin` at offset 12).

Derived from the format knowledge in ConvertWithMoss (https://github.com/git-moss/
ConvertWithMoss, (c) 2019-2026 Jürgen Moßgraber, LGPL-3.0): the Native Instruments
"NI container" item tree, its FastLZ-compressed sub trees and the Kontakt preset chunk
structure inside (programs, groups, zones, loops, file list). Only what patch-press needs is
decoded -- key/velocity mapping, root, gain/pan/tune, loops and sample paths; nothing of
modulation, effects or scripts. Distributed under the same LGPL-3.0 terms for that part.

`read_programs()` returns plain `Program` objects; turning them into `Zone`s (and finding the
WAVs on disk) is `kontakt.py`'s job, shared with the Kontakt 2-4.1 reader.

Not supported, raised as `Unsupported`: encrypted sub trees (NI's own protected libraries),
NKM banks/multis and the Kontakt 4.2 container.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

_TERMINATOR = 1
_AUTHORING_APPLICATION = 101
_PRESET_CHUNK_ITEM = 109
_SUB_TREE_ITEM = 115
_KONTAKT_APP_ID = 2

_ID_BANK = 0x03
_ID_PROGRAM = 0x28
_ID_PROGRAM_CONTAINER = 0x29
_ID_GROUP_LIST = 0x33
_ID_ZONE_LIST = 0x34
_ID_LOOP_ARRAY = 0x39
_ID_FILENAME_LIST = 0x3D
_ID_FILENAME_LIST_EX = 0x4B

_MAX_VERSION = 0x9C  # newest zone/group layout CWM has seen


class Unsupported(Exception):
    """A Kontakt 5+ file this reader cannot decode."""


class _Buf:
    """Little-endian cursor over bytes."""

    def __init__(self, data: bytes, pos: int = 0):
        self.d = data
        self.p = pos

    def left(self) -> int:
        return len(self.d) - self.p

    def take(self, n: int) -> bytes:
        if n < 0 or self.p + n > len(self.d):
            raise Unsupported("truncated data")
        out = self.d[self.p : self.p + n]
        self.p += n
        return out

    def skip(self, n: int) -> None:
        self.take(n)

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def i16(self) -> int:
        return struct.unpack("<h", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def u64(self) -> int:
        return struct.unpack("<Q", self.take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def utf16(self) -> str:
        return self.take(self.u32() * 2).decode("utf-16-le", errors="replace")

    def block64(self) -> bytes:
        """A block whose leading u64 counts itself."""
        size = self.u64()
        if size < 8 or size > self.left() + 8:
            raise Unsupported("bad block size")
        return self.take(size - 8)


# --- FastLZ -----------------------------------------------------------------------------


def fastlz_decompress(data: bytes, out_size: int) -> bytes:
    """FastLZ level 1/2 decompression (the level is in the top 3 bits of the first byte)."""
    if not data:
        raise Unsupported("empty FastLZ block")
    level = (data[0] >> 5) + 1
    if level not in (1, 2):
        raise Unsupported("not FastLZ data")
    out = bytearray()
    ip = 1
    ctrl = data[0] & 31
    n = len(data)
    loop = True
    while loop:
        ln = ctrl >> 5
        ofs = (ctrl & 31) << 8
        if ctrl >= 32:
            ln -= 1
            ref = len(out) - ofs
            if ln == 6:
                if level == 1:
                    ln += data[ip]
                    ip += 1
                else:
                    while True:
                        code = data[ip]
                        ip += 1
                        ln += code
                        if code != 255:
                            break
            if level == 1:
                ref -= data[ip]
                ip += 1
            else:
                code = data[ip]
                ip += 1
                ref -= code
                if code == 255 and ofs == 31 << 8:
                    ofs = (data[ip] << 8) + data[ip + 1]
                    ip += 2
                    ref = len(out) - ofs - 8191
            if len(out) + ln + 3 > out_size:
                raise Unsupported("FastLZ output overrun")
            if ref - 1 < 0:
                raise Unsupported("FastLZ bad back-reference")
            if ip < n:
                ctrl = data[ip]
                ip += 1
            else:
                loop = False
            ref -= 1
            for _ in range(ln + 3):  # overlapping copies are intended (run-length)
                out.append(out[ref])
                ref += 1
        else:
            ctrl += 1
            if len(out) + ctrl > out_size or ip + ctrl > n:
                raise Unsupported("FastLZ unsound literal run")
            out += data[ip : ip + ctrl]
            ip += ctrl
            loop = ip < n
            if loop:
                ctrl = data[ip]
                ip += 1
    return bytes(out)


# --- NI container ------------------------------------------------------------------------


@dataclass
class _Chunk:
    type_id: int
    next: _Chunk | None = None
    raw: bytes = b""  # the chunk's own data, undecoded
    sub: _Item | None = None  # decoded sub tree item
    encrypted: bool = False


@dataclass
class _Item:
    chunk: _Chunk
    children: list[_Item] = field(default_factory=list)


def _read_chunk(buf: _Buf) -> _Chunk:
    body = _Buf(buf.block64())
    body.skip(4)  # domain id
    type_id = body.u32()
    if body.u32() != 1:
        raise Unsupported("unknown chunk version")
    nxt = _read_chunk(body) if type_id != _TERMINATOR else None
    chunk = _Chunk(type_id, nxt, body.take(body.left()))
    if type_id == _SUB_TREE_ITEM:
        _decode_subtree(chunk)
    return chunk


def _decode_subtree(chunk: _Chunk) -> None:
    b = _Buf(chunk.raw)
    if b.u32() != 1:
        raise Unsupported("unknown sub tree version")
    if b.u8() > 0:
        usize, csize = b.u32(), b.u32()
        try:
            data = fastlz_decompress(b.take(csize), usize)
        except (Unsupported, IndexError):
            chunk.encrypted = True  # NI-protected content: opaque to us
            return
        chunk.sub = _read_item(_Buf(data))
    else:
        chunk.sub = _read_item(b)


def _read_item(buf: _Buf) -> _Item:
    b = _Buf(buf.block64())
    if b.u32() != 1:
        raise Unsupported("unknown item header version")
    if b.take(4) != b"hsin":
        raise Unsupported("not an NI container")
    b.skip(8 + 16)  # two unknown u32, the uuid
    chunk = _read_chunk(b)
    b.skip(4)  # item version
    item = _Item(chunk)
    for _ in range(b.u32()):
        b.skip(4 + 4 + 4)  # child index, domain, chunk type
        item.children.append(_read_item(b))
    return item


def _chunks(item: _Item):
    c: _Chunk | None = item.chunk
    while c is not None:
        yield c
        c = c.next


def _find(item: _Item, type_id: int) -> _Chunk | None:
    for c in _chunks(item):
        if c.type_id == type_id:
            return c
        if c.sub is not None:
            hit = _find(c.sub, type_id)
            if hit:
                return hit
    for child in item.children:
        hit = _find(child, type_id)
        if hit:
            return hit
    return None


def _has_encrypted(item: _Item) -> bool:
    return any(c.encrypted or (c.sub is not None and _has_encrypted(c.sub)) for c in _chunks(item)) or any(
        _has_encrypted(ch) for ch in item.children
    )


# --- Kontakt preset chunks ---------------------------------------------------------------


@dataclass
class _Pc:
    """A preset chunk: id, version, public data and (structured) children."""

    id: int
    version: int = -1
    public: bytes = b""
    children: list[_Pc] = field(default_factory=list)


def _structure(b: _Buf, pc: _Pc) -> None:
    if b.u8() == 0:  # plain data
        pc.public = b.take(b.left())
        return
    pc.version = b.u16()
    b.skip(b.u32())  # private data
    pc.public = b.take(b.u32())
    kids = _Buf(b.take(b.u32()))
    while kids.left() > 0:
        pc.children.append(_read_pc(kids))


def _read_pc(b: _Buf) -> _Pc:
    cid = b.u16()
    data = _Buf(b.take(b.u32()))
    pc = _Pc(cid)
    if cid in (_ID_GROUP_LIST, _ID_ZONE_LIST):
        has_ref = cid == _ID_ZONE_LIST
        for _ in range(data.u32()):
            child = _Pc(data.u32() if has_ref else _ID_GROUP)
            _structure(data, child)
            pc.children.append(child)
    elif cid in (_ID_BANK, _ID_PROGRAM_CONTAINER, _ID_PROGRAM):
        _structure(data, pc)
    else:
        pc.public = data.take(data.left())
    return pc


_ID_GROUP = 0x04


def _all(chunks: list[_Pc], cid: int) -> list[_Pc]:
    out: list[_Pc] = []
    for c in chunks:
        if c.id == cid:
            out.append(c)
        else:
            out += _all(c.children, cid)
    return out


# --- file list ---------------------------------------------------------------------------


def _read_file(b: _Buf) -> str:
    parts: list[str] = []
    for _ in range(b.u32()):
        seg = b.u8()
        if seg == 0:
            drive = b.take(2).decode("ascii", "replace").strip()
            parts.append(f"{drive}:/" if drive else "/")
        elif seg == 1:
            drive = b.utf16()
            parts.append(f"{drive}:/" if drive else "/")
        elif seg == 2:
            parts.append(b.utf16() + "/")
        elif seg == 3:
            parts.append("../")
        elif seg in (4, 8, 9):
            parts.append(b.utf16())
        elif seg == 6:
            pass
        else:
            raise Unsupported(f"file list segment {seg}")
    return "".join(parts)


def _read_files(b: _Buf) -> list[str]:
    if b.left() <= 0:
        return []
    return [_read_file(b) for _ in range(b.i32())]


def _read_files_v2(b: _Buf) -> list[str]:
    files: list[str] = []
    if b.left() <= 0:
        return files
    size = b.i32()
    b.skip(8)
    for _ in range(size - 1):
        files.append(_read_file(b))
        if b.left() > 0:
            b.skip(4 + 4 + 20)  # padding, timestamp, padding
    return files


def _file_list(pc: _Pc) -> list[str]:
    b = _Buf(pc.public)
    if pc.id == _ID_FILENAME_LIST_EX:
        version = b.u16()
        if version not in (2, 3):
            raise Unsupported(f"file list version {version}")
        if version == 3:
            return _read_files_v2(b)
    _read_files(b)  # "special" files
    return _read_files(b)


# --- programs ----------------------------------------------------------------------------


@dataclass
class Loop:
    mode: int  # 1 until end, 2 until release, 3 one-shot
    start: int
    length: int
    xfade: int


@dataclass
class KZone:
    group: int
    low_key: int
    high_key: int
    low_vel: int
    high_vel: int
    root: int
    volume: float
    pan: float
    tune: float
    sample_start: int
    sample_end: int  # frames trimmed from the end
    num_frames: int
    file_id: int
    loops: list[Loop] = field(default_factory=list)


@dataclass
class KGroup:
    name: str
    volume: float
    pan: float
    tune: float
    key_tracking: bool
    reverse: bool
    release_trigger: bool
    muted: bool
    soloed: bool


@dataclass
class Program:
    name: str
    volume: float
    pan: float
    tune: float
    group_solo: bool
    groups: list[KGroup]
    zones: list[KZone]
    files: list[str]


def _loops(data: bytes) -> list[Loop]:
    if len(data) < 2:
        return []
    b = _Buf(data)
    enabled = b.u16()
    loops = []
    for i in range(8):
        if not enabled & (1 << i):
            continue
        if b.u16() != 0x60:
            raise Unsupported("unknown loop layout")
        mode, start, length = b.i32(), b.u32(), b.u32()
        b.skip(4 + 1 + 4)  # count, alternating, tuning
        xfade = b.u32()
        if b.left() > 0:
            b.skip(1)
        loops.append(Loop(mode, start, length, xfade))
    return loops


def _zone(pc: _Pc) -> KZone | None:
    if pc.version > _MAX_VERSION:
        raise Unsupported(f"zone version {pc.version:#x}")
    b = _Buf(pc.public)
    start, end = b.u32(), b.u32()
    b.skip(4)
    lo_vel, hi_vel, lo_key, hi_key = b.u16(), b.u16(), b.u16(), b.u16()
    b.skip(8)  # the four crossfade ranges
    root = b.u16()
    vol, pan, tune = b.f32(), b.f32(), b.f32()
    if pc.version >= 0x9A:
        b.skip(1 + 1 + 4)
        if b.left() == 0:  # a script-only instrument: no sample
            return None
    file_id = b.u32()
    if file_id >= 0x80000000:
        return None
    b.skip(4 + 4 + 1)  # resolution, rate, channels
    frames = b.u32()
    zone = KZone(pc.id, lo_key, hi_key, lo_vel, hi_vel, root, vol, pan, tune, start, end, frames, file_id)
    for child in pc.children:
        if child.id == _ID_LOOP_ARRAY:
            zone.loops = _loops(child.public)
    return zone


def _group(pc: _Pc) -> KGroup:
    if pc.version > _MAX_VERSION:
        raise Unsupported(f"group version {pc.version:#x}")
    b = _Buf(pc.public)
    name = b.utf16()
    vol, pan, tune = b.f32(), b.f32(), b.f32()
    key_tracking, reverse, release = b.u8() > 0, b.u8() > 0, b.u8() > 0
    b.skip(1 + 4 + 2 + 4 + 4)  # mono release, counter, midi channel, voice group, fx split
    muted, soloed = b.u8() > 0, b.u8() > 0
    return KGroup(name, vol, pan, tune, key_tracking, reverse, release, muted, soloed)


def _program(pc: _Pc, files: list[str]) -> Program:
    b = _Buf(pc.public)
    name = b.utf16()
    b.skip(8 + 1)  # size of all samples, midi transpose
    vol, pan, tune = b.f32(), b.f32(), b.f32()
    b.skip(4 + 2 + 4 + 4 + 4 + 4)  # clip range, key switch, preload, library id, fingerprint, flags
    group_solo = b.u8() > 0
    groups: list[KGroup] = []
    zones: list[KZone] = []
    for child in pc.children:
        if child.id == _ID_GROUP_LIST:
            groups = [_group(g) for g in child.children]
        elif child.id == _ID_ZONE_LIST:
            zones = [z for z in map(_zone, child.children) if z is not None]
    return Program(name, vol, pan, tune, group_solo, groups, zones, files)


def read_programs(data: bytes) -> list[Program]:
    """The programs of a Kontakt 5+ `.nki`, with raw sample paths in `Program.files`."""
    item = _read_item(_Buf(data))
    app = _find(item, _AUTHORING_APPLICATION)
    if app is not None:
        b = _Buf(app.raw)
        b.skip(4 + 1)
        if b.u32() != _KONTAKT_APP_ID:
            raise Unsupported("not a Kontakt file")
    preset = _find(item, _PRESET_CHUNK_ITEM)
    if preset is None:
        raise Unsupported("encrypted (NI-protected) instrument" if _has_encrypted(item) else "no preset data")
    b = _Buf(preset.raw)
    b.skip(4 + 4)  # version, dictionary type
    if b.u32() != 1:
        raise Unsupported("preset dictionary with several entries")
    size = b.u32()
    b.skip(4)
    payload = _Buf(b.take(size))

    top: list[_Pc] = []
    while payload.left() > 0:
        top.append(_read_pc(payload))
    programs = _all(top, _ID_PROGRAM)
    if not programs:
        raise Unsupported("no program (NKM bank/multi)")
    list_pc = next((c for c in top if c.id in (_ID_FILENAME_LIST, _ID_FILENAME_LIST_EX)), None)
    files = _file_list(list_pc) if list_pc else []
    return [_program(p, files) for p in programs]

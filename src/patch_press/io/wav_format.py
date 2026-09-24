"""Make a WAV file one a hardware sampler will actually load.

A library WAV usually goes onto the card byte-for-byte (see
exporters/_common.py:write_sample_wav), which means the device gets whatever the
vendor's encoder wrote. The Deluge firmware is strict about that
(`audio_file.cpp`, `AudioFile::loadFile`), and fails anything else with
FILE_UNSUPPORTED:

- `fmt ` format tag 1 (PCM) or 3 (IEEE float, 32-bit only). WAVE_FORMAT_EXTENSIBLE
  (0xFFFE) is refused even when its sub-format is plain PCM, so a perfectly
  ordinary 24-bit file fails purely on how its header is spelled;
- 8, 16, 24 or 32 bits per sample;
- 1 or 2 channels;
- 5,000 to 96,000 Hz.

`conform()` fixes a file with the least change that gets it loaded. A file that
already passes is copied untouched. One whose only fault is an EXTENSIBLE header
around audio the device can read gets its `fmt ` chunk rewritten as plain PCM or
float: the audio data and every other chunk (smpl loops, clm, cue) stay
byte-identical. Anything else is decoded and re-encoded, and only then does the
audio change: more than 2 channels are folded to stereo, and a sample rate outside
the range is halved or doubled until it fits, so the ratio stays exact and loop
points scale cleanly (the caller gets the ratio back to do that). A transcode
keeps the bit depth where the device supports it, and drops the source's
metadata chunks.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


@dataclass(frozen=True)
class WavRules:
    """What one device accepts in a WAV file."""

    pcm_bits: frozenset[int]
    float_bits: frozenset[int]
    max_channels: int
    min_rate: int
    max_rate: int


# audio_file.cpp: see the module docstring.
DELUGE = WavRules(
    pcm_bits=frozenset({8, 16, 24, 32}),
    float_bits=frozenset({32}),
    max_channels=2,
    min_rate=5000,
    max_rate=96000,
)


@dataclass(frozen=True)
class WavFormat:
    """The `fmt ` chunk of a WAV file, and where it sits."""

    tag: int  # as written; see `codec` for what the audio actually is
    codec: int  # the EXTENSIBLE sub-format when tag is EXTENSIBLE, else tag
    channels: int
    rate: int
    bits: int  # container size, which is what the device reads samples as
    fmt_offset: int  # of the chunk header
    fmt_size: int  # payload size, unpadded


def read_format(path: Path) -> WavFormat | None:
    """Parse the `fmt ` chunk, reading only headers. None if this isn't a RIFF/WAVE
    file with one."""
    with open(path, "rb") as f:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            return None
        while True:
            off = f.tell()
            chunk = f.read(8)
            if len(chunk) < 8:
                return None
            cid, size = chunk[:4], struct.unpack("<I", chunk[4:])[0]
            if cid == b"fmt ":
                payload = f.read(size)
                if len(payload) < 16:
                    return None
                tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", payload[:16])
                codec = tag
                if tag == WAVE_FORMAT_EXTENSIBLE and len(payload) >= 26:
                    # cbSize(2) validBits(2) channelMask(4), then the sub-format GUID
                    # whose first two bytes are the plain format tag.
                    codec = struct.unpack("<H", payload[24:26])[0]
                return WavFormat(tag, codec, channels, rate, bits, off, size)
            f.seek(size + (size & 1), os.SEEK_CUR)


def _codec_ok(fmt: WavFormat, rules: WavRules) -> bool:
    if fmt.codec == WAVE_FORMAT_PCM:
        return fmt.bits in rules.pcm_bits
    if fmt.codec == WAVE_FORMAT_FLOAT:
        return fmt.bits in rules.float_bits
    return False


def problems(path: Path, rules: WavRules = DELUGE) -> list[str]:
    """Why the device would refuse this file; empty if it wouldn't."""
    fmt = read_format(path)
    if fmt is None:
        return ["not a RIFF/WAVE file with a fmt chunk"]
    found = []
    if fmt.tag == WAVE_FORMAT_EXTENSIBLE:
        found.append("WAVE_FORMAT_EXTENSIBLE header")
    if not _codec_ok(fmt, rules):
        found.append(f"format {fmt.codec:#06x} at {fmt.bits} bits")
    if not 1 <= fmt.channels <= rules.max_channels:
        found.append(f"{fmt.channels} channels")
    if not rules.min_rate <= fmt.rate <= rules.max_rate:
        found.append(f"{fmt.rate} Hz")
    return found


def _plain_fmt(fmt: WavFormat) -> bytes:
    block = fmt.channels * fmt.bits // 8
    payload = struct.pack("<HHIIHH", fmt.codec, fmt.channels, fmt.rate, fmt.rate * block, block, fmt.bits)
    if fmt.codec == WAVE_FORMAT_FLOAT:
        payload += b"\0\0"  # cbSize: a non-PCM fmt chunk carries one, even if empty
    return b"fmt " + struct.pack("<I", len(payload)) + payload


def _rewrite_header(src: Path, dest: Path, fmt: WavFormat) -> None:
    raw = bytearray(src.read_bytes())
    end = fmt.fmt_offset + 8 + fmt.fmt_size + (fmt.fmt_size & 1)
    new = _plain_fmt(fmt)
    raw[fmt.fmt_offset:end] = new
    raw[4:8] = struct.pack("<I", struct.unpack("<I", raw[4:8])[0] + len(new) - (end - fmt.fmt_offset))
    _atomic_write(dest, bytes(raw), src.stat().st_mode & 0o7777)


def _atomic_write(dest: Path, data: bytes, mode: int) -> None:
    """Write via a sibling temp file so `dest` may be the source being read. The temp
    file is created 0600; `mode` restores the source's own permissions."""
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    os.close(fd)
    try:
        Path(tmp).write_bytes(data)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _fitting_rate(rate: int, rules: WavRules) -> int:
    while rate > rules.max_rate:
        rate //= 2
    while rate < rules.min_rate:
        rate *= 2
    return rate


def _transcode(src: Path, dest: Path, fmt: WavFormat | None, rules: WavRules) -> float:
    import soxr

    info = sf.info(str(src))
    data, rate = sf.read(str(src), dtype="float64", always_2d=True)
    if data.shape[1] > rules.max_channels:
        # Fold to what the device can take: even channels left, odd right (or all to one).
        if rules.max_channels >= 2:
            data = np.stack([data[:, 0::2].mean(axis=1), data[:, 1::2].mean(axis=1)], axis=1)
        else:
            data = data.mean(axis=1, keepdims=True)
    new_rate = _fitting_rate(rate, rules)
    if new_rate != rate:
        data = soxr.resample(data, rate, new_rate, quality="VHQ")
    if fmt is not None and fmt.codec == WAVE_FORMAT_FLOAT and rules.float_bits:
        subtype = "FLOAT"
    elif fmt is not None and fmt.codec == WAVE_FORMAT_PCM and fmt.bits in rules.pcm_bits - {8}:
        subtype = f"PCM_{fmt.bits}"
    elif info.subtype in ("PCM_16", "PCM_U8", "PCM_S8", "ULAW", "ALAW", "IMA_ADPCM", "MS_ADPCM", "GSM610"):
        subtype = "PCM_16"
    else:
        subtype = "PCM_24"
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".wav")
    os.close(fd)
    try:
        sf.write(tmp, data, new_rate, subtype=subtype, format="WAV")
        os.chmod(tmp, src.stat().st_mode & 0o7777)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return new_rate / rate


def conform(src: Path, dest: Path, rules: WavRules = DELUGE) -> float:
    """Put `src` at `dest` in a form the device loads (see the module docstring).
    `dest` may be `src` itself, to fix a file in place. Returns the sample-rate ratio
    applied (new / old): 1.0 unless the file had to be resampled, in which case
    frame positions into it (loop points) must be scaled by it."""
    src, dest = Path(src), Path(dest)
    found = problems(src, rules)
    fmt = read_format(src)
    if not found:
        if src != dest:
            shutil.copy2(src, dest)
        return 1.0
    if fmt is not None and found == ["WAVE_FORMAT_EXTENSIBLE header"]:
        _rewrite_header(src, dest, fmt)
        log.info("%s: EXTENSIBLE header rewritten as plain %s", dest.name, "float" if fmt.codec == WAVE_FORMAT_FLOAT else "PCM")
        return 1.0
    ratio = _transcode(src, dest, fmt, rules)
    log.warning("%s: transcoded for the device (%s)", dest.name, ", ".join(found))
    return ratio

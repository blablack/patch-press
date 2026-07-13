#!/usr/bin/env python3
"""Validate Polyend Tracker .pti files — the check half of the write/verify pair.

Deliberately does NOT import the writer (patch_press/io/exporters/polyend.py):
the field table below was re-derived from the jaap3/pti-file-format spec and
Polyend's tracker-lib so a shared bug can't validate itself. Reads only the
stdlib.

Usage:

    tools/inspect_pti.py file.pti [more.pti ...]
    tools/inspect_pti.py output/Polyend/            # recurses into dirs

Prints one report per file, and exits non-zero if any file violates the format.
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

HEADER_LEN = 392
MAX_SLICES = 48

PLAYBACK_MODES = {
    0: "one-shot",
    1: "forward loop",
    2: "backward loop",
    3: "pingpong loop",
    4: "slice",
    5: "beat slice",
    6: "wavetable",
    7: "granular",
}
LOOP_MODES = {1, 2, 3}
SLICE_MODES = {4, 5}

# The six 20-byte automation envelope blocks and their 8-byte LFO blocks, in
# header order. Automation is two bytes at env +18/+19: (type 0=env 1=LFO,
# enabled 0/1) — the spec's "00/01/11" is one digit per byte, not hex (confirmed
# against device-saved files). max_steps: the volume LFO has a 24-entry steps
# enum (0-23); the other five have 29 entries (0-28).
ENV_BLOCKS = [
    # (label, env offset, lfo offset, lfo max steps)
    ("volume", 92, 212, 23),
    ("panning", 112, 220, 28),
    ("cutoff", 132, 228, 28),
    ("wavetable position", 152, 236, 28),
    ("granular position", 172, 244, 28),
    ("finetune", 192, 252, 28),
]
LFO_TYPES = {0: "rev saw", 1: "saw", 2: "triangle", 3: "square", 4: "random"}
FILTER_TYPES = {0: "low-pass", 1: "high-pass", 2: "band-pass"}
WT_WINDOWS = {32, 64, 128, 256, 512, 1024, 2048}
BIT_DEPTHS = {4, 8, 16}      # the bit-depth effect setting, not the PCM encoding
MAX_RESONANCE = 4.31         # device range is 0..~4.3


def u8(b: bytes, off: int) -> int:
    return b[off]


def i8(b: bytes, off: int) -> int:
    return struct.unpack_from("<b", b, off)[0]


def u16(b: bytes, off: int) -> int:
    return struct.unpack_from("<H", b, off)[0]


def u32(b: bytes, off: int) -> int:
    return struct.unpack_from("<I", b, off)[0]


def f32(b: bytes, off: int) -> float:
    return struct.unpack_from("<f", b, off)[0]


def pos_to_frame(pos: int, frames: int) -> int:
    """Invert the writer's uint16 fraction: round(frame / frames * 65535)."""
    return round(pos / 65535 * frames)


def inspect(path: Path) -> tuple[list[str], list[str]]:
    """Return (report lines, violations) for one file."""
    report: list[str] = []
    bad: list[str] = []

    raw = path.read_bytes()
    if len(raw) < HEADER_LEN:
        return report, [f"file is {len(raw)} bytes; smaller than the {HEADER_LEN}-byte header"]
    h = raw[:HEADER_LEN]

    if h[0:2] != b"TI":
        bad.append(f"bad magic {h[0:2]!r} (expected b'TI')")

    stored_crc = u32(h, 388)
    real_crc = zlib.crc32(h[0:388])
    if stored_crc != real_crc:
        bad.append(f"CRC mismatch: header says 0x{stored_crc:08x}, crc32(bytes 0..387) is 0x{real_crc:08x}")

    name_bytes = h[21:52].split(b"\x00", 1)[0]
    try:
        name = name_bytes.decode("ascii")
    except UnicodeDecodeError:
        name = name_bytes.decode("ascii", "replace")
        bad.append("instrument name contains non-ASCII bytes")
    if any(c < 0x20 or c > 0x7E for c in name_bytes):
        bad.append("instrument name contains non-printable characters")

    frames = u32(h, 60)
    pcm_bytes = len(raw) - HEADER_LEN
    if frames == 0:
        # A device-saved instrument with no sample loaded is legal.
        channels = "none"
        if pcm_bytes:
            bad.append(f"sample length is 0 frames but file has {pcm_bytes} PCM bytes")
    elif pcm_bytes == frames * 2:
        channels = "mono"
    elif pcm_bytes == frames * 4:
        channels = "stereo"
    else:
        channels = "?"
        bad.append(f"PCM size {pcm_bytes} bytes matches neither mono ({frames * 2}) "
                   f"nor stereo ({frames * 4}) for {frames} frames")

    mode = u8(h, 76)
    mode_name = PLAYBACK_MODES.get(mode)
    if mode_name is None:
        bad.append(f"playback mode {mode} out of range 0-7")
        mode_name = f"unknown ({mode})"

    play_start = u16(h, 78)
    loop_start = u16(h, 80)
    loop_end = u16(h, 82)
    play_end = u16(h, 84)
    if play_start > play_end:
        bad.append(f"playback start {play_start} > playback end {play_end}")
    if mode in LOOP_MODES:
        if loop_start < 1:
            bad.append(f"loop start {loop_start} < 1")
        if loop_end > 65534:
            bad.append(f"loop end {loop_end} > 65534")
        if loop_start >= loop_end:
            bad.append(f"loop start {loop_start} >= loop end {loop_end}")
        if not (play_start <= loop_start and loop_end <= play_end):
            bad.append(f"loop [{loop_start}, {loop_end}] outside playback [{play_start}, {play_end}]")

    slice_count = u8(h, 376)
    slices = [u16(h, 280 + 2 * i) for i in range(min(slice_count, MAX_SLICES))]
    if slice_count > MAX_SLICES:
        bad.append(f"slice count {slice_count} > {MAX_SLICES}")
    if any(b < a for a, b in zip(slices, slices[1:])):
        bad.append(f"slice positions not sorted: {slices}")

    is_wt = u8(h, 20)
    wt_window = u16(h, 64)
    wt_positions = u16(h, 68)
    wt_position = u16(h, 88)
    if is_wt or mode == 6:
        if not is_wt:
            bad.append("wavetable playback mode but is_wavetable flag is 0")
        if mode != 6:
            bad.append(f"is_wavetable set but playback mode is {mode_name}, not wavetable")
        if wt_window not in WT_WINDOWS:
            bad.append(f"wavetable window {wt_window} not one of {sorted(WT_WINDOWS)}")
        # The device floors: a sample that isn't a whole number of windows keeps
        # its full length but exposes only the complete windows.
        if wt_window and wt_positions != frames // wt_window:
            bad.append(f"{wt_positions} positions != {frames} frames // {wt_window} window "
                       f"(= {frames // wt_window})")
        if wt_positions and wt_position >= wt_positions:
            bad.append(f"wavetable position {wt_position} >= total positions {wt_positions}")

    env_summary: list[str] = []
    for label, off, lfo_off, max_steps in ENV_BLOCKS:
        amount = f32(h, off)
        attack = u16(h, off + 6)
        decay = u16(h, off + 10)
        sustain = f32(h, off + 12)
        release = u16(h, off + 16)
        auto_type = u8(h, off + 18)
        auto_on = u8(h, off + 19)
        lfo_type = u8(h, lfo_off)
        lfo_steps = u8(h, lfo_off + 1)
        lfo_amount = f32(h, lfo_off + 4)
        if not 0.0 <= amount <= 1.0:
            bad.append(f"{label} automation amount {amount} outside 0..1")
        if not 0.0 <= sustain <= 1.0:
            bad.append(f"{label} envelope sustain {sustain} outside 0..1")
        for what, ms in (("attack", attack), ("decay", decay), ("release", release)):
            if ms > 10000:
                bad.append(f"{label} envelope {what} {ms} ms > 10000")
        if auto_type > 1:
            bad.append(f"{label} automation type byte {auto_type} not 0 (envelope) or 1 (LFO)")
        if auto_on > 1:
            bad.append(f"{label} automation enabled byte {auto_on} not 0/1")
        if lfo_type not in LFO_TYPES:
            bad.append(f"{label} LFO type {lfo_type} not 0-4")
        if lfo_steps > max_steps:
            bad.append(f"{label} LFO steps {lfo_steps} > {max_steps}")
        if not 0.0 <= lfo_amount <= 1.0:
            bad.append(f"{label} LFO amount {lfo_amount} outside 0..1")
        if auto_on:
            if auto_type == 0:
                env_summary.append(f"{label}: envelope A{attack} D{decay} S{sustain:.2f} R{release}")
            else:
                env_summary.append(
                    f"{label}: LFO {LFO_TYPES.get(lfo_type, lfo_type)} steps#{lfo_steps} amount {lfo_amount:.2f}"
                )

    cutoff = f32(h, 260)
    resonance = f32(h, 264)
    filter_type = u8(h, 268)
    filter_on = u8(h, 269)
    if not 0.0 <= cutoff <= 1.0:
        bad.append(f"filter cutoff {cutoff} outside 0..1")
    if not 0.0 <= resonance <= MAX_RESONANCE:
        bad.append(f"filter resonance {resonance} outside 0..{MAX_RESONANCE}")
    if filter_type not in FILTER_TYPES:
        bad.append(f"filter type {filter_type} not 0-2")
    if filter_on > 1:
        bad.append(f"filter enabled byte {filter_on} not 0/1")

    tune = i8(h, 270)
    finetune = i8(h, 271)
    volume = u8(h, 272)
    pan = u8(h, 276)
    if not -24 <= tune <= 24:
        bad.append(f"tune {tune} outside -24..24 semitones")
    if not -100 <= finetune <= 100:
        bad.append(f"finetune {finetune} outside -100..100")
    if volume > 100:
        bad.append(f"volume {volume} > 100")
    if pan > 100:
        bad.append(f"pan {pan} > 100")

    bit_depth = u8(h, 386)
    if bit_depth not in BIT_DEPTHS:
        bad.append(f"bit depth {bit_depth} not one of {sorted(BIT_DEPTHS)}")

    secs = frames / 44100 if frames else 0.0
    report.append(f"name      {name!r}")
    report.append(f"audio     {frames} frames ({secs:.2f} s @ 44100), {channels}, {bit_depth}-bit")
    report.append(f"playback  {mode_name}  (start {play_start}, end {play_end})")
    if mode in LOOP_MODES:
        report.append(f"loop      {loop_start}..{loop_end} of 65535 "
                      f"(frames ~{pos_to_frame(loop_start, frames)}..{pos_to_frame(loop_end, frames)})")
    if slice_count:
        frames_at = [pos_to_frame(p, frames) for p in slices]
        report.append(f"slices    {slice_count}: {frames_at}")
    if is_wt:
        report.append(f"wavetable {wt_positions} windows x {wt_window}, position {wt_position}")
    report.append(f"levels    volume {volume}, pan {pan}, tune {tune:+d} st, finetune {finetune:+d}")
    if filter_on:
        report.append(f"filter    {FILTER_TYPES.get(filter_type, filter_type)}, "
                      f"cutoff {cutoff:.3f}, resonance {resonance:.3f}")
    if env_summary:
        report.append("autom.    " + "; ".join(env_summary))
    return report, bad


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    files: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if p.is_dir():
            files += sorted(p.rglob("*.pti"))
        else:
            files.append(p)
    if not files:
        print("no .pti files found", file=sys.stderr)
        return 2

    failures = 0
    for f in files:
        try:
            report, bad = inspect(f)
        except OSError as exc:
            report, bad = [], [str(exc)]
        status = "FAIL" if bad else "OK"
        print(f"{status}  {f}")
        for line in report:
            print(f"      {line}")
        for v in bad:
            print(f"  !!  {v}")
        failures += bool(bad)
    if failures:
        print(f"\n{failures}/{len(files)} file(s) failed validation")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

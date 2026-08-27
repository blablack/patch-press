"""Read author-baked loop points from a WAV file's RIFF `smpl` chunk.

Sample libraries (e.g. Samples From Mars) ship the same WAVs across Ableton/Kontakt/
Logic/FL, so any intended loop must live in the WAV itself — the `smpl` chunk — rather
than in a DAW-specific patch file. Those points are ground truth: preferring them over
our own loop detection is both more faithful to the author and avoids re-deriving what
is already known. Frames are indices into the file's samples at its native rate, which
match `AudioBuffer.from_file` (soundfile reads without resampling).
"""

import struct
from pathlib import Path


def read_loop_points(path: Path | str) -> tuple[int, int] | None:
    """Return the first sample loop (start, end) from the WAV's `smpl` chunk, or None.

    None means the file is not a parseable WAV, has no `smpl` chunk, or declares zero
    loops — i.e. the author shipped it as a one-shot. Only the first loop is returned;
    multisample instruments use a single sustain loop per note.
    """
    # Seek the chunk table rather than reading the file: `smpl` is a 60-byte chunk and
    # the audio next to it can be megabytes, and this runs once per sample of every
    # library preset (plus once per probe in runner/scan.py:_library_ships_authored_loops).
    try:
        with open(path, "rb") as f:
            header = f.read(12)
            if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            f.seek(0, 2)
            n = f.tell()
            pos = 12
            while pos + 8 <= n:
                f.seek(pos)
                head = f.read(8)
                if len(head) < 8:
                    return None
                chunk_id = head[:4]
                (chunk_size,) = struct.unpack("<I", head[4:])
                body = pos + 8
                if chunk_id == b"smpl":
                    # smpl header: 9 uint32 (36 bytes); num_loops is the 8th (offset 28).
                    # Each loop record is 24 bytes; start/end are at record offsets 8 and 12.
                    chunk = f.read(60)
                    if len(chunk) < 60:
                        return None
                    (num_loops,) = struct.unpack("<I", chunk[28:32])
                    if num_loops == 0:
                        return None
                    start, end = struct.unpack("<II", chunk[44:52])
                    if end <= start:
                        return None
                    return start, end
                pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned
    except OSError:
        return None
    return None

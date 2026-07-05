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
    try:
        b = Path(path).read_bytes()
    except OSError:
        return None
    if len(b) < 12 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None

    pos = 12
    n = len(b)
    while pos + 8 <= n:
        chunk_id = b[pos : pos + 4]
        (chunk_size,) = struct.unpack("<I", b[pos + 4 : pos + 8])
        body = pos + 8
        if chunk_id == b"smpl":
            # smpl header: 9 uint32 (36 bytes); num_loops is the 8th (offset 28).
            # Each loop record is 24 bytes; start/end are at record offsets 8 and 12.
            if body + 36 > n:
                return None
            (num_loops,) = struct.unpack("<I", b[body + 28 : body + 32])
            if num_loops == 0:
                return None
            rec = body + 36
            if rec + 24 > n:
                return None
            start, end = struct.unpack("<II", b[rec + 8 : rec + 16])
            if end <= start:
                return None
            return start, end
        pos = body + chunk_size + (chunk_size & 1)  # chunks are word-aligned
    return None

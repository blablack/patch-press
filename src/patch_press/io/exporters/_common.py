"""Path and WAV helpers shared by all exporters."""

import logging
import shutil
from pathlib import Path

import soundfile as sf

from ...model.sample import Sample

log = logging.getLogger(__name__)

# Serum's cycle length, and the one both target devices assume: neither the Deluge nor
# the Bento reads a window size out of the file (the Bento's own factory tables carry
# no `clm` chunk at all), so a wavetable only lines up if its windows are 2048 frames.
_WT_WINDOW = 2048


def safe_component(name: str) -> str:
    """One path component with the SD-card path separators neutralised."""
    return name.strip().replace("/", "_").replace("\\", "_")


def subfolder_parts(subfolder: str) -> list[str]:
    """Sanitised components of an output subfolder tree (see OutputConfig.subfolder).

    Drops '', '.', '..' so a config-supplied subfolder can never climb out of the
    collection directory.
    """
    parts = []
    for comp in (subfolder or "").replace("\\", "/").split("/"):
        c = safe_component(comp)
        if c and c not in (".", ".."):
            parts.append(c)
    return parts


def wav_frame_count(path: Path) -> int:
    """Frame count of a WAV an exporter just wrote.

    Read through soundfile rather than the stdlib `wave` module: a WAV copied verbatim
    from a sample library (see write_sample_wav) is whatever the vendor's encoder
    produced, which can be WAVE_FORMAT_EXTENSIBLE — `wave` refuses those outright
    ("unknown format: 65534"), and would turn a faithful copy into an export crash.
    """
    return sf.info(str(path)).frames


def write_sample_wav(sample: Sample, dest: Path) -> None:
    """Put one sample's audio on the card, copying the source file when nothing changed it.

    A sample that came from a library WAV and reached here still flagged `audio_verbatim`
    has been through the whole analysis chain without a single frame being altered — no
    trim, no loop crossfade, no normalize gain (each of those clears the flag where it
    acts). Re-encoding it through soundfile would still change the file: float32 in memory
    writes back as 16-bit PCM, and a mono source has already been widened to dual-mono by
    AudioBuffer, so a vendor's 24-bit mono master would land on the card as a 16-bit stereo
    approximation of itself for no reason at all. Copy the bytes instead: the vendor's bit
    depth, channel count and metadata chunks all survive, and both target devices read
    16/24-bit mono or stereo natively.

    Anything else — a rendered VST/CLAP capture, or a library sample the analysis genuinely
    had to modify — is encoded from the audio in memory, exactly as before, at one channel
    or two: a capture the scan measured as mono (`capture.mono`, see analysis/channels.py)
    arrives here already folded, and writing its duplicate channel out would double the
    file on the card for a signal the plugin never made stereo.
    """
    src = sample.metadata.get("source_file")
    if sample.metadata.get("audio_verbatim") and src and Path(src).suffix.lower() == ".wav":
        shutil.copy2(src, dest)
        return
    data = sample.audio.data
    # Channel 0, not the mean: the adapter folded both channels to the same signal, so
    # they are equal by construction and averaging would only add rounding.
    frames = data[0] if sample.metadata.get("mono") else data.T
    sf.write(str(dest), frames, sample.audio.sample_rate)


def sample_wav_name(sample: Sample, tempo_bpm: float, used_names: set[str]) -> str:
    """Filename for one sample's WAV, unique within `used_names` (which it updates).

    A sample that came from a file on disk keeps that file's name, so a preset built
    from a library is still recognisable on the card. Two samples whose source WAVs
    share a basename (e.g. `01.wav` in two per-instrument subdirs of a kit, or
    same-named picks across category folders in assemble-kits) would otherwise
    overwrite each other and both point at whichever WAV was written last, so a
    collision falls back to prefixing the source's parent directory.

    Rendered captures have no source file and are named from what identifies them
    instead: note, tempo, velocity and round robin.
    """
    if "source_file" in sample.metadata:
        src = Path(sample.metadata["source_file"])
        name = src.name
        if name in used_names:
            name = f"{src.parent.name}_{src.name}"
            i = 2
            while name in used_names:
                name = f"{src.parent.name}_{i}_{src.name}"
                i += 1
    else:
        bpm = int(round(tempo_bpm))
        name = f"note{sample.note:03d}_T{bpm:03d}_V{sample.velocity:03d}_RR{sample.round_robin}.wav"
    used_names.add(name)
    return name


def _iter_chunks(raw: bytes):
    """Yield (id, offset-of-chunk-header, payload-size) for each RIFF chunk."""
    off = 12
    while off + 8 <= len(raw):
        cid = raw[off:off + 4]
        size = int.from_bytes(raw[off + 4:off + 8], "little")
        yield cid, off, size
        off += 8 + size + (size % 2)


def _read_chunk(path: Path, chunk_id: bytes) -> bytes | None:
    raw = path.read_bytes()
    for cid, off, size in _iter_chunks(raw):
        if cid == chunk_id:
            return raw[off + 8:off + 8 + size]
    return None


def _insert_chunk(path: Path, chunk_id: bytes, payload: bytes) -> None:
    """Splice a chunk in ahead of `data`, fixing up the RIFF size field."""
    raw = bytearray(path.read_bytes())
    at = next((off for cid, off, _ in _iter_chunks(bytes(raw)) if cid == b"data"), None)
    if at is None:
        return
    chunk = chunk_id + len(payload).to_bytes(4, "little") + payload
    if len(payload) % 2:
        chunk += b"\0"
    raw[at:at] = chunk
    raw[4:8] = (int.from_bytes(raw[4:8], "little") + len(chunk)).to_bytes(4, "little")
    path.write_bytes(bytes(raw))


def write_wavetable_wav(src: Path, dest: Path) -> None:
    """Put a wavetable file on the card — byte-for-byte whenever that's possible.

    Copying raw bytes is the default: the file is unmodified by design (see
    WavetableAdapter), and re-encoding through soundfile risks dropping the Serum
    `clm` metadata chunk the Deluge firmware reads to recognise a wavetable.

    A STEREO source is the one case that can't be copied, and both wavetable targets
    rule it out for their own reason: the Deluge's wavetable oscillator is mono-only
    and refuses to load a stereo file as a wavetable at all, and the Bento rejects one
    outright with "Wavetables must be mono WAVs." (a firmware string). So it is
    downmixed, truncated to whole 2048-sample windows, and rewritten with any `clm`
    chunk carried across. (The Polyend Tracker Mini does play stereo wavetables, so the
    .pti export of the same config keeps both channels; this narrowing is for the two
    devices that read a WAV off the card.)
    """
    info = sf.info(str(src))
    if info.channels == 1:
        shutil.copy2(src, dest)
        return

    data, sr = sf.read(str(src), dtype="float64", always_2d=True)
    mono = data.mean(axis=1)
    usable = (len(mono) // _WT_WINDOW) * _WT_WINDOW
    if usable:
        mono = mono[:usable]
    sf.write(str(dest), mono, sr, subtype=info.subtype)
    clm = _read_chunk(src, b"clm ")
    if clm is not None:
        _insert_chunk(dest, b"clm ", clm)

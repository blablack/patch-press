"""Path and WAV helpers shared by all exporters."""

import wave
from pathlib import Path

from ...model.sample import Sample


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
    """Frame count of a WAV an exporter just wrote (plain PCM, no extensible chunks)."""
    with wave.open(str(path), "rb") as f:
        return f.getnframes()


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

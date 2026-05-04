import re
from pathlib import Path

from ...config.schema import LibrarySourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Matches note+octave at end of stem, with optional _NNNN round-robin suffix.
# Examples: A0, A#-1, C3_0001, F#2_0003
_NOTE_RR_RE = re.compile(r"([A-G]#?)(-?\d+)(?:_(\d+))?$", re.IGNORECASE)


def _parse_note_rr(stem: str) -> tuple[int, int | None] | None:
    """Return (midi_note, rr_index_or_None), or None if no note found."""
    m = _NOTE_RR_RE.search(stem)
    if not m:
        return None
    name = m.group(1).upper()
    if name not in _NOTE_NAMES:
        return None
    midi = _NOTE_NAMES.index(name) + (int(m.group(2)) + 1) * 12
    rr = int(m.group(3)) if m.group(3) is not None else None
    return midi, rr


class LibraryAdapter:
    def __init__(self, config: LibrarySourceConfig):
        self._config = config

    def capture(self, name: str | None = None) -> SampleSet:
        path = self._config.path
        sset_name = name or path.name
        wavs = sorted(path.glob("*.wav"))
        subdirs = [p for p in sorted(path.iterdir()) if p.is_dir()]
        if wavs:
            return self._load_multisample(sset_name, path, wavs)
        if subdirs:
            return self._load_kit(sset_name, path, subdirs)
        raise ValueError(f"No WAV files or subdirectories found in {path}")

    def _load_multisample(self, name: str, path: Path, wavs: list[Path]) -> SampleSet:
        # Group by note → {rr_index_or_None: path}
        # None key = base file (no RR suffix); numbered keys = round robins
        groups: dict[int, dict[int | None, Path]] = {}
        for wav in wavs:
            result = _parse_note_rr(wav.stem)
            if result is None:
                continue
            note, rr = result
            groups.setdefault(note, {})[rr] = wav

        samples: list[Sample] = []
        for note in sorted(groups):
            files = groups[note]
            numbered = {k: v for k, v in files.items() if k is not None}
            base = files.get(None)

            if numbered:
                for rr_idx, wav in sorted(numbered.items()):
                    samples.append(Sample(
                        note=note, velocity=100, round_robin=rr_idx,
                        audio=AudioBuffer.from_file(wav),
                        metadata={"source_file": str(wav)},
                    ))
            elif base:
                samples.append(Sample(
                    note=note, velocity=100, round_robin=1,
                    audio=AudioBuffer.from_file(base),
                    metadata={"source_file": str(base)},
                ))

        return SampleSet(
            name=name, category=Category.SYNTH,
            samples=samples, source_metadata={"path": str(path)},
        )

    def _load_kit(self, name: str, path: Path, subdirs: list[Path]) -> SampleSet:
        samples: list[Sample] = []
        for subdir in subdirs:
            wavs = sorted(subdir.glob("*.wav"))
            result = _parse_note_rr(subdir.name)
            note = result[0] if result else 48
            for rr, wav in enumerate(wavs, start=1):
                samples.append(Sample(
                    note=note, velocity=100, round_robin=rr,
                    audio=AudioBuffer.from_file(wav),
                    metadata={"source_file": str(wav), "instrument": subdir.name},
                ))
        samples.sort(key=lambda s: (s.note, s.round_robin))
        return SampleSet(
            name=name, category=Category.DRUM,
            samples=samples, source_metadata={"path": str(path)},
        )

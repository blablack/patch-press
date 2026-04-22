import re
from pathlib import Path

from ...config.schema import LibrarySourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
_NOTE_RE = re.compile(r"^([A-G]#?)(-?\d+)", re.IGNORECASE)
_NOTE_VEL_RE = re.compile(r"^([A-G]#?)(-?\d+)_[Vv](\d+)", re.IGNORECASE)


def _parse_note(stem: str) -> int | None:
    m = _NOTE_RE.match(stem)
    if not m:
        return None
    name = m.group(1).upper()
    if name not in _NOTE_NAMES:
        return None
    return (_NOTE_NAMES.index(name)) + (int(m.group(2)) + 1) * 12


def _parse_velocity(stem: str) -> int:
    m = _NOTE_VEL_RE.match(stem)
    return int(m.group(3)) if m else 100


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
        samples: list[Sample] = []
        for wav in wavs:
            note = _parse_note(wav.stem)
            if note is None:
                continue
            vel = _parse_velocity(wav.stem)
            samples.append(
                Sample(
                    note=note,
                    velocity=vel,
                    round_robin=1,
                    audio=AudioBuffer.from_file(wav),
                    metadata={"source_file": str(wav)},
                )
            )
        samples.sort(key=lambda s: (s.note, s.velocity))
        return SampleSet(
            name=name,
            category=Category.SYNTH,
            samples=samples,
            source_metadata={"path": str(path)},
        )

    def _load_kit(self, name: str, path: Path, subdirs: list[Path]) -> SampleSet:
        samples: list[Sample] = []
        for subdir in subdirs:
            wavs = sorted(subdir.glob("*.wav"))
            note = _parse_note(subdir.name) or 48  # default C3
            for rr, wav in enumerate(wavs, start=1):
                samples.append(
                    Sample(
                        note=note,
                        velocity=100,
                        round_robin=rr,
                        audio=AudioBuffer.from_file(wav),
                        metadata={"source_file": str(wav), "instrument": subdir.name},
                    )
                )
        samples.sort(key=lambda s: (s.note, s.round_robin))
        return SampleSet(
            name=name,
            category=Category.DRUM,
            samples=samples,
            source_metadata={"path": str(path)},
        )

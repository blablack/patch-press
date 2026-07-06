import re
from pathlib import Path

from ...config.schema import LibrarySourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet
from ..smpl import read_loop_points

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

# Matches note+octave at end of stem, with optional _NNNN round-robin suffix.
# Examples: A0, A#-1, C3_0001, F#2_0003
_NOTE_RR_RE = re.compile(r"([A-G]#?)(-?\d+)(?:_(\d+))?$", re.IGNORECASE)


def _make_sample(note: int, rr: int, wav: Path) -> Sample:
    """Build a Sample, attaching the WAV's authored `smpl` loop when present.

    When the file carries loop points, we trust them (loop_points set + an `authored_loop`
    flag) so the pipeline ships the author's exact loop with no re-detection and no
    crossfade. Frames are raw-file indices; the pipeline rebases them if it trims.
    """
    metadata = {"source_file": str(wav)}
    loop = read_loop_points(wav)
    if loop is not None:
        metadata["authored_loop"] = True
    return Sample(
        note=note, velocity=100, round_robin=rr,
        audio=AudioBuffer.from_file(wav),
        loop_points=loop,
        metadata=metadata,
    )


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


def _grouped_notes(wavs: list[Path], note_step: int = 1) -> dict[int, dict[int | None, Path]]:
    """Group WAVs by parsed note → {rr_index_or_None: path}, thinned to at least
    note_step semitones apart (greedy, low-to-high).

    None key = base file (no RR suffix); numbered keys = round robins.
    """
    groups: dict[int, dict[int | None, Path]] = {}
    for wav in wavs:
        result = _parse_note_rr(wav.stem)
        if result is None:
            continue
        note, rr = result
        groups.setdefault(note, {})[rr] = wav

    if note_step > 1:
        thinned: list[int] = []
        for note in sorted(groups):
            if not thinned or note - thinned[-1] >= note_step:
                thinned.append(note)
        return {note: groups[note] for note in thinned}
    return groups


class LibraryAdapter:
    def __init__(self, config: LibrarySourceConfig):
        self._config = config

    def capture(
        self, name: str | None = None, max_round_robins: int = 1, note_step: int = 1, progress=None
    ) -> SampleSet:
        path = self._config.path
        if path.is_file():
            return self._load_single(name or path.stem, path, progress)
        sset_name = name or path.name
        wavs = sorted(path.glob("*.wav"))
        subdirs = [p for p in sorted(path.iterdir()) if p.is_dir()]
        if wavs:
            return self._load_multisample(sset_name, path, wavs, max_round_robins, note_step, progress)
        if subdirs:
            return self._load_kit(sset_name, path, subdirs, max_round_robins, progress)
        raise ValueError(f"No WAV files or subdirectories found in {path}")

    def expected_count(self, max_round_robins: int = 1, note_step: int = 1) -> int:
        """Sample count `capture` will produce, without reading any audio.

        Mirrors the note-thinning + round-robin-capping below so callers (the batch
        progress bar) can size a total that matches the work actually done, instead of
        a raw file/subfolder count that overshoots once thinning/capping kick in.
        """
        path = self._config.path
        if path.is_file():
            return 1
        wavs = sorted(path.glob("*.wav"))
        if wavs:
            total = 0
            for files in _grouped_notes(wavs, note_step).values():
                numbered = [k for k in files if k is not None]
                if numbered:
                    total += min(len(numbered), max_round_robins)
                elif None in files:
                    total += 1
            return total
        subdirs = [p for p in sorted(path.iterdir()) if p.is_dir()]
        return sum(min(len(list(d.glob("*.wav"))), max_round_robins) for d in subdirs)

    def _load_multisample(
        self,
        name: str,
        path: Path,
        wavs: list[Path],
        max_round_robins: int = 1,
        note_step: int = 1,
        progress=None,
    ) -> SampleSet:
        groups = _grouped_notes(wavs, note_step)

        samples: list[Sample] = []
        for note in sorted(groups):
            files = groups[note]
            numbered = {k: v for k, v in files.items() if k is not None}
            base = files.get(None)

            if numbered:
                for rr_idx, wav in sorted(numbered.items())[:max_round_robins]:
                    samples.append(_make_sample(note, rr_idx, wav))
                    if progress is not None:
                        progress.update(1)
            elif base:
                samples.append(_make_sample(note, 1, base))
                if progress is not None:
                    progress.update(1)

        return SampleSet(
            name=name, category=Category.SYNTH,
            samples=samples, source_metadata={"path": str(path)},
        )

    def _load_single(self, name: str, wav: Path, progress=None) -> SampleSet:
        """Build a one-sample SampleSet from a source pointed directly at a WAV file.

        Used for oneshot-per-preset libraries (e.g. Monosounds) where each file is its
        own patch rather than one note of a multisample. `note` is the root MIDI note
        pinned by scan-oneshots (filenames there carry no octave to parse).
        """
        note = self._config.note if self._config.note is not None else 60
        sample = _make_sample(note, 1, wav)
        if progress is not None:
            progress.update(1)
        return SampleSet(
            name=name, category=Category.SYNTH,
            samples=[sample], source_metadata={"path": str(wav)},
        )

    def _load_kit(
        self, name: str, path: Path, subdirs: list[Path], max_round_robins: int = 1, progress=None
    ) -> SampleSet:
        samples: list[Sample] = []
        for subdir in subdirs:
            wavs = sorted(subdir.glob("*.wav"))
            result = _parse_note_rr(subdir.name)
            note = result[0] if result else 48
            for rr, wav in enumerate(wavs[:max_round_robins], start=1):
                samples.append(Sample(
                    note=note, velocity=100, round_robin=rr,
                    audio=AudioBuffer.from_file(wav),
                    metadata={"source_file": str(wav), "instrument": subdir.name},
                ))
                if progress is not None:
                    progress.update(1)
        samples.sort(key=lambda s: (s.note, s.round_robin))
        return SampleSet(
            name=name, category=Category.DRUM,
            samples=samples, source_metadata={"path": str(path)},
        )

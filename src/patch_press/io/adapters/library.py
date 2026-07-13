import re
from pathlib import Path

from ...analysis.drumkit import sort_key as _drumkit_sort_key
from ...analysis.pitch import NOTE_NAMES
from ...config.schema import LibrarySourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet
from ..smpl import read_loop_points

# Matches note+octave at end of stem, with optional _NNNN round-robin suffix.
# Examples: A0, A#-1, Bb2, C3_0001, F#2_0003
#
# The `(?:^|(?<=[^A-Za-z]))` prefix requires the note letter to sit at start-of-stem
# or immediately after a non-letter (`_`, ` `, `.`, `-`). Without it, `re.search`
# will greedily right-anchor on any trailing digits, so `Piano_Db3` matches the
# lowercase `b3` and reports B3 instead of Db3 — off by up to a major third for
# every flat-named note in libraries that use flat spellings.
_NOTE_RR_RE = re.compile(
    r"(?:^|(?<=[^A-Za-z]))([A-G][#b]?)(-?\d+)(?:_(\d+))?$",
    re.IGNORECASE,
)

# Fallback for libraries that put the note at the START of the stem with descriptive
# text after it (e.g. "A1 Precision Bass Amped Long_01"). Only consulted when the
# end-anchored match above fails, so end-named libraries are never affected. The note
# must be followed by a non-alphanumeric boundary so "Ambient..." / "Cello..." don't
# match as notes.
_NOTE_START_RE = re.compile(
    r"^([A-G][#b]?)(-?\d+)(?:_(\d+))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Enharmonic remap: flat spellings map to their sharp equivalents so the returned
# MIDI is correct. Bb → A#, Eb → D#, Gb → F#, Ab → G#, Db → C# (subtract one
# semitone from the natural letter's index).
_FLAT_ENHARMONIC = {"Bb": "A#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Db": "C#"}


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
    """Return (midi_note, rr_index_or_None), or None if no note found or MIDI out of range."""
    m = _NOTE_RR_RE.search(stem) or _NOTE_START_RE.match(stem)
    if not m:
        return None
    raw_name = m.group(1)
    # Normalize case: letter upper, accidental preserved-lower for `b` (flat glyph),
    # upper for `#` (sharp). Then remap flats to their enharmonic sharp so the
    # MIDI lookup stays a single table.
    if len(raw_name) == 2 and raw_name[1].lower() == "b":
        name = raw_name[0].upper() + "b"
        name = _FLAT_ENHARMONIC.get(name, name)
    else:
        name = raw_name.upper()
    if name not in NOTE_NAMES:
        return None
    midi = NOTE_NAMES.index(name) + (int(m.group(2)) + 1) * 12
    # Reject values outside the MIDI 0..127 range. Same convention as
    # runner/scan.py:note_name_to_midi. Filenames whose trailing digits
    # accidentally look like an octave (e.g. `..._D-99`, `..._C10`) end up
    # here — silently shipping a negative / >127 note produces invalid
    # Deluge XML (rangeTopNote and transpose values outside 0..127).
    if not 0 <= midi <= 127:
        return None
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
        if self._config.files is not None:
            return self._load_drumkit_flat(name or path.name, path, self._config.files, progress)
        if path.is_file():
            return self._load_single(name or path.stem, path, progress)
        sset_name = name or path.name
        wavs = sorted(path.glob("*.wav"))
        subdirs = [p for p in sorted(path.iterdir()) if p.is_dir()]
        if wavs:
            if self._config.drumkit:
                return self._load_drumkit_flat(sset_name, path, wavs, progress)
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
        if self._config.files is not None:
            return len(self._config.files)
        if path.is_file():
            return 1
        wavs = sorted(path.glob("*.wav"))
        if wavs:
            if self._config.drumkit:
                return len(wavs)
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

    def _load_drumkit_flat(
        self, name: str, path: Path, wavs: list[Path], progress=None
    ) -> SampleSet:
        """One WAV = one kit pad (e.g. Samples From Mars '808 From Mars': a flat folder
        of loose one-shot hits, no shared per-note structure, no per-instrument
        subfolders). Instrument identity comes from the filename alone (see
        analysis.drumkit.classify_instrument); `note` here is purely a synthetic sort
        key for kit-row order — the exporter's _export_kit never serializes it, pad
        position is just document order in <soundSources>.
        """
        ordered = sorted(wavs, key=lambda w: _drumkit_sort_key(w.stem))
        samples: list[Sample] = []
        for i, wav in enumerate(ordered):
            samples.append(Sample(
                note=i, velocity=100, round_robin=1,
                audio=AudioBuffer.from_file(wav),
                metadata={"source_file": str(wav), "instrument": wav.stem},
            ))
            if progress is not None:
                progress.update(1)
        return SampleSet(
            name=name, category=Category.DRUM,
            samples=samples, source_metadata={"path": str(path)},
        )

    def _load_kit(
        self, name: str, path: Path, subdirs: list[Path], max_round_robins: int = 1, progress=None
    ) -> SampleSet:
        """Subfolder-per-instrument kit (Kick/, Snare/, HH Closed/, Clap/, ...) OR a
        subfolder-per-note multisample (A3/, C#4/, ...). We tell them apart by whether
        _parse_note_rr recognises the subfolder names.

        For the kit case, subfolder names don't encode MIDI notes and we can't just
        default every un-parsed subdir to note=48 — every pad would collapse onto one
        MIDI slot and the exporter's `by_note[note][:2]` would drop every third
        instrument. Mirror _load_drumkit_flat: sort by the drumkit canonical order and
        assign a synthetic `note=i` per subdir. Exporter never serializes it (pad
        position is document order in <soundSources>).
        """
        parsed = [(subdir, _parse_note_rr(subdir.name)) for subdir in subdirs]
        is_kit = not any(r is not None for _, r in parsed)

        samples: list[Sample] = []
        if is_kit:
            ordered = sorted(subdirs, key=lambda d: _drumkit_sort_key(d.name))
            for i, subdir in enumerate(ordered):
                wavs = sorted(subdir.glob("*.wav"))
                for rr, wav in enumerate(wavs[:max_round_robins], start=1):
                    samples.append(Sample(
                        note=i, velocity=100, round_robin=rr,
                        audio=AudioBuffer.from_file(wav),
                        metadata={"source_file": str(wav), "instrument": subdir.name},
                    ))
                    if progress is not None:
                        progress.update(1)
            category = Category.DRUM
        else:
            for subdir, result in parsed:
                if result is None:
                    # A stray non-note subfolder in an otherwise pitched multisample;
                    # ignore it rather than folding it onto a wrong MIDI slot.
                    continue
                note = result[0]
                wavs = sorted(subdir.glob("*.wav"))
                for rr, wav in enumerate(wavs[:max_round_robins], start=1):
                    samples.append(Sample(
                        note=note, velocity=100, round_robin=rr,
                        audio=AudioBuffer.from_file(wav),
                        metadata={"source_file": str(wav), "instrument": subdir.name},
                    ))
                    if progress is not None:
                        progress.update(1)
            samples.sort(key=lambda s: (s.note, s.round_robin))
            category = Category.SYNTH
        return SampleSet(
            name=name, category=category,
            samples=samples, source_metadata={"path": str(path)},
        )

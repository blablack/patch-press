from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class VSTSourceConfig:
    plugin: Path
    preset: Optional[str] = None
    raw_state: Optional[str] = None


@dataclass
class CLAPSourceConfig:
    plugin: Path
    plugin_id: str
    preset: Optional[str] = None
    preset_path: Optional[Path] = None
    raw_state: Optional[str] = None


@dataclass
class LibrarySourceConfig:
    path: Path
    filename_pattern: Optional[str] = None
    # Fixed MIDI root note for a single-file source (path points at one WAV, not a folder).
    # Filenames in some libraries (e.g. Monosounds oneshots) carry no octave, so the note is
    # detected once by the scan command and pinned here rather than re-parsed from the name.
    note: Optional[int] = None
    # True when `path` is a flat folder of loose one-shot drum hits (one file = one kit
    # pad, instrument identified by filename keyword — see analysis/drumkit.py) rather
    # than a multisample (notes parsed from filenames). Set by `scan-library --type
    # drumkit`.
    drumkit: bool = False
    # Explicit file list for a kit assembled from across a "bag of hits" library tree
    # (see analysis/drumkit_assemble.py / `assemble-kits`) — the files aren't siblings
    # in one folder, so they're resolved once at scan time and pinned here rather than
    # re-derived by globbing `path`. Takes priority over `path` when set.
    files: Optional[list[Path]] = None


@dataclass
class BitwigSourceConfig:
    # A Bitwig `.multisample` archive (a zip of WAVs + a `multisample.xml` mapping).
    # Unlike a raw WAV library, the note mapping, velocity zones and loop points are all
    # authoritative metadata read from the XML — no filename parsing or loop detection.
    path: Path
    # A `.multisample` can layer several velocity zones per note. Hardware sampler
    # presets don't do velocity layering (the Deluge/Polyend exporters collapse to one
    # sample per note anyway), so the adapter keeps a single layer per note: the zone
    # whose top velocity is nearest this target. Hand-edit to pick a softer/louder layer.
    velocity: int = 100


@dataclass
class WavetableSourceConfig:
    path: Path


@dataclass
class WavetableConfig:
    """Archetype-driven Deluge patch parameters for a wavetable, resolved once at scan
    time (see runner/scan.py:scan_wavetables) from the file's own spectral content —
    see docs/inputs/wavetables.md. Everything here is a plain 0..1 fraction except
    archetype/filter_type; the exporter's _q31() spreads fractions across the Deluge's
    signed-32-bit param range.
    """

    archetype: str  # pad | pluck | bass | lead | drone | evolving_pad
    wt_position: float
    lfo2_rate: float
    lfo2_depth: float
    filter_cutoff: float
    attack: float
    decay: float
    sustain: float
    release: float
    filter_type: str = "lpf"


@dataclass
class CaptureConfig:
    note_range: tuple[int, int] = (36, 96)
    note_step: int = 3
    velocities: list[int] = field(default_factory=lambda: [100])
    round_robins: int = 1
    duration_s: float = 4.0
    release_tail_s: float = 2.0
    tempo_bpm: float = 120.0
    sample_rate: int = 48000


@dataclass
class AnalysisConfig:
    trim: bool = True
    pitch_verify: bool = True
    pitch_tolerance_cents: float = 50.0
    loop: bool = False
    loop_use_tempo: bool = False
    loop_quality_threshold: float = 0.8
    # Flat 10 ms for now (synths + pads). Covers ~1 fundamental period down to the low
    # register and gives slop for non-phase-aligned (evolving) loops, without smearing
    # movement. May differentiate per sound type later — only synths are being tested now.
    loop_crossfade_ms: float = 10.0
    tempo_bpm: float | None = None
    normalize: str = "per_set"  # "per_sample" | "per_set" | "none"
    classify_drums: bool = False


@dataclass
class OutputConfig:
    name: str
    # Optional SD-card subfolder tree under SYNTHS|KITS/<collection>/, mirroring the
    # source's own organisation (e.g. a u-he bank/author path "1 BASS" or
    # "THIRD PARTY/Mr Wobble"). Empty = filed directly under the collection.
    subfolder: str = ""


@dataclass
class RunConfig:
    source: Union[VSTSourceConfig, CLAPSourceConfig, LibrarySourceConfig, BitwigSourceConfig, WavetableSourceConfig]
    capture: CaptureConfig
    analysis: AnalysisConfig
    output: OutputConfig
    name: str = ""
    category: str = "synth"
    wavetable: Optional[WavetableConfig] = None

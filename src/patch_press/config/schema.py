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
    loop_quality_threshold: float = 0.75
    loop_crossfade_ms: float = 5.0
    tempo_bpm: float | None = None
    normalize: str = "per_set"  # "per_sample" | "per_set" | "none"
    classify_drums: bool = False


@dataclass
class OutputConfig:
    name: str


@dataclass
class RunConfig:
    source: Union[VSTSourceConfig, CLAPSourceConfig, LibrarySourceConfig]
    capture: CaptureConfig
    analysis: AnalysisConfig
    output: OutputConfig
    name: str = ""
    category: str = "synth"

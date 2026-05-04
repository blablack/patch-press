from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Union


@dataclass
class VSTSourceConfig:
    plugin: Path
    preset: Optional[str] = None
    fx_chain: list[Path] = field(default_factory=list)
    program_index: Optional[int] = None
    preset_file: Optional[Path] = None
    raw_state: Optional[str] = None
    parameter_state: dict = field(default_factory=dict)
    preset_source: Optional[str] = None


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


@dataclass
class AnalysisConfig:
    trim: bool = True
    pitch_verify: bool = True
    pitch_tolerance_cents: float = 50.0
    loop: bool = False
    loop_quality_threshold: float = 0.8
    normalize: str = "per_set"  # "per_sample" | "per_set" | "none"
    classify_drums: bool = False


@dataclass
class OutputConfig:
    name: str


@dataclass
class RunConfig:
    source: Union[VSTSourceConfig, LibrarySourceConfig]
    capture: CaptureConfig
    analysis: AnalysisConfig
    output: OutputConfig
    name: str = ""
    category: str = "synth"

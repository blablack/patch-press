from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .audio import AudioBuffer


class Category(str, Enum):
    SYNTH = "synth"
    DRUM = "drum"


@dataclass
class Sample:
    note: int
    velocity: int
    round_robin: int
    audio: AudioBuffer
    loop_points: Optional[tuple[int, int]] = None
    analysis: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class SampleSet:
    name: str
    category: Category
    samples: list[Sample] = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)
    tempo_bpm: float = 120.0

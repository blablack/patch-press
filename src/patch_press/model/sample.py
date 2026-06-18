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
    # Sample index of the note-off (key release) in `audio`, when known. Set for
    # rendered VST/CLAP captures (we control how long the note is held); left None for
    # library samples, where the recording carries no note-off. Lets the envelope
    # classifier measure the level *at release* instead of guessing from a fixed window.
    note_off: Optional[int] = None


@dataclass
class SampleSet:
    name: str
    category: Category
    samples: list[Sample] = field(default_factory=list)
    source_metadata: dict = field(default_factory=dict)
    tempo_bpm: float = 120.0

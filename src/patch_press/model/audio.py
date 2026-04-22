from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class AudioBuffer:
    """Stereo float32 audio. data shape: (2, N)."""

    data: np.ndarray
    sample_rate: int

    def __post_init__(self) -> None:
        if self.data.ndim == 1:
            self.data = np.stack([self.data, self.data])
        if self.data.ndim != 2 or self.data.shape[0] != 2:
            raise ValueError(f"Expected shape (2, N), got {self.data.shape}")
        self.data = self.data.astype(np.float32)

    @property
    def num_frames(self) -> int:
        return self.data.shape[1]

    @property
    def duration_s(self) -> float:
        return self.num_frames / self.sample_rate

    @classmethod
    def from_file(cls, path: Path | str) -> "AudioBuffer":
        import soundfile as sf

        data, sr = sf.read(str(path), dtype="float32")
        if data.ndim == 1:
            data = np.stack([data, data])
        else:
            data = data.T
        return cls(data=data.astype(np.float32), sample_rate=sr)

    def to_mono(self) -> np.ndarray:
        return self.data.mean(axis=0)

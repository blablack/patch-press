from ...config.schema import WavetableSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet


class WavetableAdapter:
    """Loads a single Serum-format wavetable file, unmodified.

    Unlike VST/CLAP/library sources, a wavetable isn't a captured performance to
    trim/envelope/loop/normalize — it ships to the SD card byte-for-byte (see
    runner/pipeline.py, which skips analyze_sampleset entirely for this source type)
    and the Deluge's own wavetable-scan engine reads it directly.
    """

    def __init__(self, config: WavetableSourceConfig):
        self._config = config

    def capture(self, name: str | None = None) -> SampleSet:
        path = self._config.path
        sample = Sample(
            note=60, velocity=100, round_robin=1,
            audio=AudioBuffer.from_file(path),
            metadata={"source_file": str(path)},
        )
        return SampleSet(
            name=name or path.stem, category=Category.WAVETABLE,
            samples=[sample], source_metadata={"path": str(path)},
        )

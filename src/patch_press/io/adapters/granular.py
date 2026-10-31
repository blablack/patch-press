from ...config.schema import GranularSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet


class GranularAdapter:
    """Loads one source sound for a Bento granular patch, unmodified.

    Like a wavetable, a granular source isn't a captured performance: runner/pipeline.py
    skips analyze_sampleset for it. The exporter cuts the excerpt(s) the grain
    oscillators read (io/exporters/bento.py:_write_grain_sources); here the file is only
    loaded.
    """

    def __init__(self, config: GranularSourceConfig):
        self._config = config

    def capture(self, name: str | None = None) -> SampleSet:
        path = self._config.path
        sample = Sample(
            note=60,
            velocity=100,
            round_robin=1,
            audio=AudioBuffer.from_file(path),
            metadata={"source_file": str(path)},
        )
        return SampleSet(
            name=name or path.stem,
            category=Category.GRANULAR,
            samples=[sample],
            source_metadata={"path": str(path)},
        )

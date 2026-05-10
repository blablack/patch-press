import numpy as np

from ..model.audio import AudioBuffer
from ..model.sample import Sample, SampleSet

_TARGET_DB = -1.0
_TARGET = 10 ** (_TARGET_DB / 20.0)


def _peak(buf: AudioBuffer) -> float:
    return float(np.abs(buf.data).max())


def _apply_gain(sample: Sample, gain: float) -> Sample:
    normed = AudioBuffer(
        data=(sample.audio.data * gain).astype(np.float32),
        sample_rate=sample.audio.sample_rate,
    )
    return Sample(
        note=sample.note,
        velocity=sample.velocity,
        round_robin=sample.round_robin,
        audio=normed,
        loop_points=sample.loop_points,
        analysis={**sample.analysis, "normalize_gain_db": round(20 * np.log10(gain), 2)},
        metadata=sample.metadata,
    )


def normalize_sample(sample: Sample) -> Sample:
    peak = _peak(sample.audio)
    if peak == 0:
        return sample
    return _apply_gain(sample, _TARGET / peak)


def normalize_set(sset: SampleSet) -> SampleSet:
    """Apply the same gain to every sample so relative levels are preserved."""
    peaks = [_peak(s.audio) for s in sset.samples]
    if not peaks:
        return sset
    max_peak = max(peaks)
    if max_peak == 0:
        return sset
    gain = _TARGET / max_peak
    return SampleSet(
        name=sset.name,
        category=sset.category,
        samples=[_apply_gain(s, gain) for s in sset.samples],
        source_metadata=sset.source_metadata,
        tempo_bpm=sset.tempo_bpm,
    )

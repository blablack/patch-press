import logging
import os
from concurrent.futures import ProcessPoolExecutor
from functools import partial


def _init_worker(level: int) -> None:
    logging.basicConfig(level=level, format="%(message)s")

from ..config.schema import AnalysisConfig
from ..model.sample import Category, Sample, SampleSet
from .classify import classify_drum
from .envelope import analyze_envelope
from .loop import find_loop_points
from .normalize import normalize_sample, normalize_set
from .pitch import verify_pitch
from .trim import trim_silence

log = logging.getLogger(__name__)


def _analyze_one(sample: Sample, config: AnalysisConfig) -> Sample:
    logging.info(
        f"Analyzing: note {sample.note} ; velocity {sample.velocity} ; rr {sample.round_robin}"
    )
    audio = sample.audio
    analysis = dict(sample.analysis)

    if config.trim:
        audio = trim_silence(audio)

    analysis.update(analyze_envelope(audio))

    if config.pitch_verify:
        result = verify_pitch(audio, sample.note, config.pitch_tolerance_cents)
        analysis.update(result)
        if not result.get("pitch_ok", True):
            log.warning(
                "  WARNING note %s: pitch mismatch (%s cents off)",
                sample.note,
                result.get("cents_diff", "?"),
            )

    loop_points = sample.loop_points
    if config.loop:
        found = find_loop_points(audio, config.loop_quality_threshold)
        if found:
            loop_points, quality = found
            analysis["loop_quality"] = round(quality, 3)
        else:
            analysis["loop_quality"] = None
            analysis["loop_warning"] = "no_suitable_loop_found"

    return Sample(
        note=sample.note,
        velocity=sample.velocity,
        round_robin=sample.round_robin,
        audio=audio,
        loop_points=loop_points,
        analysis=analysis,
        metadata=sample.metadata,
    )


def analyze_sampleset(sset: SampleSet, config: AnalysisConfig, workers: int = 1) -> SampleSet:
    level = logging.getLogger().getEffectiveLevel()
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(level,)) as executor:
        analyzed = list(executor.map(partial(_analyze_one, config=config), sset.samples))

    if sset.category == Category.DRUM and config.classify_drums:
        analyzed = [
            Sample(
                note=s.note,
                velocity=s.velocity,
                round_robin=s.round_robin,
                audio=s.audio,
                loop_points=s.loop_points,
                analysis={**s.analysis, "drum_type": classify_drum(s.audio)},
                metadata=s.metadata,
            )
            for s in analyzed
        ]

    result = SampleSet(
        name=sset.name,
        category=sset.category,
        samples=analyzed,
        source_metadata=sset.source_metadata,
    )

    if config.normalize == "per_sample":
        result = SampleSet(
            name=result.name,
            category=result.category,
            samples=[normalize_sample(s) for s in result.samples],
            source_metadata=result.source_metadata,
        )
    elif config.normalize == "per_set":
        result = normalize_set(result)

    return result

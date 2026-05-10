import logging
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial

from tqdm import tqdm


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
    audio = sample.audio
    analysis = dict(sample.analysis)

    if config.trim:
        audio = trim_silence(audio)

    analysis.update(analyze_envelope(audio))

    if config.pitch_verify:
        result = verify_pitch(audio, sample.note, config.pitch_tolerance_cents)
        analysis.update(result)

    loop_points = sample.loop_points
    if config.loop:
        found = find_loop_points(audio, config.loop_quality_threshold, config.tempo_bpm)
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
    tempo_bpm = config.tempo_bpm or sset.tempo_bpm
    level = logging.getLogger().getEffectiveLevel()
    analyzed: list[Sample] = []

    fn = partial(_analyze_one, config=config)
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(level,)) as executor:
        futures = {executor.submit(fn, s): s for s in sset.samples}
        with tqdm(total=len(sset.samples), desc="Analyzing", unit="sample", leave=False) as pbar:
            for future in as_completed(futures):
                sample = future.result()
                analyzed.append(sample)
                if config.pitch_verify and not sample.analysis.get("pitch_ok", True):
                    cents = sample.analysis.get("cents_diff", "?")
                    tqdm.write(f"  WARNING note {sample.note}: pitch mismatch ({cents} cents off)")
                pbar.set_postfix(note=sample.note, vel=sample.velocity, rr=sample.round_robin)
                pbar.update(1)

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
        tempo_bpm=tempo_bpm,
    )

    if config.normalize == "per_sample":
        result = SampleSet(
            name=result.name,
            category=result.category,
            samples=[normalize_sample(s) for s in result.samples],
            source_metadata=result.source_metadata,
            tempo_bpm=tempo_bpm,
        )
    elif config.normalize == "per_set":
        result = normalize_set(result)

    return result

import dataclasses
import logging
import os
import warnings
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from functools import partial

from ..progress import ProgressBar as tqdm


_NOISY_LIBS = ("numba", "librosa")


class _ThirdPartyFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        path = getattr(record, "pathname", "")
        return not (
            any(record.name.startswith(lib) for lib in _NOISY_LIBS)
            or any(f"/{lib}/" in path for lib in _NOISY_LIBS)
        )


def suppress_noisy_loggers() -> None:
    for handler in logging.root.handlers:
        handler.addFilter(_ThirdPartyFilter())
    for name in _NOISY_LIBS:
        logging.getLogger(name).setLevel(logging.WARNING)
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="librosa")
    warnings.filterwarnings("ignore", category=UserWarning, module="librosa")
    # resource_tracker runs in a subprocess and reads PYTHONWARNINGS from the
    # environment, so warnings.filterwarnings can't reach it — set the env var
    # before any multiprocessing pool is created so it's inherited.
    _mp_filter = "ignore::UserWarning:multiprocessing.resource_tracker"
    pw = os.environ.get("PYTHONWARNINGS", "")
    if _mp_filter not in pw:
        os.environ["PYTHONWARNINGS"] = f"{pw},{_mp_filter}" if pw else _mp_filter


def _init_worker(level: int) -> None:
    from tqdm import tqdm as _tqdm

    class _Handler(logging.StreamHandler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                _tqdm.write(self.format(record))
            except Exception:
                self.handleError(record)

    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[_Handler()],
        force=True,
    )
    suppress_noisy_loggers()

from ..config.schema import AnalysisConfig
from ..model.audio import AudioBuffer
from ..model.sample import Category, Sample, SampleSet
from .classify import classify_drum
from .envelope import analyze_envelope
from .loop import (
    _MIN_LOOP_RANK_S,
    _XFADE_FALLBACK_MIN_DISC,
    adaptive_crossfade_ms,
    bake_loop_crossfade,
    central_fallback_loop,
    find_loop_candidates,
    validate_splice_reason,
)
from .normalize import normalize_sample, normalize_set
from .pitch import NOTE_NAMES, verify_pitch
from .trim import trim_bounds

log = logging.getLogger(__name__)

_MIN_DETECTION_RATE = 0.30   # fraction of samples that must detect any period before consensus check
_MIN_PERIOD_CONSENSUS = 0.15  # fraction of all samples that must share the same BPM-snapped period


def _note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


def _sound_type_string(analyzed: list[Sample], tempo_bpm: float | None) -> str:
    classifications = [s.analysis.get("classification") for s in analyzed]
    most_common = Counter(c for c in classifications if c).most_common(1)
    sound_class = most_common[0][0] if most_common else "sustained"

    if sound_class == "pluck":
        return "Pluck"

    # Check rhythm: require enough samples to detect any period (filters sounds where only
    # a handful of notes trigger the autocorr), then require consensus on the same period.
    mod_periods = [s.analysis.get("modulation_period_samples") for s in analyzed if s.analysis.get("modulation_period_samples")]
    if len(mod_periods) / len(analyzed) >= _MIN_DETECTION_RATE:
        period_counts = Counter(mod_periods)
        top_period, top_count = period_counts.most_common(1)[0]
        if top_count / len(analyzed) >= _MIN_PERIOD_CONSENSUS:
            sr = analyzed[0].audio.sample_rate
            mod_s = top_period / sr
            if tempo_bpm:
                return f"Sustained + rhythm (mod {mod_s:.2f}s @ {tempo_bpm:.0f} BPM)"
            return f"Sustained + rhythm (mod {mod_s:.2f}s)"

    return "Sustained"


def _envelope_only(sample: Sample, bpm: float | None) -> Sample:
    env = analyze_envelope(sample.audio, bpm=bpm, note_off=sample.note_off)
    return Sample(
        note=sample.note,
        velocity=sample.velocity,
        round_robin=sample.round_robin,
        audio=sample.audio,
        loop_points=None,
        analysis=env.to_dict(),
        metadata=sample.metadata,
    )


def _analyze_one(sample: Sample, config: AnalysisConfig) -> Sample:
    audio = sample.audio
    analysis = dict(sample.analysis)

    # An authored loop (from a library WAV's smpl chunk) is ground truth: use it verbatim,
    # skip loop detection, and skip the crossfade (done later in _bake_sample_loop). Its
    # frames are raw-file indices, so trimming must not cut into it and must rebase it.
    authored_loop = sample.loop_points if sample.metadata.get("authored_loop") else None

    # note_off is an index into the captured audio; trimming leading silence shifts it.
    note_off = sample.note_off
    metadata = sample.metadata
    lead = 0
    if config.trim:
        frames = audio.num_frames
        lead, trail = trim_bounds(audio)
        if authored_loop is not None:
            # Never trim into the author's loop: keep everything from its start to its end.
            lead = min(lead, authored_loop[0])
            trail = max(trail, authored_loop[1])
        if lead != 0 or trail != frames:
            # The audio is no longer what the source file holds, so it can no longer be
            # copied onto the card verbatim (see _common.py:write_sample_wav).
            metadata = {**metadata, "audio_verbatim": False}
        audio = AudioBuffer(data=audio.data[:, lead:trail], sample_rate=audio.sample_rate)
        if note_off is not None:
            note_off -= lead

    env = analyze_envelope(audio, bpm=config.tempo_bpm, note_off=note_off)
    analysis.update(env.to_dict())

    if config.pitch_verify:
        result = verify_pitch(audio, sample.note, config.pitch_tolerance_cents)
        analysis.update(result)

    # Start from no loop: the only samples that arrive with loop_points already set are
    # authored library WAVs, and those raw-file indices are invalid until rebased below
    # (and must be dropped entirely if looping is disabled for this set).
    loop_points = None
    if config.loop and authored_loop is not None:
        # Trust the author's loop: rebase raw-file frames onto the trimmed audio.
        loop_points = (authored_loop[0] - lead, authored_loop[1] - lead)
        analysis["loop_source"] = "authored_smpl"
        analysis["loop_quality"] = None
    elif config.loop and not config.loop_detect:
        # Detection disabled and the source carries no loop of its own: the author shipped
        # this file as a one-shot, so it ships as one. Nothing is derived, nothing is faded
        # into the audio — the whole point of `loop_detect: false` (see AnalysisConfig).
        analysis["loop_quality"] = None
        analysis["loop_warning"] = "no_authored_loop"
    elif config.loop:
        loop_cands = find_loop_candidates(audio, config.loop_quality_threshold, config.tempo_bpm, envelope=env, midi_note=sample.note)
        best_quality = loop_cands[0][1] if loop_cands else 0.0
        loop_found = False
        mono = audio.to_mono()
        for (s, e), quality in loop_cands:
            # Validate on the RAW (pre-crossfade) audio. The loop crossfade manufactures a
            # smooth seam by construction (it fades mono[end-1] to audio[start-1]), so a
            # post-crossfade seam check is blind — it passes even a loop whose mid-crossfade
            # phase-cancels. The splice quality we care about (start and end on the same
            # waveform phase, so the blend stays in phase) is intrinsic to the loop points.
            fail = validate_splice_reason(mono, s, e)
            if not fail:
                loop_points = (s, e)
                analysis["loop_quality"] = round(quality, 3)
                loop_found = True
                break
            log.warning(f"Splice validation failed ({s}, {e}): {fail} — trying next candidate")
        # A validated candidate shorter than the ranking floor is a micro-loop — a high note
        # with a short sustain can yield only a tiny clean candidate (e.g. 0.1s on a 1.3s
        # sustain), which sounds worse than a longer loop. Fall through to the central fallback
        # whenever no long-enough candidate was found, and adopt it only if it is genuinely
        # longer than whatever we have.
        chosen_len = (loop_points[1] - loop_points[0]) if loop_found and loop_points else 0
        if not loop_found or chosen_len < _MIN_LOOP_RANK_S * audio.sample_rate:
            # Guaranteed fallback when loop is enabled: loop the central 50% of the sustain
            # region. The config's loop flag is ground truth — if it says loop, every note
            # gets one (partial multisample presets are unplayable), regardless of the
            # per-sample envelope pluck classification. central_fallback_loop snaps the length
            # to whole periods and phase-locks the end so even this last resort can't
            # phase-cancel, and returns None only when the sustain region is physically too
            # short to loop. (Crossfade baked later, see Fix #1.)
            fb = central_fallback_loop(audio, env.sustain_start, env.sustain_end, sample.note)
            if fb is not None and (fb[1] - fb[0]) > chosen_len:
                loop_points = fb
                analysis["loop_quality"] = round(best_quality, 3) if loop_cands else 0.0
                analysis["loop_warning"] = "fallback_central_region"
                loop_found = True
            if not loop_found:
                if loop_cands:
                    analysis["loop_quality"] = round(best_quality, 3)
                    analysis["loop_warning"] = "splice_failed"
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
        metadata=metadata,
    )


def _bake_sample_loop(sample: Sample, crossfade_ms: float | None, bake: bool = True) -> Sample:
    """Bake the loop crossfade for a sample that kept its loop after the set-level vote.

    `bake=False` works out the length and records it, but leaves the audio untouched —
    for an exporter whose device crossfades the seam itself at playback (the Bento's
    `loopfadeamt`), where baking would fade the same seam twice.

    Run once, after pluck classification (Fix #1), so a loop later stripped by a set-level
    "Pluck" vote never leaves a baked crossfade in the exported audio.

    `crossfade_ms` None (the default) means adaptive: the length is derived per note from the
    fundamental period and the raw seam discontinuity (analysis/loop.py:adaptive_crossfade_ms),
    which is right across the whole register where a fixed millisecond value isn't. A number
    forces that fixed length (escape hatch), keeping the original fallback-gets-2x behaviour.
    The chosen length is recorded in analysis["loop_crossfade_ms"] for inspection.
    """
    if sample.loop_points is None:
        return sample
    # An author-baked loop already loops cleanly by construction; a crossfade would only
    # smear the seam the author chose. Ship it verbatim.
    if sample.analysis.get("loop_source") == "authored_smpl":
        return sample
    fallback = sample.analysis.get("loop_warning") == "fallback_central_region"
    if crossfade_ms is None:
        ms = adaptive_crossfade_ms(
            sample.audio,
            sample.note,
            sample.loop_points,
            min_disc=_XFADE_FALLBACK_MIN_DISC if fallback else 0.0,
        )
    else:
        ms = crossfade_ms * 2 if fallback else crossfade_ms
    if ms <= 0:
        return sample
    analysis = {**sample.analysis, "loop_crossfade_ms": round(ms, 2)}
    if not bake:
        return dataclasses.replace(sample, analysis=analysis)
    start, end = sample.loop_points
    baked = bake_loop_crossfade(sample.audio, start, end, ms)
    return dataclasses.replace(
        sample,
        audio=baked,
        analysis=analysis,
        metadata={**sample.metadata, "audio_verbatim": False},
    )


def _map_samples(fn, samples, workers: int, level: int, desc: str) -> list[Sample]:
    """Apply ``fn`` to each sample and collect the results.

    At ``workers == 1`` this runs **in-process** — no subprocess, no ``fork`` — which
    is essential when a native plugin is resident in this process. ``scan-clap`` (and
    ``batch``/``sample`` on a VST/CLAP source) keeps Diva loaded with ~40 background
    threads while it calls the analysis per preset; forking a pool out of a
    multithreaded native process can inherit a plugin mutex that was locked by a
    parent thread absent in the child, deadlocking both sides in ``futex_wait``
    forever. That is the intermittent "hangs partway through the bank, always around
    the same spot" failure. In-process is also simply faster here: no process spawn
    and no per-sample ``AudioBuffer`` pickling, and ``workers=1`` was never parallel.

    At ``workers > 1`` fan the work out to a ``ProcessPoolExecutor`` as before (used
    by callers that pass an explicit ``--workers`` and whose source isn't a live
    plugin, e.g. library analysis).
    """
    if workers == 1:
        out: list[Sample] = []
        with tqdm(total=len(samples), desc=desc, unit="sample", leave=False) as pbar:
            for s in samples:
                out.append(fn(s))
                pbar.update(1)
        return out

    out = []
    with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(level,)) as executor:
        futures = [executor.submit(fn, s) for s in samples]
        with tqdm(total=len(samples), desc=desc, unit="sample", leave=False) as pbar:
            for future in as_completed(futures):
                out.append(future.result())
                pbar.update(1)
    return out


def analyze_sampleset(
    sset: SampleSet, config: AnalysisConfig, workers: int = 1, bake_crossfade: bool = True
) -> SampleSet:
    actual_tempo = config.tempo_bpm or sset.tempo_bpm
    loop_tempo = actual_tempo if config.loop_use_tempo else None
    level = logging.getLogger().getEffectiveLevel()

    fn = partial(_analyze_one, config=dataclasses.replace(config, tempo_bpm=loop_tempo))
    analyzed = _map_samples(fn, sset.samples, workers, level, "Analyzing")

    if config.pitch_verify:
        for sample in analyzed:
            if not sample.analysis.get("pitch_ok", True):
                cents = sample.analysis.get("cents_diff", "?")
                tqdm.write(f"  WARNING note {sample.note}: pitch mismatch ({cents} cents off)")

    sound_type = _sound_type_string(analyzed, actual_tempo)
    log.info(f"Sound type: {sound_type}")

    # The config's loop flag is ground truth (set by the scan profile, or hand-edited). We no
    # longer strip loops when the set votes "Pluck" — that override silently discarded loops a
    # loop:true config asked for. Instead just warn so the user can set profile: pluck if the
    # preset really shouldn't loop.
    # Only worth saying when a loop could actually have been derived: with `loop_detect`
    # off the loops are whatever the author shipped, and "set profile: pluck" is advice
    # about a decision the pipeline is no longer making.
    if sound_type == "Pluck" and config.loop and config.loop_detect:
        n_pluck = sum(1 for s in analyzed if s.analysis.get("classification") == "pluck")
        log.warning(
            f"{n_pluck}/{len(analyzed)} samples classify as pluck but loop is enabled — "
            f"set profile: pluck (or analysis.loop: false) if this preset should not loop."
        )

    # Bake the loop crossfade now that the set-level pluck vote is final — samples whose
    # loops were just stripped keep their clean trimmed audio (Fix #1).
    if config.loop:
        analyzed = [_bake_sample_loop(s, config.loop_crossfade_ms, bake_crossfade) for s in analyzed]

    if config.loop:
        samples_with_loop = sum(1 for s in analyzed if s.loop_points is not None)
        if 0 < samples_with_loop < len(analyzed):
            found = sorted(
                f"{_note_name(s.note)}({s.analysis.get('loop_quality', '?')})"
                for s in analyzed if s.loop_points is not None
            )
            missing = sorted(
                f"{_note_name(s.note)}({s.analysis.get('loop_quality') or 0:.2f})"
                for s in analyzed if s.loop_points is None
            )
            if config.loop_detect:
                log.warning(
                    f"Loop detection partial ({samples_with_loop}/{len(analyzed)} samples) — keeping loops where found."
                )
                log.warning(f"  Found:   {' '.join(found)}")
                log.warning(f"  Missing: {' '.join(missing)}")
            else:
                # Not a partial result: the author looped these notes and not those. Worth
                # seeing, not worth a warning.
                log.info(
                    f"Authored loops on {samples_with_loop}/{len(analyzed)} samples "
                    f"(the rest ship as one-shots, as the source library has them)."
                )

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
        tempo_bpm=actual_tempo,
    )

    if config.normalize == "per_sample":
        result = SampleSet(
            name=result.name,
            category=result.category,
            samples=[normalize_sample(s) for s in result.samples],
            source_metadata=result.source_metadata,
            tempo_bpm=actual_tempo,
        )
    elif config.normalize == "per_set":
        result = normalize_set(result)

    return result


def classify_sampleset(sset: SampleSet, tempo_bpm: float | None = None, workers: int = 1) -> str:
    """Run envelope analysis only on all samples and return the sound type string."""
    level = logging.getLogger().getEffectiveLevel()

    fn = partial(_envelope_only, bpm=tempo_bpm)
    analyzed = _map_samples(fn, sset.samples, workers, level, "Classifying")

    return _sound_type_string(analyzed, tempo_bpm)

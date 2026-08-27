import logging
from dataclasses import replace
from pathlib import Path

import soundfile as sf

from ..analysis.normalize import normalize_sample
from ..analysis.pipeline import analyze_sampleset, classify_sampleset
from ..analysis.trim import trim_silence
from ..config.schema import (
    BitwigSourceConfig,
    CLAPSourceConfig,
    LibrarySourceConfig,
    RunConfig,
    VSTSourceConfig,
    WavetableSourceConfig,
)
from ..io.adapters.bitwig import BitwigAdapter
from ..io.adapters.clap import CLAPAdapter
from ..io.adapters.library import LibraryAdapter
from ..io.adapters.vst import VSTAdapter
from ..io.adapters.wavetable import WavetableAdapter
from ..io.exporters import get_exporter

log = logging.getLogger(__name__)


def _source_location(source) -> str:
    """Best human pointer at where a source's audio was supposed to come from."""
    for attr in ("path", "preset", "preset_path"):
        value = getattr(source, attr, None)
        if value:
            return str(value)
    return type(source).__name__


def notes_to_capture(config: RunConfig, output_format: str) -> list[int]:
    """The part of a plugin config's note grid the target format will actually ship.

    Formats differ in how much of a melodic capture they can use: the Deluge maps
    every note to its own keyzone, a .pti has no keyzones at all and ships one
    repitched sample. Rendering the full grid for the latter throws away all but one
    capture, so the exporter is asked up front rather than after the renders are paid
    for. Each exporter answers for itself (`notes_used`), and picks the same note
    again at export time, so this can't select a note the export then ignores.

    Declaring `notes_used` is optional: an exporter that doesn't gets the full grid,
    which is never wrong, only slower.
    """
    lo, hi = config.capture.note_range
    notes = list(range(lo, hi + 1, config.capture.note_step))
    notes_used = getattr(get_exporter(output_format), "notes_used", None)
    return list(notes_used(notes)) if notes_used is not None else notes


def run(config: RunConfig, output_path: Path, output_format: str, workers: int = 1, progress=None) -> Path:
    if isinstance(config.source, WavetableSourceConfig):
        # A wavetable isn't a captured performance — it ships to the SD card
        # byte-for-byte, so skip analyze_sampleset (trim/envelope/loop/normalize)
        # entirely and go straight to export. See io/adapters/wavetable.py.
        adapter = WavetableAdapter(config.source)
        sset = adapter.capture(name=config.name or None)
        sset.source_metadata["wavetable"] = config.wavetable
        exporter_cls = get_exporter(output_format)
        if progress is not None:
            progress.update(1)
        return exporter_cls().export(sset, config.output, output_path)

    if isinstance(config.source, VSTSourceConfig):
        log.debug(f"{config.source.plugin.name} - {config.source.preset}")
        adapter = VSTAdapter(config.source)
        sset = adapter.capture(
            config.capture,
            name=config.name or None,
            progress=progress,
            notes=notes_to_capture(config, output_format),
        )
        analysis = replace(
            config.analysis,
            pitch_verify=False,
            tempo_bpm=config.analysis.tempo_bpm or config.capture.tempo_bpm,
        )
    elif isinstance(config.source, CLAPSourceConfig):
        log.debug(f"{config.source.plugin.name} - {config.source.preset}")
        adapter = CLAPAdapter(config.source)
        sset = adapter.capture(
            config.capture,
            name=config.name or None,
            progress=progress,
            notes=notes_to_capture(config, output_format),
        )
        analysis = replace(
            config.analysis,
            pitch_verify=False,
            tempo_bpm=config.analysis.tempo_bpm or config.capture.tempo_bpm,
        )
    elif isinstance(config.source, (LibrarySourceConfig, BitwigSourceConfig)):
        adapter = (
            BitwigAdapter(config.source)
            if isinstance(config.source, BitwigSourceConfig)
            else LibraryAdapter(config.source)
        )
        sset = adapter.capture(
            name=config.name or None,
            max_round_robins=config.capture.round_robins,
            note_step=config.capture.note_step,
            progress=progress,
        )
        analysis = config.analysis
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    if not sset.samples:
        # Every downstream stage assumes at least one sample; without this the
        # first thing to divide by len(samples) is what surfaces, which says
        # nothing about the actual cause (a library folder whose filenames carry
        # no parseable note, a source path that no longer exists, an empty dir).
        raise ValueError(
            f"{config.output.name}: source produced no samples "
            f"({_source_location(config.source)}) — nothing to analyse. For a library "
            f"folder this usually means no filename carries a note+octave, so it is a "
            f"bag of loops/one-shots rather than a multisample."
        )

    log.debug("Analyze Sampleset")
    exporter_cls = get_exporter(output_format)
    # An exporter whose device crossfades the loop seam itself declares it, and the
    # analysis leaves the audio alone rather than fading the same seam twice. The
    # length is still worked out and recorded, so the exporter can hand the device a
    # number. Same shape as `notes_used` — optional, absent means "bake it".
    bakes = getattr(exporter_cls, "bakes_loop_crossfade", None)
    sset = analyze_sampleset(
        sset, analysis, workers=workers, bake_crossfade=True if bakes is None else bakes()
    )

    return exporter_cls().export(sset, config.output, output_path)


def classify(config: RunConfig, workers: int = 1, save_path: Path | None = None) -> str:
    if isinstance(config.source, (VSTSourceConfig, CLAPSourceConfig)):
        log.debug(f"{config.source.plugin.name} - {config.source.preset}")
        adapter = VSTAdapter(config.source) if isinstance(config.source, VSTSourceConfig) else CLAPAdapter(config.source)
        sset = adapter.capture(config.capture, name=config.name or None)
        tempo_bpm = config.analysis.tempo_bpm or config.capture.tempo_bpm
    elif isinstance(config.source, (LibrarySourceConfig, BitwigSourceConfig)):
        adapter = (
            BitwigAdapter(config.source)
            if isinstance(config.source, BitwigSourceConfig)
            else LibraryAdapter(config.source)
        )
        sset = adapter.capture(
            name=config.name or None,
            max_round_robins=config.capture.round_robins,
            note_step=config.capture.note_step,
        )
        tempo_bpm = config.analysis.tempo_bpm
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    if save_path is not None:
        safe_name = config.output.name.replace("/", "_").replace("\\", "_")
        dest = save_path / safe_name
        dest.mkdir(parents=True, exist_ok=True)
        for s in sset.samples:
            audio = normalize_sample(s).audio
            audio = trim_silence(audio)
            name = f"n{s.note:03d}_v{s.velocity:03d}_rr{s.round_robin:02d}.wav"
            sf.write(str(dest / name), audio.data.T, audio.sample_rate, subtype="FLOAT")

    return classify_sampleset(sset, tempo_bpm=tempo_bpm, workers=workers)

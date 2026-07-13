import logging
from dataclasses import replace
from pathlib import Path

import soundfile as sf

from ..analysis.normalize import normalize_sample
from ..analysis.pipeline import analyze_sampleset, classify_sampleset
from ..analysis.trim import trim_silence
from ..config.schema import (
    CLAPSourceConfig,
    LibrarySourceConfig,
    RunConfig,
    VSTSourceConfig,
    WavetableSourceConfig,
)
from ..io.adapters.clap import CLAPAdapter
from ..io.adapters.library import LibraryAdapter
from ..io.adapters.vst import VSTAdapter
from ..io.adapters.wavetable import WavetableAdapter
from ..io.exporters import get_exporter

log = logging.getLogger(__name__)


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
        sset = adapter.capture(config.capture, name=config.name or None, progress=progress)
        analysis = replace(
            config.analysis,
            pitch_verify=False,
            tempo_bpm=config.analysis.tempo_bpm or config.capture.tempo_bpm,
        )
    elif isinstance(config.source, CLAPSourceConfig):
        log.debug(f"{config.source.plugin.name} - {config.source.preset}")
        adapter = CLAPAdapter(config.source)
        sset = adapter.capture(config.capture, name=config.name or None, progress=progress)
        analysis = replace(
            config.analysis,
            pitch_verify=False,
            tempo_bpm=config.analysis.tempo_bpm or config.capture.tempo_bpm,
        )
    elif isinstance(config.source, LibrarySourceConfig):
        adapter = LibraryAdapter(config.source)
        sset = adapter.capture(
            name=config.name or None,
            max_round_robins=config.capture.round_robins,
            note_step=config.capture.note_step,
            progress=progress,
        )
        analysis = config.analysis
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    log.debug("Analyze Sampleset")
    sset = analyze_sampleset(sset, analysis, workers=workers)

    return get_exporter(output_format)().export(sset, config.output, output_path)


def classify(config: RunConfig, workers: int = 1, save_path: Path | None = None) -> str:
    if isinstance(config.source, (VSTSourceConfig, CLAPSourceConfig)):
        log.debug(f"{config.source.plugin.name} - {config.source.preset}")
        adapter = VSTAdapter(config.source) if isinstance(config.source, VSTSourceConfig) else CLAPAdapter(config.source)
        sset = adapter.capture(config.capture, name=config.name or None)
        tempo_bpm = config.analysis.tempo_bpm or config.capture.tempo_bpm
    elif isinstance(config.source, LibrarySourceConfig):
        adapter = LibraryAdapter(config.source)
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

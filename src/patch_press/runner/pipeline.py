import logging
from dataclasses import replace
from pathlib import Path

import soundfile as sf

from ..analysis.normalize import normalize_sample
from ..analysis.pipeline import analyze_sampleset, classify_sampleset
from ..analysis.trim import trim_silence
from ..config.schema import CLAPSourceConfig, LibrarySourceConfig, RunConfig, VSTSourceConfig
from ..io.adapters.clap import CLAPAdapter
from ..io.adapters.library import LibraryAdapter
from ..io.adapters.vst import VSTAdapter
from ..io.exporters.deluge import DelugeExporter

log = logging.getLogger(__name__)

_EXPORTERS = {
    "deluge": DelugeExporter,
}


def run(config: RunConfig, output_path: Path, output_format: str, workers: int = 1, progress=None) -> Path:
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
        )
        analysis = config.analysis
        # Library capture reads files rather than rendering note-by-note, so advance the shared
        # batch bar in one step by the number of samples loaded.
        if progress is not None:
            progress.update(len(sset.samples))
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    log.debug("Analyze Sampleset")
    sset = analyze_sampleset(sset, analysis, workers=workers)

    exporter_cls = _EXPORTERS.get(output_format)
    if exporter_cls is None:
        raise ValueError(f"Unknown output format: {output_format!r}. Available: {list(_EXPORTERS)}")

    return exporter_cls().export(sset, config.output, output_path)


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

import logging
from dataclasses import replace
from pathlib import Path

from ..analysis.pipeline import analyze_sampleset
from ..config.schema import LibrarySourceConfig, RunConfig, VSTSourceConfig
from ..io.adapters.library import LibraryAdapter
from ..io.adapters.vst import VSTAdapter
from ..io.exporters.deluge import DelugeExporter

log = logging.getLogger(__name__)

_EXPORTERS = {
    "deluge": DelugeExporter,
}


def run(config: RunConfig, output_path: Path, output_format: str, workers: int = 1) -> Path:
    if isinstance(config.source, VSTSourceConfig):
        logging.debug(f"{config.source.plugin.name} - {config.source.preset}")
        logging.debug("Capturing")
        adapter = VSTAdapter(config.source)
        sset = adapter.capture(config.capture, name=config.name or None)
        analysis = replace(
            config.analysis,
            pitch_verify=False,
            tempo_bpm=config.analysis.tempo_bpm or config.capture.tempo_bpm,
        )
    elif isinstance(config.source, LibrarySourceConfig):
        adapter = LibraryAdapter(config.source)
        sset = adapter.capture(name=config.name or None, max_round_robins=config.capture.round_robins, note_step=config.capture.note_step)
        analysis = config.analysis
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    logging.debug("Analyze Sampleset")
    sset = analyze_sampleset(sset, analysis, workers=workers)

    exporter_cls = _EXPORTERS.get(output_format)
    if exporter_cls is None:
        raise ValueError(
            f"Unknown output format: {output_format!r}. "
            f"Available: {list(_EXPORTERS)}"
        )

    return exporter_cls().export(sset, config.output, output_path)

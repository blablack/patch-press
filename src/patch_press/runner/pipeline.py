from dataclasses import replace
from pathlib import Path

from ..analysis.pipeline import analyze_sampleset
from ..config.schema import LibrarySourceConfig, RunConfig, VSTSourceConfig
from ..io.adapters.library import LibraryAdapter
from ..io.adapters.vst import VSTAdapter
from ..io.exporters.deluge import DelugeExporter

_EXPORTERS = {
    "deluge": DelugeExporter,
}


def run(config: RunConfig) -> Path:
    if isinstance(config.source, VSTSourceConfig):
        adapter = VSTAdapter(config.source)
        sset = adapter.capture(config.capture, name=config.name or None)
        analysis = replace(config.analysis, pitch_verify=False)
    elif isinstance(config.source, LibrarySourceConfig):
        adapter = LibraryAdapter(config.source)
        sset = adapter.capture(name=config.name or None)
        analysis = config.analysis
    else:
        raise TypeError(f"Unknown source config type: {type(config.source)}")

    sset = analyze_sampleset(sset, analysis)

    exporter_cls = _EXPORTERS.get(config.output.format)
    if exporter_cls is None:
        raise ValueError(
            f"Unknown output format: {config.output.format!r}. "
            f"Available: {list(_EXPORTERS)}"
        )

    return exporter_cls().export(sset, config.output)

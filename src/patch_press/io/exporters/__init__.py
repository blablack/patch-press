from .bento import BentoExporter
from .deluge import DelugeExporter

EXPORTERS = {
    "deluge": DelugeExporter,
    "bento": BentoExporter,
}


def get_exporter(name: str):
    try:
        return EXPORTERS[name]
    except KeyError:
        raise ValueError(f"Unknown output format: {name!r}. Available: {list(EXPORTERS)}") from None


__all__ = ["BentoExporter", "DelugeExporter", "EXPORTERS", "get_exporter"]

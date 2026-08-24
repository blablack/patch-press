from .bento import BentoExporter
from .deluge import DelugeExporter
from .polyend import PolyendExporter

EXPORTERS = {
    "deluge": DelugeExporter,
    "pti": PolyendExporter,
    "bento": BentoExporter,
}


def get_exporter(name: str):
    try:
        return EXPORTERS[name]
    except KeyError:
        raise ValueError(f"Unknown output format: {name!r}. Available: {list(EXPORTERS)}") from None


__all__ = ["BentoExporter", "DelugeExporter", "PolyendExporter", "EXPORTERS", "get_exporter"]

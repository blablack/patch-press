import logging
from pathlib import Path

from ..config.loader import load_config
from .pipeline import run

log = logging.getLogger(__name__)


def run_batch(
    config_paths: list[Path],
    output_path: Path,
    output_format: str,
    workers: int = 1,
    skip_existing: bool = True,
) -> dict[Path, str | Exception]:
    results: dict[Path, str | Exception] = {}

    for cfg_path in config_paths:
        try:
            config = load_config(cfg_path)
            expected = output_path / f"{config.output.name}.xml"
            if skip_existing and expected.exists():
                results[cfg_path] = "skipped"
                log.info("SKIP   %s", cfg_path)
                continue
            output = run(config, output_path, output_format, workers=workers)
            results[cfg_path] = str(output)
            log.info("OK     %s → %s", cfg_path, output)
        except Exception as exc:
            results[cfg_path] = exc
            log.error("ERROR  %s: %s", cfg_path, exc)

    return results

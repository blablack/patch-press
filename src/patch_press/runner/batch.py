import logging
from pathlib import Path

from tqdm import tqdm

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

    for cfg_path in tqdm(config_paths, desc="Batch", unit="preset"):
        try:
            config = load_config(cfg_path)
            expected = output_path / f"{config.output.name.strip()}.xml"
            if skip_existing and expected.exists():
                results[cfg_path] = "skipped"
                tqdm.write(f"SKIP   {cfg_path.stem}")
                continue
            output = run(config, output_path, output_format, workers=workers)
            results[cfg_path] = str(output)
            tqdm.write(f"OK     {cfg_path.stem} → {output}")
        except Exception as exc:
            results[cfg_path] = exc
            tqdm.write(f"ERROR  {cfg_path.stem}: {exc}")
            log.debug("", exc_info=exc)

    return results

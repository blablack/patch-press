import sys
from pathlib import Path

from ..config.loader import load_config
from .pipeline import run


def run_batch(
    config_paths: list[Path],
    skip_existing: bool = True,
) -> dict[Path, str | Exception]:
    results: dict[Path, str | Exception] = {}

    for cfg_path in config_paths:
        try:
            config = load_config(cfg_path)
            expected = config.output.path / f"{config.output.name}.xml"
            if skip_existing and expected.exists():
                results[cfg_path] = "skipped"
                print(f"SKIP   {cfg_path}")
                continue
            output = run(config)
            results[cfg_path] = str(output)
            print(f"OK     {cfg_path} → {output}")
        except Exception as exc:
            results[cfg_path] = exc
            print(f"ERROR  {cfg_path}: {exc}", file=sys.stderr)

    return results

import logging
from pathlib import Path

from ..progress import ProgressBar as tqdm
from ..config.loader import load_config
from ..config.schema import CLAPSourceConfig, LibrarySourceConfig, RunConfig, VSTSourceConfig
from .pipeline import run

log = logging.getLogger(__name__)


def _expected_notes(config: RunConfig) -> int:
    """Best-effort count of samples a config will capture, for the shared progress total.

    Exact for VST/CLAP (the rendered note grid — the slow phase the bar tracks). For library
    sources it's an estimate from the file/subdir count, reconciled when the samples actually
    load; library capture is fast, so a small mismatch is only cosmetic.
    """
    cap = config.capture
    src = config.source
    if isinstance(src, (VSTSourceConfig, CLAPSourceConfig)):
        lo, hi = cap.note_range
        return len(range(lo, hi + 1, cap.note_step)) * len(cap.velocities) * cap.round_robins
    if isinstance(src, LibrarySourceConfig):
        try:
            wavs = list(src.path.glob("*.wav"))
            return len(wavs) or sum(1 for d in src.path.iterdir() if d.is_dir())
        except OSError:
            return 0
    return 0


def run_batch(
    config_paths: list[Path],
    output_path: Path,
    output_format: str,
    workers: int = 1,
    skip_existing: bool = True,
) -> dict[Path, str | Exception]:
    results: dict[Path, str | Exception] = {}

    # Pre-pass: load every config and decide skips up front so the shared bar's total is the
    # number of notes actually about to be captured (skipped presets contribute none).
    plan: list[tuple[Path, RunConfig | None, object]] = []  # (path, config, state)
    total_notes = 0
    for cfg_path in config_paths:
        try:
            config = load_config(cfg_path)
        except Exception as exc:
            plan.append((cfg_path, None, exc))
            continue
        safe_name = config.output.name.strip().replace("/", "_").replace("\\", "_")
        expected = output_path.parent / "SYNTHS" / output_path.name / f"{safe_name}.xml"
        if skip_existing and expected.exists():
            plan.append((cfg_path, config, "skip"))
            continue
        plan.append((cfg_path, config, None))
        total_notes += _expected_notes(config)

    with tqdm(total=total_notes, desc=f"Batch {output_path.name}", unit="note") as bar:
        for cfg_path, config, state in plan:
            if isinstance(state, Exception):
                results[cfg_path] = state
                tqdm.write(f"ERROR  {cfg_path.stem}: {state}")
                log.debug("", exc_info=state)
                continue
            if state == "skip":
                results[cfg_path] = "skipped"
                tqdm.write(f"SKIP   {cfg_path.stem}")
                continue
            bar.set_postfix_str(cfg_path.stem)
            try:
                output = run(config, output_path, output_format, workers=workers, progress=bar)
                results[cfg_path] = str(output)
                tqdm.write(f"OK     {cfg_path.stem} → {output}")
            except Exception as exc:
                results[cfg_path] = exc
                tqdm.write(f"ERROR  {cfg_path.stem}: {exc}")
                log.debug("", exc_info=exc)

    return results

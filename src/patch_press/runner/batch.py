import logging
from pathlib import Path

from ..progress import ProgressBar as tqdm
from ..config.loader import load_config
from ..config.schema import (
    BitwigSourceConfig,
    CLAPSourceConfig,
    LibrarySourceConfig,
    RunConfig,
    VSTSourceConfig,
    WavetableSourceConfig,
)
from ..io.adapters.bitwig import BitwigAdapter
from ..io.adapters.library import LibraryAdapter
from ..io.exporters import get_exporter
from .pipeline import keeps_velocity_layers, notes_to_capture, run

log = logging.getLogger(__name__)


def _expected_notes(config: RunConfig, output_format: str) -> int:
    """Exact count of samples a config will capture, for the shared progress total.

    VST/CLAP: the rendered note grid (the slow phase the bar tracks), narrowed the same
    way `run()` narrows it — a format that ships one note only renders one. Library:
    mirrors the adapter's note-thinning + round-robin-capping so the total matches what
    `capture()` actually produces.
    """
    cap = config.capture
    src = config.source
    if isinstance(src, (VSTSourceConfig, CLAPSourceConfig)):
        return len(notes_to_capture(config, output_format)) * len(cap.velocities) * cap.round_robins
    if isinstance(src, LibrarySourceConfig):
        try:
            return LibraryAdapter(src).expected_count(cap.round_robins, cap.note_step)
        except OSError:
            return 0
    if isinstance(src, BitwigSourceConfig):
        try:
            return BitwigAdapter(src).expected_count(
                cap.round_robins, cap.note_step, keeps_velocity_layers(output_format)
            )
        except (OSError, ValueError):
            return 0
    if isinstance(src, WavetableSourceConfig):
        # scan_wavetables emits one config per file; each contributes exactly one
        # `progress.update(1)` from runner/pipeline.py's wavetable branch.
        return 1
    return 0


def _warn_on_output_collisions(plan, exporter_cls, output_path: Path) -> None:
    """Report configs in this batch that would write to the same place.

    Only a batch sees the whole set, so this is the one spot that can catch it. It
    matters most for the Bento, whose patches all live in one flat directory and get
    a shortened folder name: two source folders that shorten the same way would have
    the second preset overwrite the first, or — with skip-existing on — be silently
    skipped as "already built". Reported rather than raised, since the run still
    produces a valid card for every preset that isn't part of a clash.
    """
    claimed: dict[Path, list[Path]] = {}
    for cfg_path, config, state in plan:
        if config is None:
            continue
        for out in exporter_cls.expected_outputs(config.output, output_path):
            claimed.setdefault(out, []).append(cfg_path)
    for out, sources in claimed.items():
        if len(sources) > 1:
            log.warning(
                "%s: %d configs map to the same output — %s. Only one will survive; "
                "rename one preset or its source folder.",
                out.parent.name, len(sources), ", ".join(p.stem for p in sources),
            )


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
    exporter_cls = get_exporter(output_format)
    plan: list[tuple[Path, RunConfig | None, object]] = []  # (path, config, state)
    total_notes = 0
    for cfg_path in config_paths:
        try:
            config = load_config(cfg_path)
        except Exception as exc:
            plan.append((cfg_path, None, exc))
            continue
        if skip_existing and any(p.exists() for p in exporter_cls.expected_outputs(config.output, output_path)):
            plan.append((cfg_path, config, "skip"))
            continue
        plan.append((cfg_path, config, None))
        total_notes += _expected_notes(config, output_format)

    _warn_on_output_collisions(plan, exporter_cls, output_path)

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

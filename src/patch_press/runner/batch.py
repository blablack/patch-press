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


def _check_output_collisions(plan, exporter_cls, output_path: Path) -> None:
    """Refuse a batch in which two configs would write to the same place.

    Only a batch sees the whole set, so this is the one spot that can catch it. It
    matters most for the Bento, whose patches all live in one flat directory under a
    shortened folder name (`_fit_max_name`): two source folders that shorten the same
    way would have the second preset overwrite the first, or — with skip-existing on —
    be silently skipped as "already built". Either way a preset is lost without a
    trace, so this raises before anything is built rather than warning: a warning in
    a few thousand lines of batch output is how the loss went unnoticed once.
    """
    claimed: dict[Path, list[Path]] = {}
    for cfg_path, config, state in plan:
        if config is None:
            continue
        for out in exporter_cls.expected_outputs(config.output, output_path):
            claimed.setdefault(out, []).append(cfg_path)
    # One config lists a candidate per patch type (Bento: SampInst/OneShots/Wavetable),
    # all under the same folder name, so report each clash once by that name.
    clashes = sorted({(out.parent.name, tuple(srcs)) for out, srcs in claimed.items() if len(srcs) > 1})
    if clashes:
        lines = [f"  {name!r} <- " + ", ".join(str(p) for p in srcs) for name, srcs in clashes]
        raise ValueError(
            f"{len(clashes)} output folder(s) claimed by more than one config -- only one would survive; "
            "give one of them a distinct output.folder (or output.name):\n" + "\n".join(lines)
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
    loaded: list[tuple[Path, RunConfig | None, Exception | None]] = []
    for cfg_path in config_paths:
        try:
            loaded.append((cfg_path, load_config(cfg_path), None))
        except Exception as exc:
            loaded.append((cfg_path, None, exc))

    # A format that shortens output names (Bento) names the whole set at once, so two
    # presets that shorten alike are told apart before anyone asks where they go.
    # Configs that already carry a folder keep it — a driver that runs this batch on
    # a slice of a larger corpus has named the corpus itself and persisted it there.
    assign = getattr(exporter_cls, "assign_output_folders", None)
    if assign is not None:
        assign([(config.output, output_path) for _, config, _ in loaded if config is not None])

    plan: list[tuple[Path, RunConfig | None, object]] = []  # (path, config, state)
    total_notes = 0
    for cfg_path, config, exc in loaded:
        if config is None:
            plan.append((cfg_path, None, exc))
            continue
        if skip_existing and any(p.exists() for p in exporter_cls.expected_outputs(config.output, output_path)):
            plan.append((cfg_path, config, "skip"))
            continue
        plan.append((cfg_path, config, None))
        total_notes += _expected_notes(config, output_format)

    _check_output_collisions(plan, exporter_cls, output_path)

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

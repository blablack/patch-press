import argparse
import logging
import os
import sys
from pathlib import Path

import yaml

from ..progress import ProgressBar as tqdm


class _TqdmLoggingHandler(logging.StreamHandler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            tqdm.write(self.format(record))
        except Exception:
            self.handleError(record)

from ..analysis.pipeline import suppress_noisy_loggers
from ..config.loader import load_config
from ..profiles import available_profiles
from ..runner.batch import run_batch
from ..runner.pipeline import classify as classify_run
from ..runner.pipeline import run
from ..runner.scan import (
    DEFAULT_DURATION_S,
    DEFAULT_NOTE_STEP,
    note_name_to_midi,
    scan_clap,
    scan_from_probe,
    scan_kit_assemble,
    scan_library,
    scan_oneshots,
    scan_wavetables,
)


def _note_step_arg(s: str) -> int:
    v = int(s)
    if v < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {v}")
    return v


def _duration_arg(s: str) -> float:
    v = float(s)
    if v <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {v}")
    return v


def _note_arg(s: str) -> int:
    try:
        return note_name_to_midi(s)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc))


def cmd_sample(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    output = run(config, args.path, args.format, workers=args.workers)
    print(f"→ {output}")


def cmd_batch(args: argparse.Namespace) -> None:
    configs: list[Path] = []
    for pattern in args.configs:
        found = sorted(Path(".").glob(pattern))
        if not found:
            # treat as literal path
            found = [Path(pattern)]
        configs.extend(found)

    if args.only_profile:
        def _profile(p: Path) -> str | None:
            try:
                return yaml.safe_load(p.read_text()).get("profile")
            except Exception:
                return None
        before = len(configs)
        configs = [p for p in configs if _profile(p) == args.only_profile]
        print(f"Profile filter '{args.only_profile}': {len(configs)}/{before} configs kept.")

    if not configs:
        print("No config files found.", file=sys.stderr)
        sys.exit(1)

    results = run_batch(configs, output_path=args.path, output_format=args.format, workers=args.workers, skip_existing=not args.no_skip)
    errors = [p for p, r in results.items() if isinstance(r, Exception)]
    if errors:
        print(f"\n{len(errors)} error(s) — see above.", file=sys.stderr)
        sys.exit(1)


def cmd_scan_from_probe(args: argparse.Namespace) -> None:
    summary = scan_from_probe(
        probe_dir=args.probe_dir,
        config_dir=args.config_dir,
        profile=args.profile,
        probe_note=args.probe_note,
        probe_velocity=args.probe_velocity,
        note_step=args.note_step,
        sustain_duration_s=args.duration,
        note_lo=args.start_note,
        note_hi=args.end_note,
        debug=args.debug,
        sample_rate=args.sample_rate,
        tempo_bpm=args.tempo_bpm,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )
    if summary.reviews:
        print(f"\n{len(summary.reviews)} preset(s) flagged for review:")
        for name, result in summary.reviews:
            detail = (
                ", ".join(result.flags)
                if result.flags
                else f"confidence={result.confidence}"
            )
            print(f"  - {name}: {detail}")


def cmd_scan_clap(args: argparse.Namespace) -> None:
    summary = scan_clap(
        plugin_path=args.plugin,
        preset_dir=args.preset_dir,
        config_dir=args.config_dir,
        profile=args.profile,
        probe_note=args.probe_note,
        probe_velocity=args.probe_velocity,
        note_step=args.note_step,
        sustain_duration_s=args.duration,
        note_lo=args.start_note,
        note_hi=args.end_note,
        debug=args.debug,
        sample_rate=args.sample_rate,
        tempo_bpm=args.tempo_bpm,
    )
    print(f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}")
    if summary.reviews:
        print(f"\n{len(summary.reviews)} preset(s) flagged for review:")
        for name, result in summary.reviews:
            detail = ", ".join(result.flags) if result.flags else f"confidence={result.confidence}"
            print(f"  - {name}: {detail}")


def cmd_scan_library(args: argparse.Namespace) -> None:
    summary = scan_library(
        library_path=args.library,
        config_dir=args.config_dir,
        library_type=args.type,
        profile=args.profile,
        note_step=args.note_step,
        note_lo=args.start_note,
        note_hi=args.end_note,
        debug=args.debug,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )


def cmd_scan_oneshots(args: argparse.Namespace) -> None:
    summary = scan_oneshots(
        folder=args.folder,
        config_dir=args.config_dir,
        profile=args.profile,
        debug=args.debug,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )
    if summary.reviews:
        print(f"\n{len(summary.reviews)} preset(s) flagged for review:")
        for name, result in summary.reviews:
            detail = ", ".join(result.flags) if result.flags else f"confidence={result.confidence}"
            print(f"  - {name}: {detail}")


def cmd_scan_wavetables(args: argparse.Namespace) -> None:
    summary = scan_wavetables(
        folder=args.folder,
        config_dir=args.config_dir,
        archetype=args.archetype,
        debug=args.debug,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )
    if summary.reviews:
        print(f"\n{len(summary.reviews)} preset(s) flagged for review:")
        for name, result in summary.reviews:
            detail = ", ".join(result.flags) if result.flags else f"confidence={result.confidence}"
            print(f"  - {name}: {detail}")


def cmd_assemble_kits(args: argparse.Namespace) -> None:
    summary = scan_kit_assemble(
        hits_root=args.folder,
        config_dir=args.config_dir,
        min_categories=args.min_categories,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )
    if summary.reviews:
        print(f"\n{len(summary.reviews)} kit(s) flagged for review:")
        for name, result in summary.reviews:
            detail = ", ".join(result.flags) if result.flags else f"confidence={result.confidence}"
            print(f"  - {name}: {detail}")


def cmd_classify(args: argparse.Namespace) -> None:
    configs: list[Path] = []
    for pattern in args.configs:
        found = sorted(Path(".").glob(pattern))
        if not found:
            found = [Path(pattern)]
        configs.extend(found)

    if not configs:
        print("No config files found.", file=sys.stderr)
        sys.exit(1)

    for cfg_path in tqdm(configs, desc="Classify", unit="preset"):
        try:
            config = load_config(cfg_path)
            sound_type = classify_run(config, workers=args.workers, save_path=args.save_path)
            tqdm.write(f"{config.output.name:<40} → {sound_type}")
        except Exception as exc:
            tqdm.write(f"ERROR  {cfg_path.stem}: {exc}")


def cmd_profiles(_args: argparse.Namespace) -> None:
    for name in available_profiles():
        print(name)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="patch-press",
        description="Zero-manual-work sampler presets from VST plugins and sample libraries.",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show per-item progress and status messages",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Debug mode: limit scan/scan-library to 5 items",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # patch-press sample config.yaml --path /media/DELUGE/MyPlugin --format deluge
    sample_p = sub.add_parser(
        "sample", help="Sample one preset from a YAML config file"
    )
    sample_p.add_argument("config", type=Path, help="Path to YAML config file")
    sample_p.add_argument(
        "--path",
        type=Path,
        required=True,
        metavar="PATH",
        help="Output directory for generated files",
    )
    sample_p.add_argument(
        "--format",
        required=True,
        metavar="FORMAT",
        help="Output format (e.g. deluge)",
    )
    sample_p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel analysis workers (default: 1)",
    )

    # patch-press batch configs/*.yaml --path /media/DELUGE/MyPlugin --format deluge
    batch_p = sub.add_parser("batch", help="Run multiple configs in parallel")
    batch_p.add_argument(
        "configs", nargs="+", metavar="CONFIG", help="Config paths or globs"
    )
    batch_p.add_argument(
        "--path",
        type=Path,
        required=True,
        metavar="PATH",
        help="Output directory for generated files",
    )
    batch_p.add_argument(
        "--format",
        required=True,
        metavar="FORMAT",
        help="Output format (e.g. deluge)",
    )
    batch_p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel analysis workers (default: 1)",
    )
    batch_p.add_argument(
        "--no-skip",
        action="store_true",
        help="Re-run even if output already exists",
    )
    batch_p.add_argument(
        "--only-profile",
        choices=["pluck", "synth", "pad", "drums"],
        default=None,
        metavar="PROFILE",
        dest="only_profile",
        help="Debug: only process configs matching this profile (e.g. synth, pad)",
    )

    # patch-press scan-from-probe ~/patch-probe/MyPlugin configs/MyPlugin
    scan_probe_p = sub.add_parser(
        "scan-from-probe",
        help="Analyse presets captured by patch-probe and generate config files",
    )
    scan_probe_p.add_argument(
        "probe_dir", type=Path, help="Directory containing patch-probe YAML files"
    )
    scan_probe_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    scan_probe_p.add_argument(
        "--profile",
        choices=["pluck", "synth", "pad", "drums"],
        default=None,
        help="Override auto-detected profile (pluck/synth/pad/drums); default: auto",
    )
    scan_probe_p.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        choices=[44100, 48000, 96000],
        metavar="HZ",
        help="Sample rate for VST capture (default: 48000)",
    )
    scan_probe_p.add_argument(
        "--tempo-bpm",
        type=float,
        default=120.0,
        metavar="BPM",
        help="Tempo for rhythm detection during classify (default: 120)",
    )
    scan_probe_p.add_argument("--probe-note", type=int, default=60, metavar="MIDI")
    scan_probe_p.add_argument("--probe-velocity", type=int, default=100, metavar="VEL")
    scan_probe_p.add_argument(
        "--note-step",
        type=_note_step_arg,
        default=DEFAULT_NOTE_STEP,
        help=f"Semitones between sampled notes (1=every semitone; default: {DEFAULT_NOTE_STEP})",
    )
    scan_probe_p.add_argument(
        "--duration",
        type=_duration_arg,
        default=DEFAULT_DURATION_S,
        help=f"Hold length in seconds per note (default: {DEFAULT_DURATION_S})",
    )
    scan_probe_p.add_argument(
        "--start-note",
        type=_note_arg,
        default="C1",
        metavar="NOTE",
        help="Lowest note to sample, e.g. C1 or A0 (default: C1)",
    )
    scan_probe_p.add_argument(
        "--end-note",
        type=_note_arg,
        default="C6",
        metavar="NOTE",
        help="Highest note to sample, e.g. C6 or C8 (default: C6)",
    )

    # patch-press scan-clap Plugin.clap ~/presets/Plugin configs/Plugin
    scan_clap_p = sub.add_parser(
        "scan-clap",
        help="Scan CLAP presets from a .clap-preset directory and generate config files",
    )
    scan_clap_p.add_argument("plugin", type=Path, help="Path to the .clap plugin file")
    scan_clap_p.add_argument(
        "preset_dir", type=Path, help="Directory containing .clap-preset files"
    )
    scan_clap_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    scan_clap_p.add_argument(
        "--profile",
        choices=["pluck", "synth", "pad", "drums"],
        default=None,
        help="Override auto-detected profile (pluck/synth/pad/drums); default: auto",
    )
    scan_clap_p.add_argument(
        "--sample-rate",
        type=int,
        default=48000,
        choices=[44100, 48000, 96000],
        metavar="HZ",
        help="Sample rate for capture (default: 48000)",
    )
    scan_clap_p.add_argument(
        "--tempo-bpm",
        type=float,
        default=120.0,
        metavar="BPM",
        help="Tempo for rhythm detection during classify (default: 120)",
    )
    scan_clap_p.add_argument("--probe-note", type=int, default=60, metavar="MIDI")
    scan_clap_p.add_argument("--probe-velocity", type=int, default=100, metavar="VEL")
    scan_clap_p.add_argument(
        "--note-step",
        type=_note_step_arg,
        default=DEFAULT_NOTE_STEP,
        help=f"Semitones between sampled notes (1=every semitone; default: {DEFAULT_NOTE_STEP})",
    )
    scan_clap_p.add_argument(
        "--duration",
        type=_duration_arg,
        default=DEFAULT_DURATION_S,
        help=f"Hold length in seconds per note (default: {DEFAULT_DURATION_S})",
    )
    scan_clap_p.add_argument(
        "--start-note",
        type=_note_arg,
        default="C1",
        metavar="NOTE",
        help="Lowest note to sample, e.g. C1 or A0 (default: C1)",
    )
    scan_clap_p.add_argument(
        "--end-note",
        type=_note_arg,
        default="C6",
        metavar="NOTE",
        help="Highest note to sample, e.g. C6 or C8 (default: C6)",
    )

    # patch-press scan-library "Mini From Mars" configs/Mini
    scan_lib_p = sub.add_parser(
        "scan-library", help="Generate config files for a sample library"
    )
    scan_lib_p.add_argument("library", type=Path, help="Path to library root folder")
    scan_lib_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    scan_lib_p.add_argument(
        "--type",
        required=True,
        choices=["multisample", "kit", "drumkit"],
        help="""
            Library structure: 'multisample' (one folder per
            instrument, multiple notes), 'kit' (one folder per
            instrument, one-shot hits), or 'drumkit' (one folder
            per kit, flat one-shot hits identified by filename
            keyword, e.g. Samples From Mars 808/909 kit folders)
        """,
    )
    scan_lib_p.add_argument(
        "--profile",
        choices=["pluck", "synth", "pad", "drums"],
        default=None,
        help="Override auto-detected profile (pluck/synth/pad/drums); default: auto",
    )
    scan_lib_p.add_argument(
        "--note-step",
        type=_note_step_arg,
        default=DEFAULT_NOTE_STEP,
        help=f"Semitones between sampled notes (1=every semitone; default: {DEFAULT_NOTE_STEP})",
    )
    scan_lib_p.add_argument(
        "--start-note",
        type=_note_arg,
        default="C1",
        metavar="NOTE",
        help="Lowest note to sample, e.g. C1 or A0 (default: C1)",
    )
    scan_lib_p.add_argument(
        "--end-note",
        type=_note_arg,
        default="C6",
        metavar="NOTE",
        help="Highest note to sample, e.g. C6 or C8 (default: C6)",
    )

    # patch-press scan-oneshots "input/Monosounds/Minimoog Synth Oneshots" configs/Monosounds
    scan_oneshots_p = sub.add_parser(
        "scan-oneshots",
        help="Generate one config per WAV in a folder of single-note oneshot presets (e.g. Monosounds)",
    )
    scan_oneshots_p.add_argument(
        "folder", type=Path, help="Folder of loose oneshot WAV files (one preset per file)"
    )
    scan_oneshots_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    scan_oneshots_p.add_argument(
        "--profile",
        choices=["pluck", "synth", "pad", "drums"],
        default=None,
        help="Override auto-detected profile (pluck/synth/pad/drums); default: auto",
    )

    # patch-press scan-wavetables "input/Echo Sound Works Core Tables/Basics" configs/EchoTables
    scan_wavetables_p = sub.add_parser(
        "scan-wavetables",
        help="Generate one config per WAV in a folder of Serum-format wavetable files",
    )
    scan_wavetables_p.add_argument(
        "folder", type=Path, help="Folder of wavetable WAV files (one preset per file)"
    )
    scan_wavetables_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    scan_wavetables_p.add_argument(
        "--archetype",
        choices=["pad", "pluck", "bass", "lead", "drone", "evolving_pad"],
        default=None,
        help="Override auto-detected archetype; default: auto (spectral analysis per file)",
    )

    # patch-press assemble-kits "909_from_mars/Individual Hits" configs/909Assembled
    assemble_kits_p = sub.add_parser(
        "assemble-kits",
        help="Synthesize kit configs from a bag-of-hits library organized by instrument category",
    )
    assemble_kits_p.add_argument(
        "folder", type=Path, help="Library root (one subfolder per instrument category)"
    )
    assemble_kits_p.add_argument(
        "config_dir", type=Path, help="Directory to write generated YAML configs"
    )
    assemble_kits_p.add_argument(
        "--min-categories",
        type=int,
        default=2,
        metavar="N",
        help="Minimum distinct instrument families a tag must span to become a kit (default: 2)",
    )

    # patch-press classify configs/*.yaml
    classify_p = sub.add_parser("classify", help="Print sound type for each preset without exporting")
    classify_p.add_argument(
        "configs", nargs="+", metavar="CONFIG", help="Config paths or globs"
    )
    classify_p.add_argument(
        "--workers",
        type=int,
        default=1,
        metavar="N",
        help="Number of parallel analysis workers (default: 1)",
    )
    classify_p.add_argument(
        "--save-path",
        type=Path,
        default=None,
        metavar="PATH",
        help="Save captured WAVs to PATH/<preset_name>/ for inspection",
    )

    # patch-press profiles
    sub.add_parser("profiles", help="List available profiles")

    args = parser.parse_args()

    if args.command in ("scan-from-probe", "scan-clap", "scan-library"):
        if args.start_note >= args.end_note:
            parser.error("--start-note must be below --end-note")

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.debug
            else logging.INFO if args.verbose else logging.WARNING
        ),
        format="%(message)s",
        handlers=[_TqdmLoggingHandler()],
    )
    suppress_noisy_loggers()

    if args.command == "sample":
        cmd_sample(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "scan-from-probe":
        cmd_scan_from_probe(args)
    elif args.command == "scan-clap":
        cmd_scan_clap(args)
    elif args.command == "scan-library":
        cmd_scan_library(args)
    elif args.command == "scan-oneshots":
        cmd_scan_oneshots(args)
    elif args.command == "scan-wavetables":
        cmd_scan_wavetables(args)
    elif args.command == "assemble-kits":
        cmd_assemble_kits(args)
    elif args.command == "classify":
        cmd_classify(args)
    elif args.command == "profiles":
        cmd_profiles(args)

    # C extensions (JUCE via patch-render, numba/LLVM via librosa) crash during
    # Python interpreter teardown. For a CLI tool, skipping teardown is safe
    # because temp files are cleaned up inside finally blocks in VSTAdapter._render().
    os._exit(0)


if __name__ == "__main__":
    main()

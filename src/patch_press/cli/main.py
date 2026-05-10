import argparse
import logging
import sys
from pathlib import Path

from ..config.loader import load_config
from ..profiles import available_profiles
from ..runner.batch import run_batch
from ..runner.pipeline import run
from ..runner.scan import QUALITY_CHOICES, scan_from_probe, scan_library


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
        quality=args.quality,
        debug=args.debug,
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


def cmd_scan_library(args: argparse.Namespace) -> None:
    summary = scan_library(
        library_path=args.library,
        config_dir=args.config_dir,
        library_type=args.type,
        profile=args.profile,
        quality=args.quality,
        debug=args.debug,
    )
    print(
        f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}"
    )


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

    # patch-press sample config.yaml --path /media/DELUGE/SYNTHS/MyPlugin --format deluge
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

    # patch-press batch configs/*.yaml --path /media/DELUGE/SYNTHS/MyPlugin --format deluge
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
    scan_probe_p.add_argument("--profile", choices=["synth", "drums"], default="synth")
    scan_probe_p.add_argument("--probe-note", type=int, default=60, metavar="MIDI")
    scan_probe_p.add_argument("--probe-velocity", type=int, default=100, metavar="VEL")
    scan_probe_p.add_argument(
        "--quality",
        choices=QUALITY_CHOICES,
        default="medium",
        help="Sampling quality: controls note step and capture duration (default: medium)",
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
        choices=["multisample", "kit"],
        help="""
            Library structure: 'multisample' (one folder per
            instrument, multiple notes) or 'kit' (one folder
            per instrument, one-shot hits)
        """,
    )
    scan_lib_p.add_argument("--profile", choices=["synth", "drums"], default="synth")
    scan_lib_p.add_argument(
        "--quality",
        choices=QUALITY_CHOICES,
        default="medium",
        help="Sampling quality: controls note step (default: medium)",
    )

    # patch-press profiles
    sub.add_parser("profiles", help="List available profiles")

    args = parser.parse_args()

    logging.basicConfig(
        level=(
            logging.DEBUG
            if args.debug
            else logging.INFO if args.verbose else logging.WARNING
        ),
        format="%(message)s",
    )

    if args.command == "sample":
        cmd_sample(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "scan-from-probe":
        cmd_scan_from_probe(args)
    elif args.command == "scan-library":
        cmd_scan_library(args)
    elif args.command == "profiles":
        cmd_profiles(args)


if __name__ == "__main__":
    main()

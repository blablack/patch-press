import argparse
import sys
from pathlib import Path

from ..config.loader import load_config
from ..profiles import available_profiles
from ..runner.batch import run_batch
from ..runner.pipeline import run
from ..runner.scan import scan_vst


def cmd_sample(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    output = run(config)
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

    results = run_batch(configs, skip_existing=not args.no_skip)
    errors = [p for p, r in results.items() if isinstance(r, Exception)]
    if errors:
        print(f"\n{len(errors)} error(s) — see above.", file=sys.stderr)
        sys.exit(1)


def cmd_scan(args: argparse.Namespace) -> None:
    deluge_path = args.deluge_path or Path("output") / args.plugin.stem
    summary = scan_vst(
        plugin_path=args.plugin,
        config_dir=args.config_dir,
        deluge_path=deluge_path,
        profile=args.profile,
        fmt=args.fmt,
        probe_note=args.probe_note,
        probe_velocity=args.probe_velocity,
        sustain_duration_s=args.sustain_duration,
    )
    print(f"\n{len(summary.written)}/{summary.total} configs written to {args.config_dir}")
    if summary.reviews:
        print(f"\n{len(summary.reviews)} preset(s) flagged for review:")
        for name, result in summary.reviews:
            detail = ", ".join(result.flags) if result.flags else f"confidence={result.confidence}"
            print(f"  - {name}: {detail}")


def cmd_profiles(_args: argparse.Namespace) -> None:
    for name in available_profiles():
        print(name)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="patch-press",
        description="Zero-manual-work sampler presets from VST plugins and sample libraries.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # patch-press sample config.yaml
    sample_p = sub.add_parser("sample", help="Sample one preset from a YAML config file")
    sample_p.add_argument("config", type=Path, help="Path to YAML config file")

    # patch-press batch configs/*.yaml --parallel 8
    batch_p = sub.add_parser("batch", help="Run multiple configs in parallel")
    batch_p.add_argument("configs", nargs="+", metavar="CONFIG", help="Config paths or globs")
    batch_p.add_argument(
        "--no-skip",
        action="store_true",
        help="Re-run even if output already exists",
    )

    # patch-press scan plugin.vst3 configs/MyPlugin --deluge-path /media/DELUGE/SYNTHS/MyPlugin
    scan_p = sub.add_parser("scan", help="Probe all presets in a VST and generate config files")
    scan_p.add_argument("plugin", type=Path, help="Path to VST3 plugin")
    scan_p.add_argument("config_dir", type=Path, help="Directory to write generated YAML configs")
    scan_p.add_argument("--deluge-path", type=Path, default=None, metavar="PATH",
                        help="output.path embedded in generated configs (default: output/<plugin>)")
    scan_p.add_argument("--profile", choices=["synth", "drums"], default="synth")
    scan_p.add_argument("--format", dest="fmt", default="deluge", metavar="FORMAT")
    scan_p.add_argument("--probe-note", type=int, default=60, metavar="MIDI")
    scan_p.add_argument("--probe-velocity", type=int, default=100, metavar="VEL")
    scan_p.add_argument("--sustain-duration", type=float, default=4.0, metavar="SECS",
                        help="Capture duration for sustaining sounds (default: 4.0)")

    # patch-press profiles
    sub.add_parser("profiles", help="List available profiles")

    args = parser.parse_args()

    if args.command == "sample":
        cmd_sample(args)
    elif args.command == "batch":
        cmd_batch(args)
    elif args.command == "scan":
        cmd_scan(args)
    elif args.command == "profiles":
        cmd_profiles(args)


if __name__ == "__main__":
    main()

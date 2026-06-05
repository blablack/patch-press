#!/usr/bin/env python3
"""Detect audible clicks at loop boundaries in a Deluge multisample XML.

Reads loop points from the XML, loads each WAV, and checks the splice
discontinuity at (loop_end - 1) -> loop_start for amplitude and slope mismatches.

Usage:
    python tools/detect_clicks.py "output/Deluge/Surge XT/Bass 1.xml"
    python tools/detect_clicks.py "output/Deluge/Surge XT/"   # all XMLs
    python tools/detect_clicks.py my.wav --loop-start 12345 --loop-end 67890
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree

_SLOPE_WINDOW = 16

# Matches the thresholds used in validate_splice_reason
_AMP_WARN  = 0.10   # yellow
_AMP_FAIL  = 0.25   # red
_DERIV_WARN  = 0.25
_DERIV_FAIL  = 0.50

_GREEN  = "\033[32m"
_YELLOW = "\033[33m"
_RED    = "\033[31m"
_RESET  = "\033[0m"


def _color(val: float, warn: float, fail: float) -> str:
    if val >= fail:
        return _RED
    if val >= warn:
        return _YELLOW
    return _GREEN


def _check_splice(wav_path: Path, loop_start: int, loop_end: int) -> dict:
    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    n = len(mono)
    peak = float(np.abs(mono).max()) or 1.0

    if loop_start < 1 or loop_end < 2 or loop_end >= n:
        return {"error": f"loop points out of bounds (n={n})"}

    # Amplitude discontinuity at the hard-jump boundary
    amp_disc = abs(float(mono[loop_end - 1]) - float(mono[loop_start])) / peak

    # Slope mismatch: direction of waveform just before loop_end vs just after loop_start
    w = _SLOPE_WINDOW
    slope_end   = float(mono[loop_end - 1]) - float(mono[max(loop_end - 1 - w, 0)])
    slope_start = float(mono[min(loop_start + w, n - 1)]) - float(mono[loop_start])
    deriv_disc  = abs(slope_end - slope_start) / (2.0 * peak)

    # Amplitude values at the two splice samples
    amp_at_end   = float(mono[loop_end - 1])
    amp_at_start = float(mono[loop_start])

    return {
        "loop_start": loop_start,
        "loop_end": loop_end,
        "loop_len_s": (loop_end - loop_start) / sr,
        "sr": sr,
        "amp_at_end": amp_at_end,
        "amp_at_start": amp_at_start,
        "amp_disc": amp_disc,
        "deriv_disc": deriv_disc,
    }


def _report_splice(label: str, result: dict) -> bool:
    if "error" in result:
        print(f"  {_RED}ERROR{_RESET}  {label}: {result['error']}")
        return False

    amp_c   = _color(result["amp_disc"],   _AMP_WARN,   _AMP_FAIL)
    deriv_c = _color(result["deriv_disc"], _DERIV_WARN, _DERIV_FAIL)

    overall_ok = result["amp_disc"] < _AMP_FAIL and result["deriv_disc"] < _DERIV_FAIL
    status = f"{_GREEN}OK   {_RESET}" if overall_ok else f"{_RED}CLICK{_RESET}"

    print(
        f"  {status}  {label}\n"
        f"         loop [{result['loop_start']} → {result['loop_end']}]  "
        f"len={result['loop_len_s']:.3f}s  sr={result['sr']}\n"
        f"         x[end-1]={result['amp_at_end']:+.5f}  "
        f"x[start]={result['amp_at_start']:+.5f}\n"
        f"         amp_disc={amp_c}{result['amp_disc']:.4f}{_RESET}"
        f" (warn≥{_AMP_WARN} fail≥{_AMP_FAIL})  "
        f"deriv_disc={deriv_c}{result['deriv_disc']:.4f}{_RESET}"
        f" (warn≥{_DERIV_WARN} fail≥{_DERIV_FAIL})"
    )
    return overall_ok


def _resolve_wav(xml_path: Path, wav_rel: str) -> Path | None:
    """Resolve a WAV path from a Deluge XML.

    Deluge stores paths relative to the SD card root (e.g. SAMPLES/…/foo.wav).
    We try a few heuristics to find the file on the local filesystem.
    """
    # Try relative to the XML's parent (works when output/ mirrors SD card root)
    candidate = xml_path.parent / wav_rel
    if candidate.exists():
        return candidate
    # Try from the repo root (XML is at output/Deluge/<preset>.xml, WAV at SAMPLES/…)
    repo_root = xml_path.parent
    while repo_root.parent != repo_root:
        candidate = repo_root / wav_rel
        if candidate.exists():
            return candidate
        repo_root = repo_root.parent
    # Basename fallback: look next to the XML
    candidate = xml_path.parent / Path(wav_rel).name
    return candidate if candidate.exists() else None


def _check_xml(xml_path: Path) -> tuple[int, int]:
    """Returns (pass_count, total_count)."""
    tree = etree.parse(str(xml_path))
    root = tree.getroot()

    # sampleRange holds fileName; its child zone holds loop points
    entries: list[tuple[str, int, int]] = []
    for sr_el in root.findall(".//sampleRange"):
        wav_rel = sr_el.get("fileName", "")
        zone = sr_el.find("zone")
        if zone is None:
            continue
        ls_str = zone.get("startLoopPos")
        le_str = zone.get("endLoopPos")
        if ls_str and le_str:
            entries.append((wav_rel, int(ls_str), int(le_str)))

    if not entries:
        print(f"  (no loop points found in {xml_path.name})")
        return 0, 0

    passed = 0
    for wav_rel, ls, le in entries:
        wav_path = _resolve_wav(xml_path, wav_rel)
        if wav_path is None:
            print(f"  {_RED}MISSING{_RESET}  WAV not found: {wav_rel}")
            continue
        result = _check_splice(wav_path, ls, le)
        label = wav_path.name
        if _report_splice(label, result):
            passed += 1

    return passed, len(entries)


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect loop-boundary clicks in Deluge exports")
    parser.add_argument("path", type=Path, help="Deluge XML file, directory of XMLs, or WAV file")
    parser.add_argument("--loop-start", type=int, help="Loop start sample (WAV-only mode)")
    parser.add_argument("--loop-end",   type=int, help="Loop end sample (WAV-only mode)")
    args = parser.parse_args()

    if args.path.suffix.lower() == ".wav":
        if args.loop_start is None or args.loop_end is None:
            print("Error: --loop-start and --loop-end required for WAV-only mode", file=sys.stderr)
            sys.exit(1)
        result = _check_splice(args.path, args.loop_start, args.loop_end)
        _report_splice(args.path.name, result)
        return

    xmls: list[Path] = []
    if args.path.is_dir():
        xmls = sorted(args.path.rglob("*.xml"))
    elif args.path.suffix.lower() == ".xml":
        xmls = [args.path]
    else:
        print(f"Error: expected an XML file, directory, or WAV file — got {args.path}", file=sys.stderr)
        sys.exit(1)

    if not xmls:
        print("No XML files found.")
        return

    total_pass = total_zones = 0
    for xml in xmls:
        print(f"\n{xml.name}")
        p, t = _check_xml(xml)
        total_pass += p
        total_zones += t

    if total_zones:
        print(f"\n{'─'*60}")
        overall_c = _GREEN if total_pass == total_zones else _RED
        print(f"  {overall_c}{total_pass}/{total_zones}{_RESET} loop splices clean")


if __name__ == "__main__":
    main()

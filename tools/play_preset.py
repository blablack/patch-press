#!/usr/bin/env python3
"""Preview a Deluge multisample XML with loop on the local machine.

Usage:
    python tools/play_preset.py output/Deluge/Surge\ XT/Bass\ 1.xml
    python tools/play_preset.py output/Deluge/Surge\ XT/Bass\ 1.xml --note 48 --loops 4
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree


def _resolve_wav(xml_path: Path, file_name: str) -> Path:
    # XML is at <root>/<plugin>/<preset>.xml
    # WAVs are at <root>/SAMPLES/...  →  go up two levels
    return xml_path.parent.parent / file_name


def _find_range(xml_path: Path, note: int) -> tuple[Path, int | None, int | None, int]:
    root = etree.parse(str(xml_path)).getroot()
    ranges = root.findall(".//sampleRange")
    if not ranges:
        raise ValueError(f"No sampleRange elements in {xml_path.name} — is it a multisample XML?")

    chosen = ranges[-1]
    for sr in ranges:
        if note <= int(sr.get("rangeTopNote", "127")):
            chosen = sr
            break

    zone = chosen.find("zone")
    wav_path = _resolve_wav(xml_path, chosen.get("fileName", ""))
    has_loop = zone is not None and "startLoopPos" in zone.attrib
    loop_start = int(zone.get("startLoopPos")) if has_loop else None
    loop_end   = int(zone.get("endLoopPos"))   if has_loop else None
    sampled_note = 48 - int(chosen.get("transpose", "0"))
    return wav_path, loop_start, loop_end, sampled_note


def _build_playback(data: np.ndarray, loop_start: int | None, loop_end: int | None, loops: int) -> np.ndarray:
    if loop_start is None or loop_end is None or loop_end <= loop_start:
        return data
    return np.concatenate([data[:loop_start]] + [data[loop_start:loop_end]] * loops + [data[loop_end:]])


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview a Deluge preset with loop")
    parser.add_argument("xml", type=Path, help="Deluge XML file")
    parser.add_argument("--note", type=int, default=60, metavar="MIDI",
                        help="MIDI note to preview (default: 60)")
    parser.add_argument("--loops", type=int, default=3, metavar="N",
                        help="Loop repetitions (default: 3)")
    args = parser.parse_args()

    wav_path, loop_start, loop_end, sampled_note = _find_range(args.xml, args.note)

    if not wav_path.exists():
        print(f"WAV not found: {wav_path}", file=sys.stderr)
        sys.exit(1)

    data, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    playback = _build_playback(data, loop_start, loop_end, args.loops)

    print(f"Sample: {wav_path.name}  (recorded at note {sampled_note})")
    if loop_start is not None and loop_end is not None and loop_end > loop_start:
        print(f"Loop:   {loop_start}–{loop_end}  ({(loop_end - loop_start) / sr:.3f}s × {args.loops})")
    else:
        print("Loop:   none (--loops has no effect)")
    print(f"Total:  {len(playback) / sr:.1f}s — opening…")

    tmp = Path(tempfile.mktemp(suffix=".wav"))
    sf.write(str(tmp), playback, sr)
    subprocess.run(["xdg-open", str(tmp)], check=False)


if __name__ == "__main__":
    main()

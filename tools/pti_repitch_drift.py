#!/usr/bin/env python3
"""Measure how faithful a single repitched sample is to a full multisample.

Why this exists
---------------
The Polyend .pti format has no keyzones: a synth preset ships ONE capture and the device
repitches it chromatically across the whole keyboard (`io/exporters/polyend.py:_export_synth`).
For a patch whose timbre is consistent across its range (a classic Juno/Jupiter poly) that is
faithful; for one whose timbre tracks pitch (filter tracking, fixed resonant formants, per-octave
character changes) the single repitched sample diverges from what the real multisample sounds like.

This tool measures that divergence directly, so we can calibrate WHEN one sample is enough and
when a preset should instead ship as several pitch-zoned .pti files. For each captured note it:

  1. picks the centre capture the exporter would root on (captured-range midpoint + high bias);
  2. naively resamples that centre sample to the note's pitch — exactly the device's repitch,
     formants and all (NOT a formant-preserving phase-vocoder shift);
  3. compares the repitched centre's timbre (mean MFCC, 0th coeff dropped so it is spectral
     SHAPE not level, and pitch now matches so the distance is pure timbre) to the REAL capture
     at that note.

`drift` = mean divergence across the range, `max` = worst single note. Low drift => one sample
is faithful (ship single); high => the range needs zoning. Run it across a spread of presets to
see where the threshold naturally falls before wiring the single-vs-zoned decision into the
exporter.

Usage
-----
    # One preset (a folder of noteNNN_*.wav captures, e.g. an exported Deluge SAMPLES dir):
    python tools/pti_repitch_drift.py "output/Deluge/SAMPLES/Diva/1 BASS/HS Bass Nine"

    # Many at once, ranked worst-first (glob expands to one row per preset):
    python tools/pti_repitch_drift.py output/Deluge/SAMPLES/Diva/*/*/ \
        output/Deluge/SAMPLES/Samples\ from\ Mars/*/*/

    --per-note   also print the divergence of every captured note (single-preset view)
    --json OUT   write the full per-preset + per-note numbers as JSON
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

# Run against the working tree without an install (mirrors the other tools/).
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))
from patch_press.io.adapters.library import _parse_note_rr  # noqa: E402

_ROOT_HIGH_BIAS = 2  # keep in sync with io/exporters/polyend.py
_SR = 44100
_WIN_S = 0.4         # sustain window analysed per note
_N_MFCC = 20
_NOTE_RE = re.compile(r"note(\d{3})")
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(n: int) -> str:
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"


def _load_sustain_mono(path: Path) -> np.ndarray | None:
    """Mono ~_WIN_S window from the middle of the file (past the attack, into the sustain).

    Seeks and reads ONLY the window — the Diva captures are 30 s / 48 kHz, so reading and
    resampling the whole file per note is ~75x wasted work. Resamples just the small window.
    """
    with sf.SoundFile(str(path)) as f:
        sr = f.samplerate
        win = int(_WIN_S * sr)
        if f.frames == 0:
            return None
        f.seek(max(0, f.frames // 2 - win // 2))
        y = f.read(min(win, f.frames), dtype="float32", always_2d=True)
    mono = y.mean(axis=1)
    if len(mono) == 0:
        return None
    if sr != _SR:
        mono = librosa.resample(mono, orig_sr=sr, target_sr=_SR, res_type="soxr_lq")
    return mono


def _repitch(mono: np.ndarray, semitones: float) -> np.ndarray:
    """Naive playback-rate repitch (formants move with pitch — what the Tracker does).

    semitones>0 plays the sample higher/faster => fewer samples. Kept at _SR, so a later
    MFCC reads the shifted spectral envelope directly.
    """
    p = 2.0 ** (semitones / 12.0)
    new_len = max(1, round(len(mono) / p))
    # soxr_lq: the MFCC captures spectral SHAPE, which naive resampling sets — a cheaper
    # kernel changes that negligibly, and the analysis window is small so it is quick.
    return np.asarray(
        librosa.resample(mono, orig_sr=len(mono), target_sr=new_len, res_type="soxr_lq"),
        dtype=np.float32,
    )


def _timbre(mono: np.ndarray) -> np.ndarray | None:
    """Mean MFCC over the window, 0th coeff dropped (shape, not level)."""
    if len(mono) < 512:
        return None
    m = librosa.feature.mfcc(y=mono, sr=_SR, n_mfcc=_N_MFCC).mean(axis=1)[1:]
    n = float(np.linalg.norm(m))
    return m / n if n > 0 else None


def _centre_note(notes: list[int]) -> int:
    """The note the exporter roots on: the captured note nearest the (high-biased) range midpoint."""
    c = round((min(notes) + max(notes)) / 2) + _ROOT_HIGH_BIAS
    return min(notes, key=lambda n: abs(n - c))


def _note_of(wav: Path) -> int | None:
    """MIDI note from a capture filename: VST `noteNNN` convention, else library note+octave."""
    m = _NOTE_RE.search(wav.name)
    if m:
        return int(m.group(1))
    parsed = _parse_note_rr(wav.stem)  # library naming (A#0, Bb2, C3_0001, …)
    return parsed[0] if parsed else None


def analyse_preset(folder: Path) -> dict | None:
    # One capture per note (lowest RR wins when a note has several — sorted order picks it).
    caps: dict[int, Path] = {}
    for wav in sorted(folder.glob("*.wav")):
        note = _note_of(wav)
        if note is not None:
            caps.setdefault(note, wav)
    if len(caps) < 2:
        return None

    notes = sorted(caps)
    centre = _centre_note(notes)
    centre_mono = _load_sustain_mono(caps[centre])
    if centre_mono is None:
        return None

    per_note: list[dict] = []
    for n in notes:
        if n == centre:
            continue
        real = _load_sustain_mono(caps[n])
        rt = _timbre(real) if real is not None else None
        pt = _timbre(_repitch(centre_mono, n - centre))
        if rt is None or pt is None:
            continue
        dist = float(1.0 - np.dot(rt, pt))  # cosine distance of unit MFCC vectors
        per_note.append({"note": n, "name": _note_name(n), "shift": n - centre, "drift": dist})

    if not per_note:
        return None
    drifts = np.array([d["drift"] for d in per_note])
    worst = max(per_note, key=lambda d: d["drift"])
    return {
        "preset": folder.name,
        "path": str(folder),
        "notes": len(notes),
        "range": [notes[0], notes[-1]],
        "range_oct": round((notes[-1] - notes[0]) / 12, 1),
        "centre": centre,
        "centre_name": _note_name(centre),
        "drift_mean": float(drifts.mean()),
        "drift_max": worst["drift"],
        "worst_note": worst["name"],
        "worst_shift": worst["shift"],
        "per_note": per_note,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", help="preset folders of noteNNN_*.wav captures (globs ok)")
    ap.add_argument("--per-note", action="store_true", help="print every captured note's divergence")
    ap.add_argument("--json", type=Path, help="write full results as JSON")
    args = ap.parse_args()

    # Expand any globs the shell left unexpanded and keep only directories.
    folders: list[Path] = []
    for f in args.folders:
        for hit in ([f] if Path(f).exists() else glob.glob(f)):
            p = Path(hit)
            if p.is_dir():
                folders.append(p)
    if not folders:
        sys.exit("no preset folders found")

    results = [r for r in (analyse_preset(p) for p in folders) if r]
    if not results:
        sys.exit("no analysable presets (need >=2 noteNNN_*.wav files each)")
    results.sort(key=lambda r: r["drift_mean"], reverse=True)

    w = max(len(r["preset"]) for r in results)
    print(f"\n{'preset':<{w}}  notes  range      oct  centre  drift(mean)  drift(max)  worst")
    print("-" * (w + 62))
    for r in results:
        rng = f"{_note_name(r['range'][0])}-{_note_name(r['range'][1])}"
        print(f"{r['preset']:<{w}}  {r['notes']:>5}  {rng:<9}  {r['range_oct']:>3}  "
              f"{r['centre_name']:>6}  {r['drift_mean']:>11.3f}  {r['drift_max']:>10.3f}  "
              f"{r['worst_note']}({r['worst_shift']:+d})")

    if args.per_note and len(results) == 1:
        print()
        for d in results[0]["per_note"]:
            bar = "#" * int(d["drift"] * 200)
            print(f"  {d['name']:>4} ({d['shift']:+3d})  {d['drift']:.3f}  {bar}")

    vals = np.array([r["drift_mean"] for r in results])
    print(f"\n{len(results)} presets | mean drift: "
          f"min {vals.min():.3f}  median {np.median(vals):.3f}  max {vals.max():.3f}")

    if args.json:
        args.json.write_text(json.dumps(results, indent=2))
        print(f"wrote {args.json}")


if __name__ == "__main__":
    main()

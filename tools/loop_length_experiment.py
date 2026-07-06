#!/usr/bin/env python3
"""Audition the SAME captured note looped several ways — to settle, by ear, what
actually fixes the "audible loop" on evolving/FM patches.

Why this exists
---------------
On rich synths (Dexed FM pads, etc.) the loop SEAM is already clean — amplitude and
spectrum match across the splice, the baked crossfade does its job. What you still hear
is *mechanical repetition*: the sound breathes (quasi-periodic FM beating), and a short
loop freezes one breath and repeats it identically. Our scoring optimises seam
cleanliness, which is minimised by SHORT loops, so it can't see this. The only way to
know which lever helps — longer loop, longer crossfade, or no loop at all — is to listen.

This tool bakes, for one captured note, a set of variants that isolate each lever:

  current (ship)      the loop we ship today (short, 10 ms crossfade)        [baseline]
  current (long xfade) same loop points, long crossfade   -> does xfade alone help?
  ~2x / ~3x / ~Nx     progressively longer loops (modulation-period aligned) -> does length help?
  one-shot (no loop)  plays straight through, natural decay -> is no loop best?

Each variant reuses the production crossfade (`bake_loop_crossfade`) and is rendered
through `tools/loop_audition.py`, so you HEAR the repeats and SEE the looping playhead /
seam exactly as the Deluge plays them (the Deluge hard-jumps loop_end->loop_start; the
crossfade is baked into the WAV). An index.html links them all with the measured numbers.

Usage
-----
    # Resolve the WAV + shipped loop points straight from an exported preset:
    python tools/loop_length_experiment.py \
        --xml "output/Deluge/SYNTHS/Dexed/BANKS, T..xml" --note 60 --note 72

    # Or drive it from a raw WAV with explicit loop points:
    python tools/loop_length_experiment.py \
        --wav "output/Deluge/SAMPLES/Dexed/BANKS, T./note060_T120_V100_RR1.wav" \
        --loop-start 148706 --loop-end 268638

Options:
    --out-dir DIR   where baked WAVs + HTML land (default: output/loop_experiment)
    --tempo BPM     tempo for envelope/modulation analysis (default: 120)
    --no-open       don't open the index in a browser when done
"""
from __future__ import annotations

import argparse
import html
import subprocess
import sys
import webbrowser
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree

# Run against the working tree without an install.
_SRC = Path(__file__).resolve().parent.parent / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

from patch_press.analysis.envelope import analyze_envelope  # noqa: E402
from patch_press.analysis.loop import (  # noqa: E402
    _snap_to_slope_zero_crossing,
    bake_loop_crossfade,
    find_loop_candidates,
)
from patch_press.model.audio import AudioBuffer  # noqa: E402

_FRAME = 2048
_AUDITION = Path(__file__).resolve().parent / "loop_audition.py"
_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(n: int) -> str:
    return f"{_NOTE_NAMES[n % 12]}{n // 12 - 1}"


def _resolve_from_xml(xml_path: Path, note: int) -> tuple[Path, int, int]:
    """Return (wav_path, loop_start, loop_end) for the zone captured at `note`.

    The renderer stamps the captured MIDI note into the fileName (noteNNN); the WAV path is
    SAMPLES-rooted and the XML sits at <sd>/SYNTHS/<plugin>/<preset>.xml, so the SD root is
    three levels up (mirrors tools/loop_audition.py)."""
    root = etree.parse(str(xml_path)).getroot()
    for rng in root.iter("sampleRange"):
        fname = rng.get("fileName", "")
        m = [int(s) for s in [fname.split("note")[-1][:3]] if s.isdigit()]
        if not m or m[0] != note:
            continue
        zone = rng.find("zone")
        ls = int(zone.get("startLoopPos"))
        le = int(zone.get("endLoopPos"))
        wav = xml_path.parent.parent.parent / fname
        return wav, ls, le
    raise SystemExit(f"note {note} not found in {xml_path}")


def _long_fade_ms(loop_len_samples: int, sr: int) -> float:
    """A crossfade proportional to the loop (~12%), bounded so it stays musical."""
    return float(np.clip(loop_len_samples / sr * 0.12 * 1000.0, 80.0, 900.0))


def _aligned_end(mono: np.ndarray, ls: int, target_len: int, unit: int, sr: int, limit: int) -> int:
    """End that is ~k*unit from ls (envelope-continuous at the wrap) and snapped to a
    same-direction zero crossing so the pre-crossfade seam is decent. Clamped to `limit`."""
    n = len(mono)
    k = max(1, round(target_len / unit))
    end = ls + k * unit
    end = min(end, limit, n - _FRAME)
    rising = float(mono[min(ls + 4, n - 1)]) > float(mono[max(ls - 4, 0)])
    snap_radius = max(512, unit // 8)
    return _snap_to_slope_zero_crossing(mono, end, rising, snap_radius) + 1


def _within_loop_pulse(mono: np.ndarray, ls: int, le: int, sr: int, peak_rms: float) -> float:
    """How much the level swings inside the loop, as a fraction of peak RMS. This is the
    'breathing' that becomes audible when frozen and repeated; high => more mechanical."""
    win = int(0.02 * sr)
    seg = mono[ls:le]
    if len(seg) < 2 * win or peak_rms <= 0:
        return 0.0
    env = np.sqrt([np.mean(seg[i : i + win] ** 2) for i in range(0, len(seg) - win, win)])
    return float((env.max() - env.min()) / peak_rms)


def _build_specs(base_len: int, t_mod: int | None, sr: int, sustain_avail: int) -> list[tuple[str, int | None, float]]:
    """(label, target_len_samples | None for one-shot, fade_ms). Lengths align to the
    modulation period when known, else to the shipped loop length."""
    unit = t_mod if (t_mod and t_mod > int(0.2 * sr)) else base_len
    specs: list[tuple[str, int | None, float]] = [
        ("current (ship, 10ms xfade)", base_len, 10.0),
        ("current (long xfade)", base_len, _long_fade_ms(base_len, sr)),
    ]
    seen = {round(base_len / sr, 2)}
    for k in (2, 3, 4, 6, 8):
        L = k * unit
        if L > sustain_avail:
            break
        key = round(L / sr, 2)
        if key in seen:
            continue
        seen.add(key)
        specs.append((f"~{k}x length ({key:.1f}s)", L, _long_fade_ms(L, sr)))
    specs.append(("one-shot (no loop)", None, 0.0))
    return specs


def _carrier_W(note: int | None, sr: int) -> int:
    """Seam-match window ~2 carrier periods, so the raw mismatch reflects waveform
    continuity at the note's pitch (bass notes need a wider window)."""
    if note is None:
        return 512
    freq = 440.0 * 2 ** ((note - 69) / 12.0)
    return max(512, int(2 * sr / freq))


def _raw_mismatch(mono: np.ndarray, ls: int, le: int, W: int) -> float:
    """Normalised RMS difference between the W samples before loop_end and before
    loop_start: how cleanly the wrap continues with NO crossfade. <0.1 ~ click-free raw."""
    pre = mono[ls - W : ls]
    nrm = float(np.linalg.norm(pre)) + 1e-9
    return float(np.linalg.norm(mono[le - W : le] - pre) / nrm)


def _best_loop_end(mono: np.ndarray, ls: int, sr: int, lo_s: float, hi_s: float, W: int) -> tuple[int, float]:
    """Sample-accurate search for the loop_end in [ls+lo_s, ls+hi_s] that minimises the raw
    seam mismatch — i.e. the true repeat period locked to the carrier. This is what the
    production loop-finder should do but doesn't when the modulation detector misfires."""
    los, his = ls + int(lo_s * sr), min(ls + int(hi_s * sr), len(mono) - _FRAME)
    if his <= los:
        return ls + int(lo_s * sr), 1.0
    ends = np.arange(los, his)
    pre = mono[ls - W : ls]
    nrm = float(np.linalg.norm(pre)) + 1e-9
    d = np.array([np.linalg.norm(mono[e - W : e] - pre) / nrm for e in ends])
    bi = int(np.argmin(d))
    return int(ends[bi]), float(d[bi])


def _audition(wav: Path, ls: int, le: int, loops: int, out_html: Path) -> None:
    subprocess.run(
        [sys.executable, str(_AUDITION), "--wav", str(wav),
         "--loop-start", str(ls), "--loop-end", str(le),
         "--loops", str(loops), "--out", str(out_html), "--no-open"],
        check=True,
    )


def _process_note(wav: Path, ls: int, le: int, note: int | None, tempo: float,
                  out_dir: Path, align_period: bool, production: bool = False) -> dict:
    buf = AudioBuffer.from_file(wav)
    sr = buf.sample_rate
    mono = buf.to_mono()
    n = len(mono)
    peak_rms = float(np.sqrt(np.mean(mono**2))) or 1.0
    W = _carrier_W(note, sr)

    env = analyze_envelope(buf, bpm=tempo)
    t_mod = env.modulation_period_samples
    sustain_end = env.sustain_end if env.sustain_end > ls else n
    sustain_avail = min(sustain_end, n - _FRAME) - ls
    base_len = le - ls
    has_mod = bool(t_mod and t_mod > int(0.2 * sr))
    unit = t_mod if has_mod else base_len

    # Build a flat list of variants: (name, loop_start, loop_end, fade_ms).
    variants: list[tuple[str, int, int, float]] = [("shipped (10ms xfade)", ls, le, 10.0)]
    found_period_s = None
    if production:
        # Audition exactly what the current loop.py would now pick (the real fix), vs shipped.
        cands = find_loop_candidates(buf, 0.75, tempo, envelope=env, midi_note=note)
        if cands:
            (ps, pe), _ = cands[0]
            variants.append((f"PRODUCTION loop.py = {(pe-ps)/sr:.3f}s", ps, pe, 10.0))
    elif align_period:
        # Find the true period at the shipped loop_start, then offer it raw and lightly faded,
        # plus its 2x/3x — to show that LENGTH ALIGNMENT (not crossfade) is the fix.
        le1, d1 = _best_loop_end(mono, ls, sr, 0.4, 2.8, W)
        period = le1 - ls
        found_period_s = period / sr
        variants.append((f"period x1 = {period/sr:.3f}s (NO xfade)", ls, le1, 0.0))
        variants.append((f"period x1 = {period/sr:.3f}s (10ms xfade)", ls, le1, 10.0))
        for k in (2, 3):
            ctr = k * period / sr
            if (ctr + 0.12) * sr < sustain_avail:
                lek, _ = _best_loop_end(mono, ls, sr, ctr - 0.12, ctr + 0.12, W)
                variants.append((f"period x{k} = {(lek-ls)/sr:.3f}s (NO xfade)", ls, lek, 0.0))
    else:
        for name, target, fade_ms in _build_specs(base_len, t_mod, sr, sustain_avail):
            if target is None:
                continue
            vle = _aligned_end(mono, ls, target, unit, sr, ls + sustain_avail)
            variants.append((name, ls, vle, fade_ms))
    variants.append(("one-shot (no loop)", 0, n - 1, 0.0))  # plays once, natural decay

    label = f"note{note:03d}" if note is not None else wav.stem
    rows = []
    for i, (name, vls, vle, fade_ms) in enumerate(variants):
        one_shot = name.startswith("one-shot")
        baked = buf if (fade_ms <= 0 or one_shot) else bake_loop_crossfade(buf, vls, vle, fade_ms)
        length_s = (vle - vls) / sr
        loops = 1 if one_shot else int(np.clip(round(28.0 / max(length_s, 0.2)), 2, 8))
        tag = f"{label}_{i:02d}"
        baked_wav = out_dir / f"{tag}.wav"
        sf.write(str(baked_wav), baked.data.T, sr)
        out_html = out_dir / f"{tag}.html"
        _audition(baked_wav, vls, vle, loops, out_html)

        rows.append({
            "name": name, "html": out_html.name, "length_s": length_s,
            "fade_ms": fade_ms, "loops": loops,
            "raw_mismatch": 0.0 if one_shot else _raw_mismatch(mono, vls, vle, W),
            "pulse": 0.0 if one_shot else _within_loop_pulse(mono, vls, vle, sr, peak_rms),
        })
    return {"note": note, "label": label,
            "name": _note_name(note) if note is not None else wav.stem,
            "unit_s": found_period_s if align_period else unit / sr,
            "unit_src": "detected period" if align_period else ("modulation period" if has_mod else "shipped loop length"),
            "rows": rows}


def _write_index(out_dir: Path, title: str, notes: list[dict]) -> Path:
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'><title>loop experiment — ",
        html.escape(title),
        "</title><style>body{font:14px system-ui;margin:24px;max-width:1000px}"
        "table{border-collapse:collapse;margin:8px 0 24px}td,th{border:1px solid #ccc;padding:5px 10px;text-align:right}"
        "th{background:#f3f3f3}td:first-child,th:first-child{text-align:left}a{color:#06c}"
        ".hint{color:#666;font-size:13px}</style></head><body>",
        f"<h1>loop length / crossfade experiment — {html.escape(title)}</h1>",
        "<p class='hint'>Each row is the same captured note looped a different way. "
        "Click <b>listen</b> to hear the repeats and watch the playhead wrap. "
        "<b>raw mismatch</b> = how cleanly the wrap continues with NO crossfade "
        "(&lt;0.1 ~ click-free raw); a period-aligned length scores low, a misaligned one high. "
        "<b>pulse</b> = how much the level breathes inside the loop. Compare the shipped loop's "
        "mismatch to the period-aligned ones.</p>",
    ]
    for nd in notes:
        parts.append(f"<h2>note {nd['label']} — {html.escape(str(nd['name']))} "
                     f"<span class='hint'>(lengths aligned to {nd['unit_s']:.2f}s — {nd['unit_src']})</span></h2>")
        parts.append("<table><tr><th>variant</th><th>length</th><th>xfade</th>"
                     "<th>repeats</th><th>raw mismatch</th><th>pulse</th><th></th></tr>")
        for r in nd["rows"]:
            parts.append(
                f"<tr><td>{html.escape(r['name'])}</td><td>{r['length_s']:.2f}s</td>"
                f"<td>{r['fade_ms']:.0f}ms</td><td>{r['loops']}</td>"
                f"<td>{r['raw_mismatch']:.3f}</td><td>{r['pulse']:.2f}</td>"
                f"<td><a href='{r['html']}'>listen &#9654;</a></td></tr>")
        parts.append("</table>")
    parts.append("</body></html>")
    index = out_dir / "index.html"
    index.write_text("".join(parts))
    return index


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", type=Path, help="exported preset XML")
    ap.add_argument("--note", type=int, action="append", help="captured MIDI note (repeatable; with --xml)")
    ap.add_argument("--wav", type=Path, help="raw captured WAV (alternative to --xml)")
    ap.add_argument("--loop-start", type=int, help="loop start sample (with --wav)")
    ap.add_argument("--loop-end", type=int, help="loop end sample (with --wav)")
    ap.add_argument("--out-dir", type=Path, default=Path("output/loop_experiment"))
    ap.add_argument("--tempo", type=float, default=120.0)
    ap.add_argument("--align-period", action="store_true",
                    help="find the true repeat period and audition it raw vs the shipped loop "
                         "(instead of the length ladder)")
    ap.add_argument("--production", action="store_true",
                    help="audition exactly what the current loop.py picks vs the shipped loop")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    notes: list[dict] = []
    if args.xml:
        if not args.note:
            ap.error("--xml requires at least one --note")
        title = args.xml.stem
        for note in args.note:
            wav, ls, le = _resolve_from_xml(args.xml, note)
            print(f"note {note}: {wav.name}  loop [{ls}, {le}] ({(le - ls) / 48000:.2f}s)")
            notes.append(_process_note(wav, ls, le, note, args.tempo, out_dir, args.align_period, args.production))
    elif args.wav:
        if args.loop_start is None or args.loop_end is None:
            ap.error("--wav requires --loop-start and --loop-end")
        title = args.wav.stem
        notes.append(_process_note(args.wav, args.loop_start, args.loop_end, None, args.tempo, out_dir, args.align_period, args.production))
    else:
        ap.error("provide --xml (+ --note) or --wav (+ --loop-start/--loop-end)")

    index = _write_index(out_dir, title, notes)
    print(f"\nwrote {sum(len(n['rows']) for n in notes)} variants -> {index}")
    if not args.no_open:
        webbrowser.open(index.resolve().as_uri())


if __name__ == "__main__":
    main()

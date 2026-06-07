#!/usr/bin/env python3
"""Per-note loop-quality report for Deluge exports.

Where detect_clicks.py only flags *clicks* (amp/deriv discontinuity), this measures the
things the loop algorithm is blind to and that dominate the ear-findings:

  - loop length in seconds AND in fundamental periods (is it "too close"?)
  - timbre jump across the seam: MFCC cosine distance + spectral-centroid delta between
    the window before loop_end and after loop_start (is it "different tonal points"?)
  - placement: loop length as a fraction of the sustain region

If a debug dir of PATCHPRESS_LOOP_DEBUG JSONL files is given (--debug-dir), each row is
joined by (midi_note, total length) to the internal "why": classification, rms_depth,
chroma-movement stationarity, the winning generator, and the pre-crossfade candidate score
— so you can see, e.g., "class=sustained, t_mod=None, chroma_movement=high, generator=
waveform, len=0.1s" on one line.

Usage:
    # post-hoc only (works on existing output/)
    python tools/loop_report.py "output/Deluge/Mini From Mars" --csv mfm.csv

    # joined with internal diagnostics produced by a PATCHPRESS_LOOP_DEBUG run
    python tools/loop_report.py "output/Deluge/Mini From Mars" \
        --debug-dir output/loopdbg --csv mfm.csv

    # spectrogram + RMS + loop overlay for specific notes
    python tools/loop_report.py "output/Deluge/Surge XT/Doomsday.xml" --plot all
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import re
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree

# Make sibling tools and the package importable regardless of how we're launched.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "src"))

from detect_clicks import _resolve_wav  # noqa: E402
from patch_press.analysis.envelope import analyze_envelope  # noqa: E402
from patch_press.analysis.loop import (  # noqa: E402
    _centroid_movement,
    _chroma_fingerprints,
    _chroma_movement,
    _rms_depth_region,
)
from patch_press.model.audio import AudioBuffer  # noqa: E402

import librosa  # noqa: E402

_SLOPE_WINDOW = 16
_TIMBRE_WIN = 4096  # matches loop._CHROMA_WIN — "what the splice sees"

# Defaults; recalibrate from the Dexed/Odin2 control distribution printed in the summary.
_SHORT_SEC = 0.30
_SHORT_PERIODS = 30.0
_TONAL_MFCC = 0.15
_MOVE_THRESH = 0.04   # chroma-movement above which the sustain is "moving" (timbre evolves)

_CLICK_AMP = 0.25     # matches validate_splice_reason
_CLICK_DERIV = 0.50

_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"

_NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def _note_name(m: int | None) -> str:
    if m is None:
        return "?"
    return f"{_NOTE_NAMES[m % 12]}{m // 12 - 1}"


_NAME_TO_SEMI = {"C": 0, "C#": 1, "D": 2, "D#": 3, "E": 4, "F": 5,
                 "F#": 6, "G": 7, "G#": 8, "A": 9, "A#": 10, "B": 11}


def _parse_note(name: str) -> tuple[int | None, str]:
    """Parse a note from an exported WAV name.

    Two conventions in play: VST/CLAP exports use 'note060' (MIDI number); sample
    libraries keep their own label, e.g. 'Mini_ComboOrgan_C-1' (name + octave).
    Returns (midi_or_None, display_label).
    """
    m = re.search(r"note(\d+)", name)
    if m:
        midi = int(m.group(1))
        return midi, _note_name(midi)
    matches = re.findall(r"([A-G]#?)(-?\d+)", name)
    if matches:
        nm, octv = matches[-1]  # the trailing token is the pitch label
        semi = _NAME_TO_SEMI.get(nm)
        if semi is not None:
            return (int(octv) + 1) * 12 + semi, f"{nm}{octv}"
    return None, "?"


def _iter_zones(xml_path: Path):
    """Yield (fileName, loop_start, loop_end) for every looped zone in a Deluge XML.

    Covers both multisample synths (sampleRange > zone) and kits (osc1 > zone): we find
    every <zone> carrying startLoopPos and read fileName from its parent element.
    """
    root = etree.parse(str(xml_path)).getroot()
    for zone in root.iter("zone"):
        ls, le = zone.get("startLoopPos"), zone.get("endLoopPos")
        if ls is None or le is None:
            continue
        parent = zone.getparent()
        fname = parent.get("fileName") if parent is not None else None
        if fname:
            yield fname, int(ls), int(le)


def _seam_disc(mono: np.ndarray, ls: int, le: int) -> tuple[float, float]:
    """Raw amp/deriv discontinuity at the seam — identical math to validate_splice_reason."""
    n = len(mono)
    peak = float(np.abs(mono).max()) or 1.0
    amp_disc = abs(float(mono[le - 1]) - float(mono[ls])) / peak
    w = _SLOPE_WINDOW
    slope_end = float(mono[le - 1]) - float(mono[max(le - 1 - w, 0)])
    slope_start = float(mono[min(ls + w, n - 1)]) - float(mono[ls])
    deriv_disc = abs(slope_end - slope_start) / (2.0 * peak)
    return amp_disc, deriv_disc


def _timbre_jump(mono: np.ndarray, sr: int, ls: int, le: int) -> tuple[float | None, float | None]:
    """(mfcc cosine distance, |centroid delta| Hz) across the wrap seam.

    Compares content just before loop_end (pre) to just after loop_start (post). High mfcc
    distance with a clean seam == "different tonal points": the loop is click-free but the
    timbre jumps. This is the metric the chroma-only loop score lacks.
    """
    pre = mono[max(0, le - _TIMBRE_WIN):le]
    post = mono[ls:ls + _TIMBRE_WIN]
    if len(pre) < _TIMBRE_WIN // 2 or len(post) < _TIMBRE_WIN // 2:
        return None, None

    def mfcc_vec(x: np.ndarray) -> np.ndarray:
        m = librosa.feature.mfcc(y=x.astype(np.float32), sr=sr, n_mfcc=13)
        return m[1:].mean(axis=1)  # drop coeff 0 (energy) → timbre shape only

    a, b = mfcc_vec(pre), mfcc_vec(post)
    denom = (np.linalg.norm(a) * np.linalg.norm(b)) + 1e-12
    mfcc_dist = 1.0 - float(np.dot(a, b) / denom)

    def centroid(x: np.ndarray) -> float:
        return float(librosa.feature.spectral_centroid(y=x.astype(np.float32), sr=sr).mean())

    cd = abs(centroid(pre) - centroid(post))
    return mfcc_dist, cd


def _mfcc_movement(mono: np.ndarray, sr: int, start: int, end: int, hop: int, win: int = 4096) -> float:
    """Mean cosine distance of per-window MFCC-shape vectors to their centroid over the sustain.

    Unlike chroma_movement (pitch-class, blind to brightness), MFCC captures the spectral
    ENVELOPE, so a filter sweep at constant pitch lights this up. This is the sensor that
    matches the 'filter movement' / 'different tonal points' complaints.
    """
    vecs = []
    for pos in range(start, end - win, hop):
        chunk = mono[pos:pos + win].astype(np.float32)
        m = librosa.feature.mfcc(y=chunk, sr=sr, n_mfcc=13)[1:].mean(axis=1)  # drop energy coeff
        vecs.append(m)
    if len(vecs) < 2:
        return 0.0
    V = np.stack([v / (np.linalg.norm(v) + 1e-12) for v in vecs])
    c = V.mean(axis=0)
    c /= np.linalg.norm(c) + 1e-12
    return float(np.mean(1.0 - V @ c))


def _loop_periods(mono: np.ndarray, sr: int, ls: int, le: int):
    """Fundamental period (samples) and loop length in periods, via YIN on the loop body.

    YIN tracks the fundamental directly, so it won't lock onto a harmonic the way a raw
    autocorrelation argmax can. Pitch is stable across the loop, so a 1 s centre chunk is
    enough and keeps it fast.
    """
    body = mono[ls:le]
    if len(body) < 2048:
        return None, None
    if len(body) > sr:
        mid = len(body) // 2
        body = body[mid - sr // 2: mid + sr // 2]
    try:
        # fmin=25 (below A0=27.5, the lowest rendered note) is the floor that YIN allows
        # at the default frame_length=2048 for 44.1/48k material.
        f0 = librosa.yin(body.astype(np.float32), fmin=25.0, fmax=4000.0, sr=sr)
    except Exception:
        return None, None
    f0 = f0[np.isfinite(f0)]
    if len(f0) == 0:
        return None, None
    med = float(np.median(f0))
    if med <= 0:
        return None, None
    period = sr / med
    return period, (le - ls) / period


def _load_debug(debug_dir: Path) -> list[dict]:
    """Load all PATCHPRESS_LOOP_DEBUG JSONL records (across PID-suffixed files)."""
    recs: list[dict] = []
    for jf in sorted(debug_dir.glob("*.jsonl")):
        with open(jf) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return recs


def _match_debug(recs: list[dict], note: int | None, wav_len: int) -> dict | None:
    """Match a WAV to its record by total length (convention-free), note as tiebreak.

    n_samples at find_loop_candidates time equals the exported WAV length (crossfade and
    normalize don't change length, trim happens earlier), so it's an exact, reliable key
    that sidesteps the two filename note conventions.
    """
    if not recs:
        return None
    exact = [r for r in recs if r.get("n_samples") == wav_len]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        by_n = [r for r in exact if r.get("midi_note") == note]
        return by_n[0] if by_n else exact[0]
    by_n = [r for r in recs if r.get("midi_note") == note]
    pool = by_n or recs
    return min(pool, key=lambda r: abs((r.get("n_samples") or 0) - wav_len))


def _winning_generator(rec: dict | None, ls: int, le: int):
    """Return (generator, candidate_score, reason). Generator 'fallback/other' when the
    exported loop matches no recorded candidate (i.e. pipeline fallback_central_region)."""
    if not rec:
        return None, None, None
    reason = rec.get("reason")
    for c in rec.get("candidates") or []:
        if c.get("start") == ls and c.get("end") == le:
            return c.get("src"), c.get("score"), reason
    return "fallback/other", None, reason


def analyze_zone(xml_path: Path, fname: str, ls: int, le: int, recs: list[dict] | None) -> dict:
    wav = _resolve_wav(xml_path, fname)
    note, label = _parse_note(Path(fname).name)
    row: dict = {
        "preset": xml_path.stem,
        "source": xml_path.parent.name,
        "note": note,
        "note_name": label,
        "loop_start": ls,
        "loop_end": le,
    }
    if wav is None:
        row["error"] = f"WAV not found: {fname}"
        return row

    data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    n = len(mono)
    if ls < 1 or le < 2 or le > n:
        row["error"] = f"loop out of bounds (n={n})"
        return row

    amp_disc, deriv_disc = _seam_disc(mono, ls, le)
    mfcc_dist, cdelta = _timbre_jump(mono, sr, ls, le)
    period, len_periods = _loop_periods(mono, sr, ls, le)

    row.update({
        "sr": sr,
        "total_len_s": round(n / sr, 3),
        "loop_len_s": round((le - ls) / sr, 4),
        "loop_len_periods": round(len_periods, 1) if len_periods else None,
        "fundamental_hz": round(sr / period, 1) if period else None,
        "amp_disc": round(amp_disc, 4),
        "deriv_disc": round(deriv_disc, 4),
        "mfcc_dist": round(mfcc_dist, 4) if mfcc_dist is not None else None,
        "centroid_delta_hz": round(cdelta, 1) if cdelta is not None else None,
    })

    # Post-hoc envelope + movement (always). Crossfade/normalize don't shift the envelope,
    # so these closely track what the pipeline saw. analyze_envelope here runs bpm=None, so
    # class_nobpm is 'sustained_with_modulation' whenever ANY periodicity is found; the
    # joined (real) classification can still be 'sustained' if the pipeline's BPM-snap
    # rejected a free LFO — that gap is itself the free-LFO signal.
    env = analyze_envelope(AudioBuffer(data=data.T.copy(), sample_rate=sr))
    ss, se = env.sustain_start, env.sustain_end
    sustain = max(se - ss, 0)
    monof = mono.astype(np.float32)
    fps = _chroma_fingerprints(monof, sr, ss, se, int(0.5 * sr))
    row.update({
        "class_nobpm": env.classification,
        "chroma_movement": round(_chroma_movement(fps), 4),
        "centroid_cv": round(_centroid_movement(monof, sr, ss, se), 4),
        "mfcc_movement": round(_mfcc_movement(monof, sr, ss, se, int(0.5 * sr)), 4),
        "rms_depth": round(_rms_depth_region(monof, ss, se), 4),
        "sustain_len_s": round(sustain / sr, 3) if sustain else None,
        "loop_frac_sustain": round((le - ls) / sustain, 2) if sustain else None,
    })

    if recs is not None:
        rec = _match_debug(recs, note, n)
        if rec:
            gen, cscore, reason = _winning_generator(rec, ls, le)
            t_mod = rec.get("t_mod")
            row.update({
                "classification": rec.get("classification"),
                "t_mod_s": round(t_mod / sr, 3) if t_mod else None,
                "tempo_bpm": rec.get("tempo_bpm"),
                "generator": gen,
                "cand_score": cscore,
                "reason": reason,
                "n_passed": rec.get("n_passed"),
            })
        else:
            row["reason"] = "no_debug_record"

    row["flags"] = _flags(row)
    return row


def _flags(row: dict) -> str:
    flags = []
    lp = row.get("loop_len_periods")
    ls_s = row.get("loop_len_s")
    short = (ls_s is not None and ls_s < _SHORT_SEC) or (lp is not None and lp < _SHORT_PERIODS)
    if short:
        flags.append("SHORT")
    cm = row.get("chroma_movement")
    moving = cm is not None and cm > _MOVE_THRESH
    if moving:
        flags.append("MOVING")
    cov = row.get("loop_frac_sustain")
    # The real bug signature: the sound moves, but the loop is a short/low-coverage snapshot
    # of it, so each wrap replays one frozen instant of an evolving tone.
    if moving and (short or (cov is not None and cov < 0.5)):
        flags.append("MOVER_SHORT")
    md = row.get("mfcc_dist")
    if md is not None and md > _TONAL_MFCC:
        flags.append("TONAL_JUMP")
    if (row.get("amp_disc") or 0) >= _CLICK_AMP or (row.get("deriv_disc") or 0) >= _CLICK_DERIV:
        flags.append("CLICK")
    if row.get("generator") == "fallback/other":
        flags.append("FALLBACK")
    return ",".join(flags)


def _c(val, warn, fail, lower_is_better=True) -> str:
    if val is None:
        return _DIM
    bad = (val >= fail) if lower_is_better else (val <= fail)
    mid = (val >= warn) if lower_is_better else (val <= warn)
    return _RED if bad else (_YELLOW if mid else _GREEN)


def print_table(rows: list[dict], joined: bool) -> None:
    cur = None
    for r in sorted(rows, key=lambda x: (x["source"], x["preset"], x.get("note") or 0)):
        if r["preset"] != cur:
            cur = r["preset"]
            print(f"\n{r['source']} / {r['preset']}")
        if "error" in r:
            print(f"  {_RED}ERR{_RESET}  {r['note_name']:>4}  {r['error']}")
            continue
        lp = r.get("loop_len_periods")
        lp_s = f"{lp:>6.1f}p" if lp is not None else "    ?p"
        len_c = _c(r["loop_len_s"], _SHORT_SEC * 2, _SHORT_SEC)
        md = r.get("mfcc_dist")
        md_c = _c(md, _TONAL_MFCC, _TONAL_MFCC * 2)
        md_s = f"{md:.3f}" if md is not None else "  ?  "
        cls = (r.get("classification") or r.get("class_nobpm") or "?")[:9]
        cm = r.get("chroma_movement")
        cm_c = _c(cm, _MOVE_THRESH, _MOVE_THRESH * 2)
        cm_s = f"{cm:.3f}" if cm is not None else "  ?  "
        cov = r.get("loop_frac_sustain")
        cov_s = f"{cov:.2f}" if cov is not None else " ?  "
        extra = f"  {_DIM}{cls:<9}{_RESET} mv={cm_c}{cm_s}{_RESET} cov={cov_s}"
        if joined:
            extra += f" {_DIM}gen={(r.get('generator') or '?')[:11]}{_RESET}"
        flag_s = f"  {_RED}{r['flags']}{_RESET}" if r.get("flags") else ""
        print(
            f"  {r['note_name']:>4}  "
            f"len={len_c}{r['loop_len_s']:>6.3f}s{_RESET} {lp_s}  "
            f"mfcc={md_c}{md_s}{_RESET}  "
            f"amp={r['amp_disc']:.3f} drv={r['deriv_disc']:.3f}"
            f"{extra}{flag_s}"
        )


def _median(xs):
    xs = [x for x in xs if x is not None]
    return round(float(np.median(xs)), 3) if xs else None


def _p90(xs):
    xs = [x for x in xs if x is not None]
    return round(float(np.percentile(xs, 90)), 3) if xs else None


def print_summary(rows: list[dict]) -> None:
    print(f"\n{'─' * 78}\nPER-SOURCE SUMMARY  (medians; p90 for movement/mfcc)")
    by_src: dict[str, list[dict]] = {}
    for r in rows:
        if "error" not in r:
            by_src.setdefault(r["source"], []).append(r)
    for src, rs in sorted(by_src.items()):
        n = len(rs)
        def cnt(flag: str) -> int:
            return sum(1 for r in rs if flag in (r.get("flags") or ""))

        short, moving, ms = cnt("SHORT"), cnt("MOVING"), cnt("MOVER_SHORT")
        tonal, click, fb = cnt("TONAL_JUMP"), cnt("CLICK"), cnt("FALLBACK")
        cls: dict = {}
        for r in rs:
            k = r.get("classification") or r.get("class_nobpm")
            cls[k] = cls.get(k, 0) + 1
        cls_s = " ".join(f"{k}:{v}" for k, v in cls.items() if k) or "—"
        print(
            f"\n  {src}  (n={n})\n"
            f"    loop_len_s med={_median([r.get('loop_len_s') for r in rs])}  "
            f"len_periods med={_median([r.get('loop_len_periods') for r in rs])}  "
            f"total_len_s med={_median([r.get('total_len_s') for r in rs])}\n"
            f"    chroma_mv med={_median([r.get('chroma_movement') for r in rs])} "
            f"p90={_p90([r.get('chroma_movement') for r in rs])}  "
            f"centroid_cv med={_median([r.get('centroid_cv') for r in rs])} "
            f"p90={_p90([r.get('centroid_cv') for r in rs])}\n"
            f"    mfcc_mv med={_median([r.get('mfcc_movement') for r in rs])} "
            f"p90={_p90([r.get('mfcc_movement') for r in rs])}  "
            f"seam_mfcc p90={_p90([r.get('mfcc_dist') for r in rs])}\n"
            f"    SHORT={short}/{n}  MOVING={moving}/{n}  MOVER_SHORT={ms}/{n}  "
            f"TONAL_JUMP={tonal}/{n}  CLICK={click}/{n}  FALLBACK={fb}/{n}\n"
            f"    class: {cls_s}"
        )


def write_csv(rows: list[dict], path: Path) -> None:
    cols = [
        "source", "preset", "note", "note_name", "loop_start", "loop_end",
        "loop_len_s", "loop_len_periods", "fundamental_hz", "total_len_s",
        "amp_disc", "deriv_disc", "mfcc_dist", "centroid_delta_hz",
        "class_nobpm", "classification", "rms_depth",
        "chroma_movement", "centroid_cv", "mfcc_movement",
        "t_mod_s", "tempo_bpm", "sustain_len_s", "loop_frac_sustain",
        "generator", "cand_score", "n_passed", "reason", "flags", "error",
    ]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x["source"], x["preset"], x.get("note") or 0)):
            w.writerow(r)
    print(f"\nWrote {len(rows)} rows → {path}")


def plot_zone(xml_path: Path, fname: str, ls: int, le: int, out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import librosa.display  # noqa: F401  (registers specshow)

    wav = _resolve_wav(xml_path, fname)
    if wav is None:
        print(f"  plot skip (WAV not found): {fname}")
        return
    data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    t = np.arange(len(mono)) / sr
    _, label = _parse_note(Path(fname).name)

    fig, (ax0, ax1, ax2) = plt.subplots(3, 1, figsize=(13, 8), constrained_layout=True)
    S = librosa.amplitude_to_db(np.abs(librosa.stft(mono, n_fft=2048, hop_length=512)), ref=np.max)
    librosa.display.specshow(S, sr=sr, hop_length=512, x_axis="time", y_axis="log", ax=ax0)
    ax0.set(title=f"{xml_path.stem} — {label} — loop [{ls}→{le}]  ({(le-ls)/sr:.3f}s)")
    rms = librosa.feature.rms(y=mono, hop_length=512)[0]
    ax1.plot(np.arange(len(rms)) * 512 / sr, rms, lw=0.8)
    ax1.set(ylabel="RMS", xlim=(0, t[-1]))
    ax2.plot(t, mono, lw=0.4)
    ax2.set(ylabel="wave", xlabel="time (s)", xlim=(0, t[-1]))
    for ax in (ax0, ax1, ax2):
        ax.axvline(ls / sr, color="lime", lw=1.2)
        ax.axvline(le / sr, color="red", lw=1.2)
    ax1.axvspan(ls / sr, le / sr, color="cyan", alpha=0.15)

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{xml_path.stem}_{label}.png".replace("#", "sharp")
    fig.savefig(out, dpi=90)
    plt.close(fig)
    print(f"  plot → {out}")


def main() -> None:
    global _SHORT_SEC, _SHORT_PERIODS, _TONAL_MFCC, _MOVE_THRESH
    ap = argparse.ArgumentParser(description="Per-note loop-quality report for Deluge exports")
    ap.add_argument("path", type=Path, help="Deluge XML file or directory of XMLs")
    ap.add_argument("--debug-dir", type=Path, help="dir of PATCHPRESS_LOOP_DEBUG *.jsonl files")
    ap.add_argument("--csv", type=Path, help="write per-note CSV here")
    ap.add_argument("--plot", help="'all' or comma-separated MIDI notes to plot")
    ap.add_argument("--plot-dir", type=Path, default=Path("loop_plots"))
    ap.add_argument("--short-sec", type=float, default=_SHORT_SEC)
    ap.add_argument("--short-periods", type=float, default=_SHORT_PERIODS)
    ap.add_argument("--tonal-mfcc", type=float, default=_TONAL_MFCC)
    ap.add_argument("--move-thresh", type=float, default=_MOVE_THRESH)
    args = ap.parse_args()
    _SHORT_SEC, _SHORT_PERIODS, _TONAL_MFCC = args.short_sec, args.short_periods, args.tonal_mfcc
    _MOVE_THRESH = args.move_thresh

    if args.path.is_dir():
        xmls = sorted(args.path.rglob("*.xml"))
    elif args.path.suffix.lower() == ".xml":
        xmls = [args.path]
    else:
        print(f"Error: expected an XML file or directory — got {args.path}", file=sys.stderr)
        sys.exit(1)
    if not xmls:
        print("No XML files found.")
        return

    dbg_recs = _load_debug(args.debug_dir) if args.debug_dir else None
    if dbg_recs is not None:
        print(f"Loaded {len(dbg_recs)} debug records from {args.debug_dir}")

    plot_set = None
    if args.plot:
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            print("--plot needs matplotlib (pip install matplotlib); skipping plots.", file=sys.stderr)
        else:
            plot_set = "all" if args.plot.strip().lower() == "all" else {int(x) for x in args.plot.split(",")}

    rows: list[dict] = []
    for xml in xmls:
        for fname, ls, le in _iter_zones(xml):
            rows.append(analyze_zone(xml, fname, ls, le, dbg_recs))
            if plot_set is not None:
                note, _ = _parse_note(Path(fname).name)
                if plot_set == "all" or note in plot_set:
                    plot_zone(xml, fname, ls, le, args.plot_dir)

    if not rows:
        print("No looped zones found.")
        return

    print_table(rows, joined=dbg_recs is not None)
    print_summary(rows)
    if args.csv:
        write_csv(rows, args.csv)


if __name__ == "__main__":
    main()

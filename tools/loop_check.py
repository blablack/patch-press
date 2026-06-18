#!/usr/bin/env python3
"""Objective seam-quality check for the loops patch-press ships — no ears required.

Every audible loop defect has a numeric fingerprint, so this measures the loops we
actually export and ranks them worst-first. Lets the model (and you) catch clicks /
level-pumping / timbre jumps across a whole bank in seconds, instead of copying to the
Deluge and listening to 88 notes. The subjective "does it sound *good*" call still
belongs to your ears via tools/loop_audition.py — this only flags the objectively broken.

Two modes:

    # Check the rendered output (every looped zone in every preset XML), worst first:
    python tools/loop_check.py output
    python tools/loop_check.py "output/Deluge/SYNTHS/Dexed/RUMBLE   1.xml"

    # Check one candidate on RAW audio (pre-crossfade) — for iterating on loop code:
    python tools/loop_check.py --wav some.wav --loop-start 112205 --loop-end 200358

What the columns mean (all measured at the on-device wrap mono[end-1] -> mono[start]):

  amp_disc   value step at the wrap / peak       — the click. Same metric & threshold
             as the pipeline's validate_splice_reason (>= 0.25 fails -> CLICK).
  deriv_disc forward-slope mismatch across wrap   — phase/shape kink (>= 0.50 fails -> CLICK).
  lvl_step   |level(start) - level(end)| / louder side, 0..1 — INFORMATIONAL.
  spec_disc  spectral cosine distance start vs end — INFORMATIONAL.
  len_s      loop length in seconds               — you want these LONG for evolving synths.

Only an instantaneous discontinuity (amp/deriv) is an objective defect — it clicks on any
sound. lvl_step and spec_disc are deliberately NOT verdicts: a long loop on an evolving /
decaying synth legitimately spans a level and timbre change (the whole point of a long
loop), so flagging those would cry wolf on exactly the loops you want. They're shown so you
(or the model) can spot an outlier worth auditioning by ear with tools/loop_audition.py.
Very short len_s on a sustained synth is also worth a look — that's the short-loop bug.

NOTE on rendered output: the exported WAV already has patch-press's loop crossfade baked
in, which is exactly what the Deluge plays (the device itself does no crossfade). So
amp_disc/deriv_disc here are the *real* on-device seam — a large value means the bake
failed to hide a genuine click. rms_step/spec_disc are measured just outside the crossfade
window, so they expose pumping/timbre drift the bake cannot mask. In --wav mode the audio
is raw, so the numbers are the intrinsic splice quality (what validate_splice_reason gates).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import soundfile as sf
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from patch_press.analysis.loop import (  # noqa: E402
    _AMP_DISC_THRESHOLD,
    _DERIV_DISC_THRESHOLD,
    _SLOPE_WINDOW,
    _seam_slopes,
)

_GUARD_MS = 25.0               # skip this much each side of the wrap (past the baked crossfade)
_WIN_MS = 50.0                 # window for the level/spectral comparison


def _resolve_wav(xml_path: Path, file_name: str) -> Path:
    # fileName is SD-root-relative ("SAMPLES/..."); XML lives at <root>/SYNTHS/<plugin>/<preset>.xml.
    return xml_path.parent.parent.parent / file_name


def _zones_from_xml(xml_path: Path) -> list[tuple[str, Path, int, int]]:
    """(label, wav_path, loop_start, loop_end) for every looped zone in a preset XML."""
    root = etree.parse(str(xml_path)).getroot()
    out: list[tuple[str, Path, int, int]] = []
    for sr_el in root.findall(".//sampleRange"):
        zone = sr_el.find("zone")
        if zone is None or "startLoopPos" not in zone.attrib or "endLoopPos" not in zone.attrib:
            continue
        ls, le = int(zone.get("startLoopPos")), int(zone.get("endLoopPos"))
        if le <= ls:
            continue
        fname = sr_el.get("fileName", "")
        label = Path(fname).stem or sr_el.get("rangeTopNote", "?")
        out.append((label, _resolve_wav(xml_path, fname), ls, le))
    return out


def _cos_dist(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


def measure(mono: np.ndarray, sr: int, start: int, end: int) -> dict | None:
    """Seam metrics at the on-device wrap mono[end-1] -> mono[start]. None if out of bounds."""
    n = len(mono)
    if start < 1 or end < 2 or end > n or end <= start:
        return None
    peak = float(np.abs(mono).max()) or 1.0

    amp_disc = abs(float(mono[end - 1]) - float(mono[start])) / peak
    s_start, s_end = _seam_slopes(mono, start, end, _SLOPE_WINDOW)
    deriv_disc = abs(s_end - s_start) / (2.0 * peak)

    guard = int(_GUARD_MS * sr / 1000)
    win = int(_WIN_MS * sr / 1000)
    body = end - start
    win = min(win, max(8, body // 2 - guard))
    lvl_step = spec_disc = 0.0
    if win >= 8 and start + guard + win <= end - guard and end - guard - win >= start:
        post = mono[start + guard : start + guard + win]
        pre = mono[end - guard - win : end - guard]
        r_post = float(np.sqrt(np.mean(post ** 2)))
        r_pre = float(np.sqrt(np.mean(pre ** 2)))
        louder = max(r_post, r_pre, 1e-9)
        lvl_step = abs(r_pre - r_post) / louder  # 0 = same level, ->1 = one side near silent
        w = np.hanning(len(post))
        mp = np.abs(np.fft.rfft(post * w))
        me = np.abs(np.fft.rfft(pre * w))
        spec_disc = _cos_dist(mp, me)

    # Only an instantaneous discontinuity is an objective defect (it clicks on any sound).
    # lvl_step / spec_disc are informational — a long loop on an evolving synth spans them
    # by design, so they must not flip the verdict.
    verdict = "CLICK" if (amp_disc >= _AMP_DISC_THRESHOLD or deriv_disc >= _DERIV_DISC_THRESHOLD) else "ok"
    severity = max(amp_disc / _AMP_DISC_THRESHOLD, deriv_disc / _DERIV_DISC_THRESHOLD)
    return {
        "len_s": body / sr, "amp_disc": amp_disc, "deriv_disc": deriv_disc,
        "lvl_step": lvl_step, "spec_disc": spec_disc,
        "verdict": verdict, "severity": severity,
    }


def _read_mono(wav: Path) -> tuple[np.ndarray, int]:
    data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
    return data.mean(axis=1), sr


def _iter_xmls(paths: list[Path]) -> list[Path]:
    xmls: list[Path] = []
    for p in paths:
        if p.is_dir():
            xmls.extend(sorted(p.rglob("*.xml")))
        elif p.suffix.lower() == ".xml":
            xmls.append(p)
    return xmls


def main() -> None:
    ap = argparse.ArgumentParser(description="Rank exported loop seams worst-first (objective click/pump/timbre check).")
    ap.add_argument("paths", nargs="*", type=Path, help="XML files or dirs to scan (default: output/)")
    ap.add_argument("--wav", type=Path, help="check one WAV with explicit loop points (raw, pre-crossfade)")
    ap.add_argument("--loop-start", type=int)
    ap.add_argument("--loop-end", type=int)
    ap.add_argument("--all", action="store_true", help="show every loop, not just flagged ones")
    ap.add_argument("--top", type=int, default=0, help="show only the N worst rows")
    args = ap.parse_args()

    rows: list[tuple[str, str, dict]] = []  # (preset, label, metrics)

    if args.wav:
        if args.loop_start is None or args.loop_end is None:
            ap.error("--wav needs --loop-start and --loop-end")
        mono, sr = _read_mono(args.wav)
        m = measure(mono, sr, args.loop_start, args.loop_end)
        if m is None:
            raise SystemExit(f"bad loop points for {args.wav.name}: {args.loop_start}..{args.loop_end} (n={len(mono)})")
        rows.append((args.wav.stem, f"{args.loop_start}..{args.loop_end}", m))
    else:
        xmls = _iter_xmls(args.paths or [Path("output")])
        if not xmls:
            raise SystemExit("no XML files found — pass an output dir or XML path")
        for xml in xmls:
            preset = xml.stem
            for label, wav, ls, le in _zones_from_xml(xml):
                if not wav.exists():
                    print(f"  ! WAV missing: {wav}", file=sys.stderr)
                    continue
                mono, sr = _read_mono(wav)
                m = measure(mono, sr, ls, le)
                if m is not None:
                    rows.append((preset, label, m))

    if not rows:
        print("no looped zones found.")
        return

    rows.sort(key=lambda r: r[2]["severity"], reverse=True)
    shown = [r for r in rows if args.all or r[2]["verdict"] != "ok"]
    if args.top:
        shown = shown[: args.top]

    hdr = f"{'PRESET':<22} {'NOTE/ZONE':<26} {'len_s':>6} {'amp':>6} {'deriv':>6} {'lvl':>5} {'spec':>5}  VERDICT"
    print(hdr)
    print("-" * len(hdr))
    for preset, label, m in shown:
        print(
            f"{preset[:22]:<22} {label[:26]:<26} {m['len_s']:>6.2f} {m['amp_disc']:>6.3f} "
            f"{m['deriv_disc']:>6.3f} {m['lvl_step']:>5.2f} {m['spec_disc']:>5.2f}  {m['verdict']}"
        )

    from collections import Counter
    counts = Counter(m["verdict"] for _, _, m in rows)
    total = len(rows)
    clicks = counts.get("CLICK", 0)
    print("-" * len(hdr))
    print(f"{total} loops checked  (CLICK={clicks}, ok={counts.get('ok', 0)})"
          + ("  — no clicks" if not clicks else ""))
    if not args.all and not shown:
        print("re-run with --all to see every loop (lvl/spec columns help spot outliers to audition).")


if __name__ == "__main__":
    main()

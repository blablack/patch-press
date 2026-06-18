#!/usr/bin/env python3
"""Find a seam metric that reproduces the user's EAR labels.

The shipped click/length checks (tools/loop_check.py) already match the user on clicks and
short loops. They do NOT catch the dominant complaint — "audible spectral difference across
the seam" — and the obvious static start-vs-end spectral distance is uncorrelated with the
ear (a smoothly-evolving loop can have a big start/end difference yet sound fine, because
the seam change is no faster than the loop's own evolution).

This harness computes SEVERAL candidate features on the user's labeled notes and prints them
grouped by label, so we can SEE which feature separates "audible" from "good" before
committing it to the verdict. Run, eyeball the separation, keep the winner.

Hypothesis under test: the audible defect is a LOCAL spectral discontinuity at the wrap —
spectral flux at the seam that spikes ABOVE the loop's own in-body flux. Relative to each
sound's natural rate of change, so an evolving-but-clean loop (high body flux, matching seam
flux) scores low while a true discontinuity scores high.

    python tools/loop_seam_experiment.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))
sys.path.insert(0, str(_ROOT / "src"))
from loop_check import _zones_from_xml  # noqa: E402

OUTPUT = Path("output")
CONFIG_DIR = OUTPUT / "Configs"

# (preset xml stem, midi note) -> "audible" | "good". From the user's ear notes.
# Short-loop failures (BANKS 60, ENCOUNTERS 96) are excluded — length already catches those;
# here we only need to separate audible-spectral from good among real (long) loops.
LABELS: dict[tuple[str, int], str] = {
    # ELECTRON 1 — user: ALL loops audible
    **{("ELECTRON 1", n): "audible" for n in (24, 36, 48, 60, 72, 84, 96, 108)},
    # BANKS, T.
    ("BANKS, T.", 48): "audible",
    ("BANKS, T.", 72): "audible",
    ("BANKS, T.", 24): "good",
    ("BANKS, T.", 84): "good",
    ("BANKS, T.", 96): "good",
    ("BANKS, T.", 108): "good",
    # ENCOUNTERS
    ("ENCOUNTERS", 60): "audible",
    ("ENCOUNTERS", 72): "good",
    ("ENCOUNTERS", 84): "good",
    ("ENCOUNTERS", 108): "good",
    # MIRIDOR 1 — user: all good
    **{("MIRIDOR 1", n): "good" for n in (60, 72, 84)},
    # ARP 2600 / C,D,Eb,F — user: perfect (simple waveforms)
    **{("ARP 2600", n): "good" for n in (60, 72, 84)},
    **{("C,D,Eb,F", n): "good" for n in (60, 72, 84)},
}

_NFFT = 1024
_HOP = 256
_REPEATS = 4


def _find_zone(stem: str, note: int):
    xml = next((p for p in OUTPUT.rglob(f"{stem}.xml")), None)
    if xml is None:
        return None
    tag = f"note{note:03d}"
    for label, wav, ls, le in _zones_from_xml(xml):
        if tag in label:
            return wav, ls, le
    return None


_CONFIG_BY_NAME: dict[str, Path] | None = None
_ADAPTERS: dict[str, object] = {}


def _config_for(stem: str) -> Path | None:
    """Map a preset XML stem to its config by matching output.name (cached)."""
    global _CONFIG_BY_NAME
    if _CONFIG_BY_NAME is None:
        from patch_press.config.loader import load_config
        _CONFIG_BY_NAME = {}
        for y in CONFIG_DIR.rglob("*.yaml"):
            try:
                _CONFIG_BY_NAME[load_config(y).output.name.strip()] = y
            except Exception:
                pass
    return _CONFIG_BY_NAME.get(stem)


def _raw_mono(stem: str, note: int, ls: int, le: int, shipped_len: int):
    """Re-render the note RAW (no crossfade/normalize), trimmed to align with the shipped WAV.

    Returns mono float array, or None if the render can't be length-aligned to the shipped
    output (so the XML loop points wouldn't apply).
    """
    from patch_press.config.loader import load_config
    from patch_press.io.adapters.vst import VSTAdapter
    from patch_press.analysis.trim import trim_bounds

    cfg_path = _config_for(stem)
    if cfg_path is None:
        return None
    if stem not in _ADAPTERS:
        cfg = load_config(cfg_path)
        ad = VSTAdapter(cfg.source)
        ad._apply_preset(cfg.source.preset)
        _ADAPTERS[stem] = (ad, cfg)
    ad, cfg = _ADAPTERS[stem]
    cap = cfg.capture
    audio = ad.render_note(note, 100, cap.duration_s, cap.duration_s + cap.release_tail_s,
                           cap.tempo_bpm, cap.sample_rate)
    lead, trail = trim_bounds(audio)
    mono = audio.data.mean(axis=0)[lead:trail]
    if len(mono) != shipped_len:
        print(f"  ! length mismatch {stem} n{note}: raw={len(mono)} shipped={shipped_len} — skipping", file=sys.stderr)
        return None
    return mono


def _features(mono: np.ndarray, sr: int, ls: int, le: int) -> dict:
    L = le - ls
    peak = float(np.abs(mono).max()) or 1.0

    # Build the actually-played looped signal: intro, then the loop body repeated.
    body = mono[ls:le]
    sig = np.concatenate([mono[:ls]] + [body] * _REPEATS)
    wrap_positions = [ls + k * L for k in range(1, _REPEATS)]  # sample idx of each wrap

    S = np.abs(librosa.stft(sig, n_fft=_NFFT, hop_length=_HOP))
    flux = np.sqrt(((np.diff(S, axis=1)) ** 2).sum(axis=0))  # per-frame spectral flux
    flux = flux / (S[:, 1:].sum(axis=0) + 1e-9)              # normalise by frame energy
    n_frames = len(flux)

    wrap_frames = sorted({min(max(p // _HOP - 1, 0), n_frames - 1) for p in wrap_positions})
    near = set()
    for wf in wrap_frames:
        near.update(range(wf - 2, wf + 3))
    body_frames = [i for i in range(n_frames) if i not in near]

    seam_flux = float(np.median([flux[max(0, wf - 1): wf + 2].max() for wf in wrap_frames])) if wrap_frames else 0.0
    body_flux = float(np.median([flux[i] for i in body_frames])) if body_frames else 1e-9
    flux_ratio = seam_flux / (body_flux + 1e-9)

    # In-loop spectral DRIFT: does the loop region keep evolving (non-cyclic), so the repeat is
    # audible regardless of seam quality? Compare averaged spectra of the loop's first vs last
    # third (monotonic drift -> high; a cyclic LFO loop that returns to its start -> low).
    third = max(1, L // 3)
    sa = np.abs(librosa.stft(body[:third], n_fft=_NFFT, hop_length=_HOP)).mean(axis=1)
    sb = np.abs(librosa.stft(body[-third:], n_fft=_NFFT, hop_length=_HOP)).mean(axis=1)
    na, nb = np.linalg.norm(sa), np.linalg.norm(sb)
    drift = 1.0 - float(sa @ sb / (na * nb)) if na > 1e-9 and nb > 1e-9 else 0.0

    # Static start-vs-end spectral distance (the current spec_disc, for reference).
    guard = int(0.025 * sr)
    win = min(int(0.05 * sr), max(8, L // 2 - guard))
    static_spec = 0.0
    if win >= 8 and ls + guard + win <= le - guard:
        a = mono[ls + guard: ls + guard + win] * np.hanning(win)
        b = mono[le - guard - win: le - guard] * np.hanning(win)
        ma, mb = np.abs(np.fft.rfft(a)), np.abs(np.fft.rfft(b))
        na, nb = np.linalg.norm(ma), np.linalg.norm(mb)
        static_spec = 1.0 - float(ma @ mb / (na * nb)) if na > 1e-9 and nb > 1e-9 else 0.0

    # Crossfade phase-cancellation: RMS dip right at the wrap vs just around it.
    dip = 0.0
    w = int(0.012 * sr)
    if wrap_positions and wrap_positions[0] - 2 * w > 0 and wrap_positions[0] + 2 * w < len(sig):
        p = wrap_positions[0]
        at = float(np.sqrt(np.mean(sig[p - w: p + w] ** 2)))
        around = float(np.sqrt(np.mean(np.concatenate([sig[p - 2 * w: p - w], sig[p + w: p + 2 * w]]) ** 2)))
        dip = (around - at) / (around + 1e-9)  # >0 means energy dips at the wrap

    return {
        "len_s": L / sr,
        "flux_ratio": flux_ratio,
        "body_flux": body_flux,
        "drift": drift,
        "static_spec": static_spec,
        "amp_disc": abs(float(mono[le - 1]) - float(mono[ls])) / peak,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Find a seam metric matching the user's ear labels.")
    ap.add_argument("--raw", action="store_true",
                    help="re-render each note RAW (pre-crossfade) instead of reading the baked output WAV")
    args = ap.parse_args()
    print(f"source: {'RAW re-render (pre-crossfade)' if args.raw else 'baked output WAV'}\n")

    rows: list[tuple[str, str, dict]] = []
    for (stem, note), lab in sorted(LABELS.items()):
        z = _find_zone(stem, note)
        if z is None:
            print(f"  ! not found: {stem} note{note}", file=sys.stderr)
            continue
        wav, ls, le = z
        if not wav.exists():
            print(f"  ! wav missing: {wav}", file=sys.stderr)
            continue
        data, sr = sf.read(str(wav), dtype="float32", always_2d=True)
        if args.raw:
            mono = _raw_mono(stem, note, ls, le, data.shape[0])
            if mono is None:
                continue
        else:
            mono = data.mean(axis=1)
        rows.append((lab, f"{stem} n{note}", _features(mono, sr, ls, le)))

    keys = ["len_s", "flux_ratio", "body_flux", "drift", "static_spec", "amp_disc"]
    hdr = f"{'LABEL':<8} {'NOTE':<18} " + " ".join(f"{k:>11}" for k in keys)
    print(hdr); print("-" * len(hdr))
    for lab in ("audible", "good"):
        for _, name, f in [r for r in rows if r[0] == lab]:
            print(f"{lab:<8} {name:<18} " + " ".join(f"{f[k]:>11.3f}" for k in keys))
        print()

    print("=" * len(hdr))
    print(f"{'GROUP MEANS':<27} " + " ".join(f"{k:>11}" for k in keys))
    for lab in ("audible", "good"):
        grp = [r[2] for r in rows if r[0] == lab]
        if grp:
            means = {k: float(np.mean([g[k] for g in grp])) for k in keys}
            print(f"{lab:<27} " + " ".join(f"{means[k]:>11.3f}" for k in keys))
    # Separation quality per feature: how cleanly do the two groups split?
    aud = [r[2] for r in rows if r[0] == "audible"]
    good = [r[2] for r in rows if r[0] == "good"]
    if aud and good:
        print("\nseparation (audible median vs good 90th-pctile; >1 means a usable threshold exists):")
        for k in keys:
            a_med = float(np.median([g[k] for g in aud]))
            g_hi = float(np.percentile([g[k] for g in good], 90))
            print(f"  {k:<12} audible_med={a_med:7.3f}  good_p90={g_hi:7.3f}  margin={a_med - g_hi:+.3f}")


if __name__ == "__main__":
    main()

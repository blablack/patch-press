#!/usr/bin/env python3
"""
Diagnostic tool for sustain/pluck detection on specific presets.
Usage:
    python tools/probe_debug.py input/Probes/Dexed/006_lavitar_y.yaml input/Probes/Dexed/004_pharoh.yaml
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from patch_press.analysis.probe import (
    LONG_HOLD_S,
    SHORT_HOLD_S,
    _HOP,
    _SILENCE_DB,
    _SUSTAIN_RATIO_THRESHOLD,
    classify_sustain_type,
    probe,
)
from patch_press.config.schema import VSTSourceConfig
from patch_press.io.adapters.vst import VSTAdapter

_PROBE_RELEASE_S = 4.0
_TOTAL_S = LONG_HOLD_S + _PROBE_RELEASE_S
_PROBE_NOTE = 60
_PROBE_VEL = 100


def _rms_env(audio, sr: int) -> np.ndarray:
    mono = audio.data.mean(axis=0)
    n = (len(mono) + _HOP - 1) // _HOP
    out = np.zeros(n)
    for i in range(n):
        chunk = mono[i * _HOP: (i + 1) * _HOP]
        out[i] = float(np.sqrt(np.mean(chunk**2))) if len(chunk) else 0.0
    return out


def analyse(probe_yaml: Path, adapter: VSTAdapter, preset_name: str) -> None:
    print(f"\n{'='*60}")
    print(f"Preset: {preset_name}")
    print(f"{'='*60}")

    adapter._apply_preset(preset_name)
    short_audio = adapter.render_note(_PROBE_NOTE, _PROBE_VEL, SHORT_HOLD_S, _TOTAL_S)
    long_audio = adapter.render_note(_PROBE_NOTE, _PROBE_VEL, LONG_HOLD_S, _TOTAL_S)

    sr = long_audio.sample_rate

    # Existing classify_sustain_type logic (inlined for inspection)
    start = int(LONG_HOLD_S * sr)
    end = start + sr  # window_s=1.0
    def _wrms(buf):
        if buf.data.shape[1] <= start:
            return 0.0
        seg = buf.data[:, start: min(end, buf.data.shape[1])]
        return float(np.sqrt(np.mean(seg**2)))

    rms_short_at_noteoff = _wrms(short_audio)
    rms_long_at_noteoff = _wrms(long_audio)
    ratio = rms_long_at_noteoff / (rms_short_at_noteoff + 1e-9)
    sustains_by_ratio = ratio > _SUSTAIN_RATIO_THRESHOLD

    print(f"  RMS at note-off ({LONG_HOLD_S:.0f}s window):")
    print(f"    long render  : {rms_long_at_noteoff:.6f}")
    print(f"    short render : {rms_short_at_noteoff:.6f}")
    print(f"    ratio        : {ratio:.1f}  (threshold={_SUSTAIN_RATIO_THRESHOLD})")
    print(f"    sustains (by ratio): {sustains_by_ratio}")

    # Hold envelope decay analysis
    rms = _rms_env(long_audio, sr)
    hold_hops = int(LONG_HOLD_S * sr) // _HOP
    hold_rms = rms[:hold_hops]

    if len(hold_rms) > 0:
        peak = hold_rms.max()
        silence_thr = peak * (10 ** (_SILENCE_DB / 20))

        # Split hold into quarters and show average RMS
        q = max(1, len(hold_rms) // 4)
        quarters = []
        for i in range(4):
            seg = hold_rms[i*q: (i+1)*q]
            quarters.append(seg.mean() if len(seg) > 0 else 0.0)

        print(f"\n  Hold envelope (peak={peak:.6f}, silence_thr={silence_thr:.6f}):")
        labels = ["Q1 (0-25%)", "Q2 (25-50%)", "Q3 (50-75%)", "Q4 (75-100%)"]
        for lbl, v in zip(labels, quarters):
            db = 20 * np.log10(v / peak + 1e-12)
            print(f"    {lbl}: {v:.6f}  ({db:+.1f} dB rel peak)")

        late_start = max(0, int(len(hold_rms) * 0.8))
        late_rms = hold_rms[late_start:].mean() if len(hold_rms[late_start:]) > 0 else 0.0
        late_ratio = late_rms / (peak + 1e-9)
        late_db = 20 * np.log10(late_ratio + 1e-12)
        print(f"\n  Late-hold vs peak ratio (last 20%): {late_ratio:.4f}  ({late_db:+.1f} dB)")
        print(f"  Pluck-like if ratio < 0.10 (< -20 dB): {late_ratio < 0.10}")

    # Probe result
    sustains = classify_sustain_type(short_audio, long_audio)
    result = probe(long_audio, LONG_HOLD_S, sustains_hint=sustains)
    print(f"\n  classify_sustain_type → {sustains}")
    print(f"  probe result: sustains={result.sustains}, duration={result.duration_s}s, release={result.release_tail_s}s")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: probe_debug.py <probe.yaml> [probe.yaml ...]")
        sys.exit(1)

    yaml_paths = [Path(p) for p in sys.argv[1:]]

    # Load plugin from first YAML
    first_data = yaml.safe_load(yaml_paths[0].read_text())
    plugin_path = Path(first_data["source"]["plugin"])

    state_map: dict[str, "VSTSourceConfig"] = {}
    preset_names: list[str] = []
    for yp in yaml_paths:
        data = yaml.safe_load(yp.read_text())
        src = data["source"]
        name = src["preset"]
        state_map[name] = VSTSourceConfig(
            plugin=plugin_path,
            preset=name,
            raw_state=src.get("raw_state"),
        )
        preset_names.append(name)

    print(f"Loading plugin: {plugin_path}")
    adapter = VSTAdapter(VSTSourceConfig(plugin=plugin_path), state_map=state_map)

    for name in preset_names:
        yp = yaml_paths[preset_names.index(name)]
        analyse(yp, adapter, name)


if __name__ == "__main__":
    main()

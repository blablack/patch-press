from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..analysis.probe import ProbeResult, probe
from ..config.schema import VSTSourceConfig
from ..io.adapters.vst import VSTAdapter


@dataclass
class ScanSummary:
    total: int
    written: list[Path] = field(default_factory=list)
    reviews: list[tuple[str, ProbeResult]] = field(default_factory=list)


def _sanitize(name: str) -> str:
    s = re.sub(r"[^\w]", "_", name)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def _write_config(
    preset_name: str,
    plugin_path: Path,
    result: ProbeResult,
    config_dir: Path,
    deluge_path: Path,
    plugin_stem: str,
    profile: str,
    fmt: str,
) -> Path:
    safe_name = _sanitize(preset_name)

    review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""

    meta = f"confidence={result.confidence}"
    if result.loop_quality is not None:
        meta += f" loop_quality={result.loop_quality:.2f}"
    meta += f" sustains={'yes' if result.sustains else 'no'}"
    meta += f" release={result.release_tail_s:.1f}s"

    content = (
        f"{review_line}"
        f"# {meta}\n"
        f"source:\n"
        f"  type: vst\n"
        f"  plugin: {plugin_path}\n"
        f'  preset: "{preset_name}"\n'
        f"\n"
        f"profile: {profile}\n"
        f"\n"
        f"capture:\n"
        f"  duration_s: {result.duration_s}\n"
        f"  release_tail_s: {result.release_tail_s}\n"
        f"\n"
        f"analysis:\n"
        f"  loop: {'true' if result.loop else 'false'}\n"
        f"\n"
        f"output:\n"
        f"  format: {fmt}\n"
        f"  path: {deluge_path}\n"
        f"  name: {plugin_stem}_{safe_name}\n"
    )

    out = config_dir / f"{safe_name}.yaml"
    out.write_text(content)
    return out


def scan_vst(
    plugin_path: Path,
    config_dir: Path,
    deluge_path: Path,
    profile: str = "synth",
    fmt: str = "deluge",
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_hold_s: float = 6.0,
    probe_release_s: float = 4.0,
    sustain_duration_s: float = 4.0,
) -> ScanSummary:
    config_dir.mkdir(parents=True, exist_ok=True)
    adapter = VSTAdapter(VSTSourceConfig(plugin=plugin_path))
    presets = adapter.list_presets()
    plugin_stem = plugin_path.stem
    summary = ScanSummary(total=len(presets))

    for i, preset_name in enumerate(presets, 1):
        print(f"  [{i}/{summary.total}] {preset_name}", flush=True)
        audio = adapter.probe_preset(
            preset_name, probe_note, probe_velocity, probe_hold_s, probe_release_s,
        )
        result = probe(audio, probe_hold_s)
        if result.sustains:
            result = replace(result, duration_s=sustain_duration_s)
        path = _write_config(
            preset_name, plugin_path, result,
            config_dir, deluge_path, plugin_stem, profile, fmt,
        )
        summary.written.append(path)
        if result.confidence != "high" or result.flags:
            summary.reviews.append((preset_name, result))

    return summary

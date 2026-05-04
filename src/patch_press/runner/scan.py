from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import yaml

log = logging.getLogger(__name__)

from ..analysis.probe import ProbeResult, probe
from ..config.schema import VSTSourceConfig
from ..io.adapters.library import _parse_note_rr
from ..io.adapters.vst import VSTAdapter
from ..model.audio import AudioBuffer


class _QualitySettings(NamedTuple):
    note_step: int
    probe_hold_s: float
    sustain_duration_s: float


QUALITY: dict[str, _QualitySettings] = {
    "low": _QualitySettings(note_step=12, probe_hold_s=3.0, sustain_duration_s=2.0),
    "medium": _QualitySettings(note_step=3, probe_hold_s=6.0, sustain_duration_s=5.0),
    "high": _QualitySettings(note_step=1, probe_hold_s=15.0, sustain_duration_s=15.0),
}

QUALITY_CHOICES = list(QUALITY)


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
    plugin_stem: str,
    profile: str,
    note_step: int,
    raw_state: str | None = None,
) -> Path:
    safe_name = _sanitize(preset_name)

    review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""

    meta = f"confidence={result.confidence}"
    if result.loop_quality is not None:
        meta += f" loop_quality={result.loop_quality:.2f}"
    meta += f" sustains={'yes' if result.sustains else 'no'}"
    meta += f" release={result.release_tail_s:.1f}s"

    note_step_line = "" if profile == "drums" else f"  note_step: {note_step}\n"
    raw_state_line = f'  raw_state: "{raw_state}"\n' if raw_state else ""
    content = (
        f"{review_line}"
        f"# {meta}\n"
        f"source:\n"
        f"  type: vst\n"
        f"  plugin: {plugin_path}\n"
        f'  preset: "{preset_name}"\n'
        f"{raw_state_line}"
        f"\n"
        f"profile: {profile}\n"
        f"\n"
        f"capture:\n"
        f"{note_step_line}"
        f"  duration_s: {result.duration_s}\n"
        f"  release_tail_s: {result.release_tail_s}\n"
        f"\n"
        f"analysis:\n"
        f"  loop: {'true' if result.loop else 'false'}\n"
        f"\n"
        f"output:\n"
        f"  name: {plugin_stem}_{safe_name}\n"
    )

    out = config_dir / f"{safe_name}.yaml"
    out.write_text(content)
    return out


def _parse_probe_yaml(yaml_path: Path) -> tuple[str, VSTSourceConfig]:
    data = yaml.safe_load(yaml_path.read_text())
    src = data["source"]
    cfg = VSTSourceConfig(
        plugin=Path(src["plugin"]),
        preset=src.get("preset"),
        raw_state=src.get("raw_state"),
        parameter_state=src.get("parameter_state") or {},
        preset_source=src.get("preset_source"),
    )
    return src["preset"], cfg


def scan_from_probe(
    probe_dir: Path,
    config_dir: Path,
    profile: str = "synth",
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_release_s: float = 4.0,
    quality: str = "medium",
    debug: bool = False,
) -> ScanSummary:
    q = QUALITY[quality]
    loop_capture_s = 2.0
    config_dir.mkdir(parents=True, exist_ok=True)

    state_map: dict[str, VSTSourceConfig] = {}
    plugin_path: Path | None = None

    for yaml_file in sorted(probe_dir.glob("*.yaml")):
        preset_name, cfg = _parse_probe_yaml(yaml_file)
        state_map[preset_name] = cfg
        if plugin_path is None:
            plugin_path = cfg.plugin

    if not state_map:
        raise RuntimeError(f"No preset YAMLs found in {probe_dir}")

    log.info("Loading plugin: %s", plugin_path)
    adapter = VSTAdapter(VSTSourceConfig(plugin=plugin_path), state_map=state_map)
    presets = list(state_map.keys())
    if debug:
        presets = presets[:5]

    plugin_stem = plugin_path.stem
    summary = ScanSummary(total=len(presets))

    _switching_verified = len(presets) < 2
    _prev_audio: AudioBuffer | None = None

    for i, preset_name in enumerate(presets, 1):
        log.info("  [%d/%d] %s", i, summary.total, preset_name)
        audio = adapter.probe_preset(
            preset_name,
            probe_note,
            probe_velocity,
            q.probe_hold_s,
            probe_release_s,
        )
        if not _switching_verified:
            if _prev_audio is None:
                _prev_audio = audio
            else:
                import numpy as np

                if np.allclose(_prev_audio.data, audio.data, atol=1e-6):
                    log.warning(
                        "WARNING: first two presets produced identical audio — "
                        "raw_state restore may not be working for this plugin."
                    )
                _switching_verified = True

        result = probe(audio, q.probe_hold_s)
        if result.sustains:
            if result.loop:
                result = replace(result, duration_s=loop_capture_s)
            else:
                result = replace(result, duration_s=q.sustain_duration_s)

        path = _write_config(
            preset_name,
            plugin_path,
            result,
            config_dir,
            plugin_stem,
            profile,
            q.note_step,
            raw_state=state_map[preset_name].raw_state,
        )
        summary.written.append(path)
        if result.confidence != "high" or result.flags:
            summary.reviews.append((preset_name, result))

    return summary


def _find_closest_wavs(wavs: list[Path], targets: list[int]) -> list[Path]:
    """Return WAVs closest to each target MIDI note; falls back to first N files."""
    note_map: dict[int, Path] = {}
    for wav in wavs:
        parsed = _parse_note_rr(wav.stem)
        if parsed:
            note, _ = parsed
            if note not in note_map:
                note_map[note] = wav

    if not note_map:
        return wavs[: len(targets)]

    selected: list[Path] = []
    seen: set[int] = set()
    for target in targets:
        closest = min(note_map.keys(), key=lambda n: abs(n - target))
        if closest not in seen:
            selected.append(note_map[closest])
            seen.add(closest)
    return selected


def _detect_loop_for_folder(subfolder: Path, library_type: str) -> bool:
    if library_type == "kit":
        return False

    wavs = sorted(subfolder.glob("*.wav"))
    if not wavs:
        return False

    candidates = _find_closest_wavs(wavs, [48, 60, 72]) or wavs[:3]
    loop_votes = 0
    for wav in candidates:
        audio = AudioBuffer.from_file(wav)
        result = probe(audio, audio.duration_s)
        if result.loop:
            loop_votes += 1

    return loop_votes > len(candidates) / 2


def scan_library(
    library_path: Path,
    config_dir: Path,
    library_type: str,
    profile: str = "synth",
    quality: str = "medium",
    debug: bool = False,
) -> ScanSummary:
    q = QUALITY[quality]
    config_dir.mkdir(parents=True, exist_ok=True)
    library_stem = _sanitize(library_path.name)

    subfolders = sorted(
        p for p in library_path.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if debug:
        subfolders = subfolders[:5]
    summary = ScanSummary(total=len(subfolders))

    for i, subfolder in enumerate(subfolders, 1):
        loop = _detect_loop_for_folder(subfolder, library_type)
        label = "loop" if loop else "one-shot"
        log.info("  [%d/%d] %s → %s", i, summary.total, subfolder.name, label)
        safe_name = _sanitize(subfolder.name)

        capture_block = (
            "" if profile == "drums" else f"\ncapture:\n  note_step: {q.note_step}\n"
        )
        content = (
            f"source:\n"
            f"  type: library\n"
            f"  path: {subfolder}\n"
            f"\n"
            f"profile: {profile}\n"
            f"{capture_block}\n"
            f"analysis:\n"
            f"  loop: {'true' if loop else 'false'}\n"
            f"\n"
            f"output:\n"
            f"  name: {library_stem}_{safe_name}\n"
        )

        out = config_dir / f"{safe_name}.yaml"
        out.write_text(content)
        summary.written.append(out)

    return summary

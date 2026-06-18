from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import NamedTuple

import yaml

from ..progress import ProgressBar as tqdm

log = logging.getLogger(__name__)

from ..analysis.pipeline import classify_sampleset
from ..analysis.probe import (
    LONG_HOLD_S,
    SHORT_HOLD_S,
    ProbeResult,
    classify_sustain_type,
    probe,
)
from ..config.schema import CLAPSourceConfig, VSTSourceConfig
from ..io.adapters.clap import CLAPAdapter
from ..io.adapters.library import _parse_note_rr
from ..io.adapters.vst import VSTAdapter
from ..model.audio import AudioBuffer
from ..model.sample import Category, Sample, SampleSet


class _QualitySettings(NamedTuple):
    note_step: int
    sustain_duration_s: float
    min_release_s: float


QUALITY: dict[str, _QualitySettings] = {
    "low": _QualitySettings(note_step=12, sustain_duration_s=5.0, min_release_s=2.0),
    "medium": _QualitySettings(note_step=3, sustain_duration_s=15.0, min_release_s=4.0),
    "high": _QualitySettings(note_step=1, sustain_duration_s=25.0, min_release_s=6.0),
}

QUALITY_CHOICES = list(QUALITY)


@dataclass
class ScanSummary:
    total: int
    written: list[Path] = field(default_factory=list)
    reviews: list[tuple[str, ProbeResult]] = field(default_factory=list)


_CLASSIFY_NOTES = [36, 48, 60, 72, 84]


def _sound_type_to_profile(sound_type: str) -> str:
    if sound_type == "Pluck":
        return "pluck"
    if sound_type.startswith("Sustained + rhythm"):
        return "pad"
    return "synth"


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
    sample_rate: int = 48000,
) -> Path:
    safe_name = _sanitize(preset_name)

    review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""

    meta = f"confidence={result.confidence}"
    meta += f" sustains={'yes' if result.sustains else 'no'}"
    meta += f" release={result.release_tail_s:.1f}s"

    note_step_line = "" if profile == "drums" else f"  note_step: {note_step}\n"
    sample_rate_line = f"  sample_rate: {sample_rate}\n" if sample_rate != 48000 else ""
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
        f"{sample_rate_line}"
        f"{note_step_line}"
        f"  duration_s: {result.duration_s}\n"
        f"  release_tail_s: {result.release_tail_s}\n"
        f"\n"
        f"output:\n"
        f'  name: "{preset_name}"\n'
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
    )
    return src["preset"], cfg


def scan_from_probe(
    probe_dir: Path,
    config_dir: Path,
    profile: str | None = None,
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_release_s: float = 4.0,
    quality: str = "medium",
    debug: bool = False,
    sample_rate: int = 48000,
    tempo_bpm: float = 120.0,
) -> ScanSummary:
    q = QUALITY[quality]
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

    tqdm.write(f"Loading plugin: {plugin_path}")
    adapter = VSTAdapter(VSTSourceConfig(plugin=plugin_path), state_map=state_map)
    presets = list(state_map.keys())
    if debug:
        presets = presets[:30]

    plugin_stem = plugin_path.stem
    summary = ScanSummary(total=len(presets))

    _switching_verified = len(presets) < 2
    _prev_audio: AudioBuffer | None = None

    # Probe over the full capture duration, not a fixed 10 s. A slow-decaying pluck
    # (e.g. Dexed "-ANALOG 1-": silent by ~15 s but held for 25 s) still has energy at
    # 10 s, so a 10 s hold can't see it decay to silence and misreads it as sustained.
    # Classifying over the same hold we will actually capture keeps scan and batch consistent.
    long_hold_s = q.sustain_duration_s
    _total_s = long_hold_s + probe_release_s

    for preset_name in tqdm(presets, desc=f"Scanning {plugin_stem}", unit="preset"):
        adapter._apply_preset(preset_name)
        short_audio = adapter.render_note(probe_note, probe_velocity, SHORT_HOLD_S, _total_s, sample_rate=sample_rate)
        long_audio = adapter.render_note(probe_note, probe_velocity, long_hold_s, _total_s, sample_rate=sample_rate)

        if not _switching_verified:
            if _prev_audio is None:
                _prev_audio = long_audio
            else:
                import numpy as np

                if np.allclose(_prev_audio.data, long_audio.data, atol=1e-6):
                    tqdm.write(
                        "WARNING: first two presets produced identical audio — "
                        "raw_state restore may not be working for this plugin."
                    )
                _switching_verified = True

        sustains = classify_sustain_type(short_audio, long_audio, note_off_s=long_hold_s)
        result = probe(long_audio, long_hold_s, sustains_hint=sustains)
        if result.sustains:
            result = replace(
                result,
                duration_s=q.sustain_duration_s,
                release_tail_s=max(result.release_tail_s, q.min_release_s),
            )

        if profile is not None:
            preset_profile = profile
        elif not sustains:
            # Sound decays while held → pluck; no need to run classify_sampleset
            tqdm.write(f"  {preset_name}: Pluck (decay-while-held)")
            preset_profile = "pluck"
        else:
            classify_samples = [
                Sample(
                    note=n,
                    velocity=probe_velocity,
                    round_robin=1,
                    audio=adapter.render_note(n, probe_velocity, long_hold_s, _total_s, sample_rate=sample_rate),
                    note_off=int(long_hold_s * sample_rate),
                )
                for n in _CLASSIFY_NOTES
            ]
            classify_sset = SampleSet(name=preset_name, category=Category.SYNTH, samples=classify_samples)
            sound_type = classify_sampleset(classify_sset, tempo_bpm=tempo_bpm, workers=1)
            tqdm.write(f"  {preset_name}: {sound_type}")
            preset_profile = _sound_type_to_profile(sound_type)
        path = _write_config(
            preset_name,
            plugin_path,
            result,
            config_dir,
            plugin_stem,
            preset_profile,
            q.note_step,
            raw_state=state_map[preset_name].raw_state,
            sample_rate=sample_rate,
        )
        summary.written.append(path)
        if result.confidence != "high" or result.flags:
            summary.reviews.append((preset_name, result))

    return summary


def _write_clap_config(
    preset_name: str,
    plugin_path: Path,
    plugin_id: str,
    preset_path: Path,
    result: ProbeResult,
    config_dir: Path,
    profile: str,
    note_step: int,
    sample_rate: int = 48000,
) -> Path:
    safe_name = _sanitize(preset_name)
    review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""
    meta = f"confidence={result.confidence}"
    meta += f" sustains={'yes' if result.sustains else 'no'}"
    meta += f" release={result.release_tail_s:.1f}s"
    note_step_line = "" if profile == "drums" else f"  note_step: {note_step}\n"
    sample_rate_line = f"  sample_rate: {sample_rate}\n" if sample_rate != 48000 else ""
    content = (
        f"{review_line}"
        f"# {meta}\n"
        f"source:\n"
        f"  type: clap\n"
        f"  plugin: {plugin_path}\n"
        f"  plugin_id: {plugin_id}\n"
        f'  preset: "{preset_name}"\n'
        f"  preset_path: {preset_path}\n"
        f"\n"
        f"profile: {profile}\n"
        f"\n"
        f"capture:\n"
        f"{sample_rate_line}"
        f"{note_step_line}"
        f"  duration_s: {result.duration_s}\n"
        f"  release_tail_s: {result.release_tail_s}\n"
        f"\n"
        f"output:\n"
        f'  name: "{preset_name}"\n'
    )
    out = config_dir / f"{safe_name}.yaml"
    out.write_text(content)
    return out


def scan_clap(
    plugin_path: Path,
    preset_dir: Path,
    config_dir: Path,
    profile: str | None = None,
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_release_s: float = 4.0,
    quality: str = "medium",
    debug: bool = False,
    sample_rate: int = 48000,
    tempo_bpm: float = 120.0,
) -> ScanSummary:
    """Scan CLAP presets from a directory of .clap-preset files.

    Discovers the plugin_id automatically via patch_render.list_clap_plugins(),
    then probes each .clap-preset file to generate config YAMLs.
    """
    try:
        import patch_render as _pr
    except ImportError:
        raise RuntimeError("patch_render extension required for CLAP scanning")

    q = QUALITY[quality]
    config_dir.mkdir(parents=True, exist_ok=True)

    # Discover plugin ID from the .clap file
    plugins = _pr.list_clap_plugins(str(plugin_path))
    if not plugins:
        raise RuntimeError(f"No plugins found in: {plugin_path}")
    if len(plugins) > 1:
        ids = ", ".join(p["id"] for p in plugins)
        tqdm.write(f"Multiple plugins in {plugin_path.name}: {ids}")
        tqdm.write(f"  Using first: {plugins[0]['id']}")
    plugin_id = plugins[0]["id"]
    tqdm.write(f"Plugin: {plugins[0]['name']} ({plugin_id})")

    # Collect preset files (.clap-preset preferred, fall back to .fxp)
    preset_files = sorted(preset_dir.glob("*.clap-preset"))
    if not preset_files:
        preset_files = sorted(preset_dir.glob("*.fxp"))
    if not preset_files:
        raise RuntimeError(f"No .clap-preset or .fxp files found in: {preset_dir}")

    if debug:
        import random

        rng = random.Random(42)
        preset_files = rng.sample(preset_files, min(30, len(preset_files)))

    state_map: dict[str, CLAPSourceConfig] = {}
    for pf in preset_files:
        preset_name = pf.stem
        state_map[preset_name] = CLAPSourceConfig(
            plugin=plugin_path,
            plugin_id=plugin_id,
            preset=preset_name,
            preset_path=pf,
        )

    tqdm.write(f"Found {len(state_map)} preset(s) in {preset_dir}")
    adapter = CLAPAdapter(
        CLAPSourceConfig(plugin=plugin_path, plugin_id=plugin_id, preset=next(iter(state_map))),
        state_map=state_map,
    )

    summary = ScanSummary(total=len(state_map))
    plugin_stem = plugin_path.stem
    # Probe over the full capture duration so slow-decaying plucks are seen to decay to
    # silence (see scan_from_probe for the rationale).
    long_hold_s = q.sustain_duration_s
    _total_s = long_hold_s + probe_release_s

    for preset_name in tqdm(list(state_map), desc=f"Scanning {plugin_stem}", unit="preset"):
        adapter._apply_preset(preset_name)
        short_audio = adapter.render_note(probe_note, probe_velocity, SHORT_HOLD_S, _total_s, sample_rate=sample_rate)
        long_audio = adapter.render_note(probe_note, probe_velocity, long_hold_s, _total_s, sample_rate=sample_rate)

        sustains = classify_sustain_type(short_audio, long_audio, note_off_s=long_hold_s)
        result = probe(long_audio, long_hold_s, sustains_hint=sustains)
        if result.sustains:
            result = replace(
                result,
                duration_s=q.sustain_duration_s,
                release_tail_s=max(result.release_tail_s, q.min_release_s),
            )

        if profile is not None:
            preset_profile = profile
        elif not sustains:
            tqdm.write(f"  {preset_name}: Pluck (decay-while-held)")
            preset_profile = "pluck"
        else:
            classify_samples = [
                Sample(
                    note=n,
                    velocity=probe_velocity,
                    round_robin=1,
                    audio=adapter.render_note(n, probe_velocity, long_hold_s, _total_s, sample_rate=sample_rate),
                    note_off=int(long_hold_s * sample_rate),
                )
                for n in _CLASSIFY_NOTES
            ]
            classify_sset = SampleSet(name=preset_name, category=Category.SYNTH, samples=classify_samples)
            sound_type = classify_sampleset(classify_sset, tempo_bpm=tempo_bpm, workers=1)
            tqdm.write(f"  {preset_name}: {sound_type}")
            preset_profile = _sound_type_to_profile(sound_type)

        path = _write_clap_config(
            preset_name,
            plugin_path,
            plugin_id,
            state_map[preset_name].preset_path,
            result,
            config_dir,
            preset_profile,
            q.note_step,
            sample_rate=sample_rate,
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


def _detect_folder_profile(subfolder: Path, library_type: str) -> str:
    """Return profile for the folder using classify on candidate WAVs."""
    if library_type == "kit":
        return "drums"

    wavs = sorted(subfolder.glob("*.wav"))
    if not wavs:
        return "synth"

    candidates = _find_closest_wavs(wavs, [48, 60, 72]) or wavs[:3]
    samples = []
    for wav in candidates:
        audio = AudioBuffer.from_file(wav)
        note_info = _parse_note_rr(wav.stem)
        note = note_info[0] if note_info else 60
        samples.append(Sample(note=note, velocity=100, round_robin=1, audio=audio))

    sset = SampleSet(name=subfolder.name, category=Category.SYNTH, samples=samples)
    sound_type = classify_sampleset(sset, workers=1)
    return _sound_type_to_profile(sound_type)


def scan_library(
    library_path: Path,
    config_dir: Path,
    library_type: str,
    profile: str | None = None,
    quality: str = "medium",
    debug: bool = False,
) -> ScanSummary:
    q = QUALITY[quality]
    config_dir.mkdir(parents=True, exist_ok=True)
    library_stem = _sanitize(library_path.name)

    subfolders = sorted(p for p in library_path.iterdir() if p.is_dir() and not p.name.startswith("."))
    if debug:
        subfolders = subfolders[:30]
    summary = ScanSummary(total=len(subfolders))

    for subfolder in tqdm(subfolders, desc=f"Scanning {library_stem}", unit="folder"):
        detected_profile = _detect_folder_profile(subfolder, library_type)
        folder_profile = profile if profile is not None else detected_profile
        tqdm.write(f"  {subfolder.name} → {folder_profile}")
        safe_name = _sanitize(subfolder.name)

        capture_block = "" if folder_profile == "drums" else f"\ncapture:\n  note_step: {q.note_step}\n"
        content = (
            f"source:\n"
            f"  type: library\n"
            f"  path: {subfolder}\n"
            f"\n"
            f"profile: {folder_profile}\n"
            f"{capture_block}\n"
            f"output:\n"
            f'  name: "{subfolder.name}"\n'
        )

        out = config_dir / f"{safe_name}.yaml"
        out.write_text(content)
        summary.written.append(out)

    return summary

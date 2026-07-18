from __future__ import annotations

import logging
import re
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path

import yaml

from ..progress import ProgressBar as tqdm

log = logging.getLogger(__name__)

from ..analysis.drumkit import classify_instrument
from ..analysis.drumkit_assemble import assemble_kit, discover_flavors, walk_hit_tree
from ..analysis.pipeline import classify_sampleset
from ..analysis.pitch import NOTE_NAMES, detect_pitch, hz_to_midi
from ..analysis.probe import (
    SHORT_HOLD_S,
    ProbeResult,
    classify_sustain_type,
    probe,
)
from ..analysis.wavetable import _ARCHETYPES, classify_archetype
from ..config.schema import CLAPSourceConfig, VSTSourceConfig
from ..io.adapters.clap import CLAPAdapter
from ..io.adapters.library import _parse_note_rr
from ..io.adapters.vst import VSTAdapter
from ..io.smpl import read_loop_points
from ..model.audio import AudioBuffer
from ..model.sample import Category, Sample, SampleSet

# Defaults for the two independent capture axes, exposed as raw CLI params:
#   note_step  — semitones between sampled notes (1 = every semitone = most notes)
#   duration_s — hold length in seconds per note
# These two were formerly bundled into a single --quality knob, which wrongly assumed
# sample memory and slot count scale together; different samplers pinch different axes.
DEFAULT_NOTE_STEP = 3
DEFAULT_DURATION_S = 15.0

# Sampled note range (inclusive), as a sensible default for melodic content. C4 = MIDI 60,
# so C1 = 24, C6 = 84. Overridable per scan via --start-note / --end-note.
DEFAULT_START_NOTE = 24  # C1
DEFAULT_END_NOTE = 84  # C6

_NOTE_NAME_RE = re.compile(r"^([A-Ga-g])(#|b)?(-?\d+)$")


def note_name_to_midi(name: str) -> int:
    """Parse a note name like 'C1', 'F#3', 'Bb2' to a MIDI number (C4 = 60)."""
    m = _NOTE_NAME_RE.match(name.strip())
    if not m:
        raise ValueError(f"invalid note name {name!r} (expected e.g. C1, F#3, A0)")
    letter, accidental, octave = m.group(1).upper(), m.group(2), int(m.group(3))
    semi = NOTE_NAMES.index(letter)
    if accidental == "#":
        semi += 1
    elif accidental == "b":
        semi -= 1
    midi = semi + (octave + 1) * 12
    if not 0 <= midi <= 127:
        raise ValueError(f"note {name!r} is out of MIDI range 0..127")
    return midi


def _min_release_s(duration_s: float) -> float:
    """Floor on the release tail for a sustaining preset, derived from the hold length."""
    return max(2.0, duration_s * 0.25)


def _validate_capture_params(note_step: int, duration_s: float | None, note_lo: int, note_hi: int) -> None:
    """Backstop guards for capture params (the CLI validates too, but protect API callers)."""
    if note_step < 1:
        raise ValueError(f"note_step must be >= 1, got {note_step}")
    if duration_s is not None and duration_s <= 0:
        raise ValueError(f"duration must be > 0, got {duration_s}")
    if not 0 <= note_lo <= 127 or not 0 <= note_hi <= 127:
        raise ValueError(f"note range [{note_lo}, {note_hi}] outside MIDI 0..127")
    if note_lo >= note_hi:
        raise ValueError(f"start note ({note_lo}) must be below end note ({note_hi})")


@dataclass
class ScanSummary:
    total: int
    written: list[Path] = field(default_factory=list)
    reviews: list[tuple[str, ProbeResult]] = field(default_factory=list)


_CLASSIFY_NOTES = [36, 48, 60, 72, 84]

# Loopability: a sustained sound that EVOLVES continuously (a swept filter, e.g. CASCADE 21) never
# returns to an earlier timbre, so no loop wraps seamlessly — the brightness jumps once per wrap.
# The MFCC distance between the shipped loop's start window and end window measures that jump.
# Measured over the 5 classify notes (median), it separates repeatable sustains from evolving ones.
# IMPORTANT: the seam depends on the capture duration (the loop finder may escape to a settled tail
# on a long hold), so this MUST be tuned at the production --duration, not the scan default. Tuned at
# 25 s over 3 renders each (median, worst case): loopable — ARP 2, PHAROH 4, MIRIDOR 8, ENCOUNTERS 8,
# BANKS,T. 11, ELECTRON 1 11.4 | evolving — CASCADE 21 20, TRW 22, OB GenvivY 24, RUMBLE 32,
# Thunder 3 34, SAHARA 49, SAW EM UP 35+. Clean gap 11.4→20; threshold 16 sits mid-gap (~4.5 each
# side). NOTE: median is the right aggregator — max/count can't separate (good ELECTRON 1 has 2 notes
# at 34/48, same as CASCADE; only its other 3 notes being <12 vs CASCADE's 10/15/20 distinguishes
# them). Decision is also written as a # REVIEW flag the user can flip.
_LOOP_SEAM_WIN_S = 0.3
_LOOP_TIMBRE_SEAM_MAX = 16.0  # median MFCC seam (over classify notes) above which we disable looping


def _sound_type_to_profile(sound_type: str) -> str:
    if sound_type == "Pluck":
        return "pluck"
    if sound_type.startswith("Sustained + rhythm"):
        return "pad"
    return "synth"


def _loop_timbre_seam(analyzed_sample, tempo_bpm: float | None) -> float | None:
    """MFCC distance at the shipped loop's wrap for one analysed note, or None if it has no loop.

    `analyzed_sample` is the output of the real analysis (`_analyze_one`): its `audio` is trimmed
    and `loop_points` are the loop the pipeline would actually export (real candidate or fallback).
    Compare the timbre (mean MFCC over a short window) just after loop_start with that just before
    loop_end — the two spans that abut at the wrap. Low = the timbre repeats (seamless); high = it
    jumped (an evolving, non-repeatable sound).
    """
    import librosa
    import numpy as np

    if analyzed_sample.loop_points is None:
        return None
    ls, le = analyzed_sample.loop_points
    audio = analyzed_sample.audio
    sr = audio.sample_rate
    w = int(_LOOP_SEAM_WIN_S * sr)
    mono = audio.to_mono()
    if ls < 0 or le > len(mono) or le - w < ls + w:
        return None
    a = librosa.feature.mfcc(y=mono[ls : ls + w], sr=sr, n_mfcc=13).mean(axis=1)
    b = librosa.feature.mfcc(y=mono[le - w : le], sr=sr, n_mfcc=13).mean(axis=1)
    return float(np.linalg.norm(a - b))


def _is_non_loopable(samples, tempo_bpm: float | None) -> tuple[bool, float | None]:
    """Vote across the classify notes: True if the median loop timbre-seam is too high to loop.

    Runs the real per-sample analysis (trim → envelope → loop find → fallback) so the seam is
    measured on the loop the pipeline would actually ship, then takes the median over the notes.
    Per-note seams are noisy (a 5-note sample), so require at least 3 looped notes before making the
    call — otherwise leave looping enabled (the safe default).
    """
    import numpy as np

    from ..analysis.pipeline import _analyze_one
    from ..config.schema import AnalysisConfig

    cfg = AnalysisConfig(loop=True, pitch_verify=False, normalize="none", tempo_bpm=tempo_bpm)
    seams = []
    for s in samples:
        analyzed = _analyze_one(s, cfg)
        seam = _loop_timbre_seam(analyzed, tempo_bpm)
        if seam is not None:
            seams.append(seam)
    if len(seams) < 3:
        return False, None
    median = float(np.median(seams))
    return median > _LOOP_TIMBRE_SEAM_MAX, median


def _sanitize(name: str) -> str:
    s = re.sub(r"[^\w]", "_", name)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


def _write_config_yaml(out: Path, content: str, used_stems: set[str]) -> Path:
    """Write `content` to `out`, disambiguating on stem collision.

    _sanitize collapses commas/dots/spaces/hyphens all to `_`, so preset names like
    `BANKS,T.` and `BANKS T` share a sanitised stem and would clobber each other.
    We track per-run stems and suffix `_2`, `_3`, ... instead of silently overwriting.
    Also refuses to clobber a pre-existing file on disk from an earlier scan run.
    """
    stem = out.stem
    ext = out.suffix
    parent = out.parent
    if stem not in used_stems and not out.exists():
        used_stems.add(stem)
        out.write_text(content)
        return out
    i = 2
    while True:
        alt_stem = f"{stem}_{i}"
        alt = parent / f"{alt_stem}{ext}"
        if alt_stem not in used_stems and not alt.exists():
            used_stems.add(alt_stem)
            alt.write_text(content)
            log.warning("%s collided with existing %s; wrote %s instead", out.name, stem, alt.name)
            return alt
        i += 1


def _config_yaml(
    preset_name: str,
    source_lines: str,
    result: ProbeResult,
    profile: str,
    note_step: int,
    note_lo: int,
    note_hi: int,
    sample_rate: int,
    subfolder: str = "",
) -> str:
    """Render the shared body of a scan-generated config YAML around a source-specific block."""
    review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""
    subfolder_line = f'  subfolder: "{subfolder}"\n' if subfolder else ""

    meta = f"confidence={result.confidence}"
    meta += f" sustains={'yes' if result.sustains else 'no'}"
    meta += f" release={result.release_tail_s:.1f}s"

    # Drums keep the profile's full-kit range; start/end-note is a melodic concept.
    note_range_line = "" if profile == "drums" else f"  note_range: [{note_lo}, {note_hi}]\n"
    note_step_line = "" if profile == "drums" else f"  note_step: {note_step}\n"
    sample_rate_line = f"  sample_rate: {sample_rate}\n" if sample_rate != 48000 else ""
    return (
        f"{review_line}"
        f"# {meta}\n"
        f"source:\n"
        f"{source_lines}"
        f"\n"
        f"profile: {profile}\n"
        f"\n"
        f"capture:\n"
        f"{sample_rate_line}"
        f"{note_range_line}"
        f"{note_step_line}"
        f"  duration_s: {result.duration_s}\n"
        f"  release_tail_s: {result.release_tail_s}\n"
        f"\n"
        f"output:\n"
        f'  name: "{preset_name}"\n'
        f"{subfolder_line}"
    )


def _write_config(
    preset_name: str,
    plugin_path: Path,
    result: ProbeResult,
    config_dir: Path,
    plugin_stem: str,
    profile: str,
    note_step: int,
    note_lo: int,
    note_hi: int,
    used_stems: set[str],
    raw_state: str | None = None,
    sample_rate: int = 48000,
) -> Path:
    raw_state_line = f'  raw_state: "{raw_state}"\n' if raw_state else ""
    source_lines = f'  type: vst\n  plugin: {plugin_path}\n  preset: "{preset_name}"\n{raw_state_line}'
    content = _config_yaml(preset_name, source_lines, result, profile, note_step, note_lo, note_hi, sample_rate)
    out = config_dir / f"{_sanitize(preset_name)}.yaml"
    return _write_config_yaml(out, content, used_stems)


def _parse_probe_yaml(yaml_path: Path) -> tuple[str, VSTSourceConfig]:
    data = yaml.safe_load(yaml_path.read_text())
    src = data["source"]
    cfg = VSTSourceConfig(
        plugin=Path(src["plugin"]),
        preset=src.get("preset"),
        raw_state=src.get("raw_state"),
    )
    return src["preset"], cfg


def _probe_and_classify_preset(
    adapter,
    preset_name: str,
    probe_note: int,
    probe_velocity: int,
    long_hold_s: float,
    total_s: float,
    sample_rate: int,
    sustain_duration_s: float,
    profile: str | None,
    tempo_bpm: float,
) -> tuple[ProbeResult, str, AudioBuffer]:
    """Render, probe and pick a profile for one preset. Returns (result, preset_profile, long_audio).

    long_audio is returned for callers (scan_from_probe) that need it for a raw_state
    switching sanity check; scan_clap ignores it.
    """
    adapter._apply_preset(preset_name)
    short_audio = adapter.render_note(probe_note, probe_velocity, SHORT_HOLD_S, total_s, sample_rate=sample_rate)
    long_audio = adapter.render_note(probe_note, probe_velocity, long_hold_s, total_s, sample_rate=sample_rate)

    sustains = classify_sustain_type(short_audio, long_audio, note_off_s=long_hold_s)
    result = probe(long_audio, long_hold_s, sustains_hint=sustains)
    if result.sustains:
        result = replace(
            result,
            duration_s=sustain_duration_s,
            release_tail_s=max(result.release_tail_s, _min_release_s(sustain_duration_s)),
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
                audio=adapter.render_note(n, probe_velocity, long_hold_s, total_s, sample_rate=sample_rate),
                note_off=int(long_hold_s * sample_rate),
            )
            for n in _CLASSIFY_NOTES
        ]
        classify_sset = SampleSet(name=preset_name, category=Category.SYNTH, samples=classify_samples)
        sound_type = classify_sampleset(classify_sset, tempo_bpm=tempo_bpm, workers=1)
        tqdm.write(f"  {preset_name}: {sound_type}")
        preset_profile = _sound_type_to_profile(sound_type)
        # A sustained but continuously-evolving timbre (swept filter) has no seamless loop —
        # disable looping (captured one-shot at its full sustain length) and flag for review.
        if preset_profile != "pluck":
            non_loopable, seam = _is_non_loopable(classify_samples, tempo_bpm)
            if non_loopable:
                preset_profile = "pluck"
                result.flags.append(f"evolving timbre (loop seam {seam:.0f}) — looping disabled")
                tqdm.write(f"  {preset_name}: non-repeatable timbre (seam {seam:.0f}) → no loop")

    return result, preset_profile, long_audio


def scan_from_probe(
    probe_dir: Path,
    config_dir: Path,
    profile: str | None = None,
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_release_s: float = 4.0,
    note_step: int = DEFAULT_NOTE_STEP,
    sustain_duration_s: float = DEFAULT_DURATION_S,
    note_lo: int = DEFAULT_START_NOTE,
    note_hi: int = DEFAULT_END_NOTE,
    debug: bool = False,
    sample_rate: int = 48000,
    tempo_bpm: float = 120.0,
) -> ScanSummary:
    _validate_capture_params(note_step, sustain_duration_s, note_lo, note_hi)
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
        presets = presets[:5]

    plugin_stem = plugin_path.stem
    summary = ScanSummary(total=len(presets))
    used_stems: set[str] = set()

    _switching_verified = len(presets) < 2
    _prev_audio: AudioBuffer | None = None

    # Probe over the full capture duration, not a fixed 10 s. A slow-decaying pluck
    # (e.g. Dexed "-ANALOG 1-": silent by ~15 s but held for 25 s) still has energy at
    # 10 s, so a 10 s hold can't see it decay to silence and misreads it as sustained.
    # Classifying over the same hold we will actually capture keeps scan and batch consistent.
    long_hold_s = sustain_duration_s
    _total_s = long_hold_s + probe_release_s

    for preset_name in tqdm(presets, desc=f"Scanning {plugin_stem}", unit="preset"):
        result, preset_profile, long_audio = _probe_and_classify_preset(
            adapter,
            preset_name,
            probe_note,
            probe_velocity,
            long_hold_s,
            _total_s,
            sample_rate,
            sustain_duration_s,
            profile,
            tempo_bpm,
        )

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

        path = _write_config(
            preset_name,
            plugin_path,
            result,
            config_dir,
            plugin_stem,
            preset_profile,
            note_step,
            note_lo,
            note_hi,
            used_stems,
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
    note_lo: int,
    note_hi: int,
    used_stems: set[str],
    sample_rate: int = 48000,
    subfolder: str = "",
) -> Path:
    source_lines = (
        f"  type: clap\n"
        f"  plugin: {plugin_path}\n"
        f"  plugin_id: {plugin_id}\n"
        f'  preset: "{preset_name}"\n'
        f"  preset_path: {preset_path}\n"
    )
    content = _config_yaml(
        preset_name, source_lines, result, profile, note_step, note_lo, note_hi, sample_rate, subfolder=subfolder
    )
    # Mirror the preset's source subfolder in the config tree too, so configs are
    # organised the same way as the presets (and the exported XMLs).
    target_dir = config_dir
    for comp in subfolder.split("/"):
        if comp:
            target_dir = target_dir / comp
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"{_sanitize(preset_name)}.yaml"
    return _write_config_yaml(out, content, used_stems)


def scan_clap(
    plugin_path: Path,
    preset_dir: Path,
    config_dir: Path,
    profile: str | None = None,
    probe_note: int = 60,
    probe_velocity: int = 100,
    probe_release_s: float = 4.0,
    note_step: int = DEFAULT_NOTE_STEP,
    sustain_duration_s: float = DEFAULT_DURATION_S,
    note_lo: int = DEFAULT_START_NOTE,
    note_hi: int = DEFAULT_END_NOTE,
    debug: bool = False,
    sample_rate: int = 48000,
    tempo_bpm: float = 120.0,
    collection_root: Path | None = None,
) -> ScanSummary:
    """Scan CLAP presets from a directory of .clap-preset files.

    Discovers the plugin_id automatically via patch_render.list_clap_plugins(),
    then probes each .clap-preset file to generate config YAMLs.
    """
    try:
        import patch_render as _pr
    except ImportError:
        raise RuntimeError("patch_render extension required for CLAP scanning")

    _validate_capture_params(note_step, sustain_duration_s, note_lo, note_hi)
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

    # Collect preset files (.clap-preset preferred, fall back to .fxp, then u-he .h2p).
    # u-he ships presets as `.h2p` — their native format doubles as the plugin's CLAP
    # state blob, so CLAPAdapter._raw_state_for base64s the raw bytes straight into
    # state->load() (the FXP-header strip is a no-op on them). They nest into bank
    # subfolders, so search recursively.
    preset_files = sorted(preset_dir.glob("*.clap-preset"))
    if not preset_files:
        preset_files = sorted(preset_dir.glob("*.fxp"))
    if not preset_files:
        preset_files = sorted(preset_dir.rglob("*.h2p"))
    if not preset_files:
        raise RuntimeError(f"No .clap-preset, .fxp or .h2p files found in: {preset_dir}")

    if debug:
        import random

        rng = random.Random(42)
        preset_files = rng.sample(preset_files, min(5, len(preset_files)))

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
    used_stems: set[str] = set()
    # Probe over the full capture duration so slow-decaying plucks are seen to decay to
    # silence (see scan_from_probe for the rationale).
    long_hold_s = sustain_duration_s
    _total_s = long_hold_s + probe_release_s

    for preset_name in tqdm(list(state_map), desc=f"Scanning {plugin_stem}", unit="preset"):
        result, preset_profile, _ = _probe_and_classify_preset(
            adapter,
            preset_name,
            probe_note,
            probe_velocity,
            long_hold_s,
            _total_s,
            sample_rate,
            sustain_duration_s,
            profile,
            tempo_bpm,
        )

        # Mirror the preset's folder position (relative to collection_root, e.g. the
        # plugin's Presets root) into the config + exported XML so the on-card layout
        # matches how the presets are organised in the plugin.
        subfolder = ""
        preset_path = state_map[preset_name].preset_path
        if collection_root is not None and preset_path is not None:
            try:
                rel = preset_path.parent.relative_to(collection_root).as_posix()
                subfolder = "" if rel == "." else rel
            except ValueError:
                subfolder = ""

        path = _write_clap_config(
            preset_name,
            plugin_path,
            plugin_id,
            preset_path,
            result,
            config_dir,
            preset_profile,
            note_step,
            note_lo,
            note_hi,
            used_stems,
            sample_rate=sample_rate,
            subfolder=subfolder,
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


def _detect_folder_profile(subfolder: Path, library_type: str) -> tuple[str, str | None]:
    """Return (profile, review_flag) for the folder, classifying the candidate WAVs.

    Like the VST/CLAP paths, a sustained-but-continuously-evolving timbre (a swept filter that never
    repeats) has no seamless loop, so it is demoted to `pluck` (captured one-shot, no loop) and a
    review flag is returned. Unlike those paths there is no render duration to choose — the WAV is a
    fixed recording — so `_is_non_loopable` measures the real sample's real loop directly. NOTE: the
    seam threshold was tuned on 25 s VST renders; library material may want its own value (see the
    constant's comment) — the flag lets the user override.
    """
    if library_type == "kit":
        return "drums", None

    if library_type == "drumkit":
        hit_wavs = sorted(subfolder.glob("*.wav"))
        if not hit_wavs:
            return "drums", "no WAV files found directly in this folder — not a flat drumkit folder"
        categories = {classify_instrument(wav.stem) for wav in hit_wavs}
        if categories == {"other"}:
            return "drums", "no drum/percussion instrument keywords recognized in any filename — verify manually"
        if len(categories) == 1:
            only = next(iter(categories))
            return (
                "drums",
                f"all {len(hit_wavs)} files classified as '{only}' — verify this is a complete kit, not a single-instrument multisample",
            )
        return "drums", None

    wavs = sorted(subfolder.glob("*.wav"))
    if not wavs:
        return "synth", None

    candidates = _find_closest_wavs(wavs, [48, 60, 72]) or wavs[:3]
    samples = []
    for wav in candidates:
        audio = AudioBuffer.from_file(wav)
        note_info = _parse_note_rr(wav.stem)
        note = note_info[0] if note_info else 60
        samples.append(Sample(note=note, velocity=100, round_robin=1, audio=audio))

    sset = SampleSet(name=subfolder.name, category=Category.SYNTH, samples=samples)
    sound_type = classify_sampleset(sset, workers=1)
    profile = _sound_type_to_profile(sound_type)

    # Author-baked smpl loops are ground truth that the sound is meant to loop: trust them,
    # skip our evolving-timbre gate, and never demote to pluck (a classifier "pluck" on a
    # looped bass just means "short envelope" — the author still loops it → treat as synth).
    if any(read_loop_points(wav) is not None for wav in candidates):
        return ("synth" if profile == "pluck" else profile), None

    if profile != "pluck":
        non_loopable, seam = _is_non_loopable(samples, tempo_bpm=None)
        if non_loopable:
            return "pluck", f"evolving timbre (loop seam {seam:.0f}) — looping disabled"
    return profile, None


def scan_library(
    library_path: Path,
    config_dir: Path,
    library_type: str,
    profile: str | None = None,
    note_step: int = DEFAULT_NOTE_STEP,
    note_lo: int = DEFAULT_START_NOTE,
    note_hi: int = DEFAULT_END_NOTE,
    debug: bool = False,
) -> ScanSummary:
    _validate_capture_params(note_step, None, note_lo, note_hi)
    config_dir.mkdir(parents=True, exist_ok=True)
    library_stem = _sanitize(library_path.name)

    subfolders = sorted(p for p in library_path.iterdir() if p.is_dir() and not p.name.startswith("."))
    if debug:
        subfolders = subfolders[:5]
    summary = ScanSummary(total=len(subfolders))
    used_stems: set[str] = set()

    for subfolder in tqdm(subfolders, desc=f"Scanning {library_stem}", unit="folder"):
        if profile is not None:
            folder_profile, review_flag = profile, None
        else:
            folder_profile, review_flag = _detect_folder_profile(subfolder, library_type)
        if review_flag:
            tqdm.write(f"  {subfolder.name} → {folder_profile} ({review_flag})")
        else:
            tqdm.write(f"  {subfolder.name} → {folder_profile}")
        safe_name = _sanitize(subfolder.name)

        review_line = f"# REVIEW: {review_flag}\n" if review_flag else ""
        capture_block = (
            "" if folder_profile == "drums" else f"\ncapture:\n  note_range: [{note_lo}, {note_hi}]\n  note_step: {note_step}\n"
        )
        drumkit_line = "  drumkit: true\n" if library_type == "drumkit" else ""
        content = (
            f"{review_line}"
            f"source:\n"
            f"  type: library\n"
            f"  path: {subfolder}\n"
            f"{drumkit_line}"
            f"\n"
            f"profile: {folder_profile}\n"
            f"{capture_block}\n"
            f"output:\n"
            f'  name: "{subfolder.name}"\n'
        )

        out = config_dir / f"{safe_name}.yaml"
        out = _write_config_yaml(out, content, used_stems)
        summary.written.append(out)
        if review_flag:
            summary.reviews.append(
                (
                    subfolder.name,
                    ProbeResult(duration_s=0.0, release_tail_s=0.0, sustains=True, confidence="high", flags=[review_flag]),
                )
            )

    return summary


_ONESHOT_NOTE_TOKEN_RE = re.compile(r"^([A-Ga-g])(#|b)?$")


def _parse_note_letter(stem: str) -> int | None:
    """Return the pitch class (0-11) from a standalone note-letter token in the filename.

    Monosounds-style names carry the pitch class but no octave, e.g.
    'Monosounds - Minimoog Bass - F - 016' → token 'F'. Splits on the same
    separators the library uses (spaces/hyphens/underscores) and only matches a
    token that is *exactly* a note letter, so words like the "Bass" in the name
    itself are never mistaken for a note.
    """
    for token in re.split(r"[\s_-]+", stem):
        m = _ONESHOT_NOTE_TOKEN_RE.match(token)
        if m:
            semi = NOTE_NAMES.index(m.group(1).upper())
            if m.group(2) == "#":
                semi += 1
            elif m.group(2) == "b":
                semi -= 1
            return semi % 12
    return None


def _detect_oneshot_note(audio: AudioBuffer, pitch_class: int | None) -> tuple[int, str | None]:
    """Detect the recorded root note of a single-file oneshot, or fall back to C4.

    Raw autocorrelation pitch detection can latch onto a harmonic instead of the
    fundamental — confirmed on real Monosounds bass oneshots (~1/4 came back ~6+
    octaves too high). When the filename gives a reliable pitch class (see
    _parse_note_letter), constrain the answer to the nearest MIDI note that actually
    matches that class instead of trusting the raw detected frequency's octave.
    """
    hz = detect_pitch(audio)
    if hz is None:
        if pitch_class is not None:
            # Nearest note of the known class to middle C, as a plausible default.
            candidates = [n for n in range(0, 128) if n % 12 == pitch_class]
            return min(candidates, key=lambda n: abs(n - 60)), "pitch detection failed — verify root note/octave manually"
        return 60, "pitch detection failed — defaulted to C4, verify root note manually"

    if pitch_class is not None:
        candidates = [n for n in range(0, 128) if n % 12 == pitch_class]
        detected_midi = hz_to_midi(hz)
        best = min(candidates, key=lambda n: abs(n - detected_midi))
    else:
        best = max(0, min(127, int(round(hz_to_midi(hz)))))

    # The pitch-class constraint only fixes wrong-letter errors; autocorrelation can still
    # latch onto a harmonic and return a frequency that's implausible for any instrument
    # register (confirmed on real Monosounds bass oneshots — detected Hz pinned near the
    # detector's 4000 Hz ceiling). Outside a generous playable range (C0..C8), don't trust
    # the octave: flag it instead of silently shipping a note ~6+ octaves off.
    if not 12 <= best <= 108:
        return best, f"detected pitch implausible ({hz:.0f} Hz) — verify root note/octave manually"
    return best, None


def _detect_oneshot_profile(audio: AudioBuffer, note: int) -> str:
    """Classify a single oneshot WAV's envelope into a profile.

    Unlike _detect_folder_profile, there is only one note here, so the evolving-timbre
    seam check (_is_non_loopable) can't run — it needs >=3 notes for a stable median.
    """
    sample = Sample(note=note, velocity=100, round_robin=1, audio=audio)
    sset = SampleSet(name="oneshot", category=Category.SYNTH, samples=[sample])
    sound_type = classify_sampleset(sset, workers=1)
    return _sound_type_to_profile(sound_type)


def scan_oneshots(
    folder: Path,
    config_dir: Path,
    profile: str | None = None,
    debug: bool = False,
) -> ScanSummary:
    """Generate one config per WAV in a folder of single-note oneshot presets.

    For libraries like Monosounds: every subfolder holds many loose WAVs, each its own
    preset at one fixed pitch — not round robins or notes of a shared multisample. The
    root note (absent from the filename's octave-less pitch class) is auto-detected per
    file via autocorrelation pitch tracking, and pinned into the generated config's
    source.note so LibraryAdapter doesn't need to re-parse it from the name.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    wavs = sorted(folder.glob("*.wav"))
    if debug:
        wavs = wavs[:5]
    summary = ScanSummary(total=len(wavs))
    used_stems: set[str] = set()

    for wav in tqdm(wavs, desc=f"Scanning {folder.name}", unit="file"):
        preset_name = wav.stem
        audio = AudioBuffer.from_file(wav)
        pitch_class = _parse_note_letter(preset_name)
        note, note_flag = _detect_oneshot_note(audio, pitch_class)
        preset_profile = profile if profile is not None else _detect_oneshot_profile(audio, note)

        flags = [note_flag] if note_flag else []
        review_line = f"# REVIEW: {', '.join(flags)}\n" if flags else ""
        content = (
            f"{review_line}"
            f"source:\n"
            f"  type: library\n"
            f"  path: {wav}\n"
            f"  note: {note}\n"
            f"\n"
            f"profile: {preset_profile}\n"
            f"\n"
            f"output:\n"
            f'  name: "{preset_name}"\n'
        )

        out = config_dir / f"{_sanitize(preset_name)}.yaml"
        out = _write_config_yaml(out, content, used_stems)
        summary.written.append(out)
        if flags:
            summary.reviews.append(
                (preset_name, ProbeResult(duration_s=0.0, release_tail_s=0.0, sustains=True, confidence="high", flags=flags))
            )

    return summary


def scan_bitwig(
    folder: Path,
    config_dir: Path,
    profile: str | None = None,
    note_step: int = DEFAULT_NOTE_STEP,
    debug: bool = False,
) -> ScanSummary:
    """Generate one config per Bitwig `.multisample` archive found under `folder`.

    Everything needed to classify the preset is authoritative metadata in the archive's
    `multisample.xml` — no rendering or audio classification. The loop flag is bimodal per
    file (Bitwig writes `mode="loop"` on every zone of a sustaining articulation and
    `mode="off"` on every zone of a staccato/one-shot one), so the profile follows it
    directly: all-loop → `synth` (loops the authored sustain), otherwise → `pluck` (ships
    the one-shot, no loop). Root notes are ground truth, so pitch verification is disabled.

    Archives are discovered recursively and their folder position under `folder` is
    mirrored into both the config tree and `output.subfolder`, so a vendor pack's own
    category folders (e.g. "Orchestral Brass/") survive onto the SD card.
    """
    from ..io.adapters.bitwig import _multisample_xml_name, parse_multisample_xml

    config_dir.mkdir(parents=True, exist_ok=True)
    archives = sorted(folder.rglob("*.multisample"))
    if debug:
        archives = archives[:5]
    summary = ScanSummary(total=len(archives))
    used_stems: set[str] = set()

    for archive in tqdm(archives, desc=f"Scanning {folder.name}", unit="file"):
        preset_name = archive.stem
        try:
            with zipfile.ZipFile(archive) as z:
                xml_name = _multisample_xml_name(z.namelist())
                if xml_name is None:
                    raise ValueError("no multisample.xml inside archive")
                zones = parse_multisample_xml(z.read(xml_name))
        except (zipfile.BadZipFile, ValueError) as exc:
            tqdm.write(f"  {archive.name}: SKIPPED ({exc})")
            continue

        if not zones:
            tqdm.write(f"  {archive.name}: SKIPPED (no mapped samples)")
            continue

        n = len(zones)
        looped = sum(1 for zn in zones if zn.loop_start is not None)
        fixed = sum(1 for zn in zones if not zn.track)
        roots = sorted({zn.root for zn in zones})

        if profile is not None:
            preset_profile = profile
        else:
            preset_profile = "synth" if looped > n / 2 else "pluck"

        flags: list[str] = []
        if fixed > n / 2:
            flags.append(
                "fixed-pitch samples (percussion?) — each key maps to its own hit; "
                "verify keyboard mapping or split into a kit"
            )
        tqdm.write(
            f"  {preset_name} → {preset_profile} "
            f"({len(roots)} notes, {looped}/{n} looped)"
            + (f" [{', '.join(flags)}]" if flags else "")
        )

        rel = archive.parent.relative_to(folder).as_posix()
        subfolder = "" if rel == "." else rel

        review_line = f"# REVIEW: {', '.join(flags)}\n" if flags else ""
        subfolder_line = f'  subfolder: "{subfolder}"\n' if subfolder else ""
        content = (
            f"{review_line}"
            f"# {n} samples, {len(roots)} notes [{roots[0]}-{roots[-1]}], {looped} looped\n"
            f"source:\n"
            f"  type: bitwig\n"
            f"  path: {archive}\n"
            f"\n"
            f"profile: {preset_profile}\n"
            f"\n"
            f"analysis:\n"
            f"  pitch_verify: false\n"
            f"\n"
            f"capture:\n"
            f"  note_step: {note_step}\n"
            f"\n"
            f"output:\n"
            f'  name: "{preset_name}"\n'
            f"{subfolder_line}"
        )

        target_dir = config_dir
        for comp in subfolder.split("/"):
            if comp:
                target_dir = target_dir / comp
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"{_sanitize(preset_name)}.yaml"
        out = _write_config_yaml(out, content, used_stems)
        summary.written.append(out)
        if flags:
            summary.reviews.append(
                (preset_name, ProbeResult(duration_s=0.0, release_tail_s=0.0, sustains=True, confidence="high", flags=flags))
            )

    return summary


def scan_wavetables(
    folder: Path,
    config_dir: Path,
    archetype: str | None = None,
    debug: bool = False,
) -> ScanSummary:
    """Generate one config per WAV in a folder of Serum-format wavetable files.

    Each file's own spectral content picks its archetype (envelope/filter shape) and
    two continuous parameters (WT scan start position, LFO2-depth) — see
    analysis/wavetable.py and docs/inputs/wavetables.md. Files are shipped to the SD
    card unmodified; nothing here mutates the audio.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    wavs = sorted(folder.glob("*.wav"))
    if debug:
        wavs = wavs[:5]
    summary = ScanSummary(total=len(wavs))
    used_stems: set[str] = set()

    for wav in tqdm(wavs, desc=f"Scanning {folder.name}", unit="file"):
        preset_name = wav.stem
        audio = AudioBuffer.from_file(wav)
        result = classify_archetype(audio)
        if archetype is not None:
            t = _ARCHETYPES[archetype]
            result.archetype = archetype
            result.attack, result.decay, result.sustain, result.release = t["attack"], t["decay"], t["sustain"], t["release"]
            result.filter_type = t["filter_type"]

        review_line = f"# REVIEW: {', '.join(result.flags)}\n" if result.flags else ""
        content = (
            f"{review_line}"
            f"source:\n"
            f"  type: wavetable\n"
            f"  path: {wav}\n"
            f"\n"
            f"wavetable:\n"
            f"  archetype: {result.archetype}\n"
            f"  wt_position: {result.wt_position:.4f}\n"
            f"  lfo2_rate: {result.lfo2_rate:.4f}\n"
            f"  lfo2_depth: {result.lfo2_depth:.4f}\n"
            f"  filter_cutoff: {result.filter_cutoff:.4f}\n"
            f"  attack: {result.attack:.4f}\n"
            f"  decay: {result.decay:.4f}\n"
            f"  sustain: {result.sustain:.4f}\n"
            f"  release: {result.release:.4f}\n"
            f"  filter_type: {result.filter_type}\n"
            f"\n"
            f"output:\n"
            f'  name: "{preset_name}"\n'
        )

        out = config_dir / f"{_sanitize(preset_name)}.yaml"
        out = _write_config_yaml(out, content, used_stems)
        summary.written.append(out)
        if result.flags:
            summary.reviews.append(
                (
                    preset_name,
                    ProbeResult(duration_s=0.0, release_tail_s=0.0, sustains=True, confidence="high", flags=result.flags),
                )
            )

    return summary


def scan_kit_assemble(
    hits_root: Path,
    config_dir: Path,
    min_categories: int = 2,
) -> ScanSummary:
    """Generate one synthetic kit config per shared "flavor" tag found across a
    bag-of-hits library tree (e.g. Samples From Mars 909_from_mars/Individual Hits).

    Unlike scan-library, there's no existing 1:1 folder-to-preset structure here —
    the folder tree is a browsing bank (one subfolder per instrument category, each
    subdivided by a descriptive taxonomy like Clean/Color/Various that repeats across
    categories). See analysis/drumkit_assemble.py for the tree walk and the
    tier1(exact flavor)/tier2(Various fallback)/tier3(any file) selection per pad —
    the resolved file list is written into `source.files` once here, not re-derived
    at sample/batch time, so the user can hand-edit any single pick afterward.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    library_stem = _sanitize(hits_root.name)

    hits = walk_hit_tree(hits_root)
    flavors = discover_flavors(hits, min_families=min_categories)
    summary = ScanSummary(total=len(flavors))
    used_stems: set[str] = set()

    for flavor in tqdm(flavors, desc=f"Assembling {hits_root.name}", unit="kit"):
        kit = assemble_kit(hits, flavor)
        flavor_title = flavor.title()
        preset_name = f"{hits_root.name} - {flavor_title} Kit"
        tqdm.write(f"  {preset_name} → {len(kit)} pads")

        fallback_cats = sorted(cat for cat, (_, tier) in kit.items() if tier > 1)
        flags = (
            [f"no exact '{flavor_title}' match for {', '.join(fallback_cats)} (used Various/any-file fallback)"]
            if fallback_cats
            else []
        )
        review_line = f"# REVIEW: {', '.join(flags)}\n" if flags else ""

        files_block = "".join(f"    - {path}\n" for path, _ in kit.values())
        content = (
            f"{review_line}"
            f"source:\n"
            f"  type: library\n"
            f"  path: {hits_root}\n"
            f"  drumkit: true\n"
            f"  files:\n"
            f"{files_block}"
            f"\n"
            f"profile: drums\n"
            f"\n"
            f"output:\n"
            f'  name: "{preset_name}"\n'
        )

        out = config_dir / f"{library_stem}_{_sanitize(flavor_title)}_Kit.yaml"
        out = _write_config_yaml(out, content, used_stems)
        summary.written.append(out)
        if flags:
            summary.reviews.append(
                (preset_name, ProbeResult(duration_s=0.0, release_tail_s=0.0, sustains=True, confidence="high", flags=flags))
            )

    return summary

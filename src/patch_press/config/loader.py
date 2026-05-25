from pathlib import Path

import yaml

from ..profiles import load_profile
from .schema import (
    AnalysisConfig,
    CaptureConfig,
    CLAPSourceConfig,
    LibrarySourceConfig,
    OutputConfig,
    RunConfig,
    VSTSourceConfig,
)


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(path: Path) -> RunConfig:
    raw = yaml.safe_load(path.read_text())

    if "profile" in raw:
        base = load_profile(raw["profile"])
        raw = _deep_merge(base, raw)

    src = raw["source"]
    if src["type"] == "vst":
        source = VSTSourceConfig(
            plugin=Path(src["plugin"]),
            preset=src.get("preset"),
            raw_state=src.get("raw_state"),
        )
    elif src["type"] == "clap":
        source = CLAPSourceConfig(
            plugin=Path(src["plugin"]),
            plugin_id=src["plugin_id"],
            preset=src.get("preset"),
            preset_path=Path(src["preset_path"]) if src.get("preset_path") else None,
            raw_state=src.get("raw_state"),
        )
    elif src["type"] == "library":
        source = LibrarySourceConfig(
            path=Path(src["path"]),
            filename_pattern=src.get("filename_pattern"),
        )
    else:
        raise ValueError(f"Unknown source type: {src['type']!r}")

    cap = raw.get("capture", {})
    capture = CaptureConfig(
        note_range=tuple(cap.get("note_range", [36, 96])),
        note_step=cap.get("note_step", 3),
        velocities=cap.get("velocities", [100]),
        round_robins=cap.get("round_robins", 1),
        duration_s=cap.get("duration_s", 4.0),
        release_tail_s=cap.get("release_tail_s", 2.0),
        tempo_bpm=cap.get("tempo_bpm", 120.0),
        sample_rate=cap.get("sample_rate", 48000),
    )

    ana = raw.get("analysis", {})
    analysis = AnalysisConfig(
        trim=ana.get("trim", True),
        pitch_verify=ana.get("pitch_verify", True),
        pitch_tolerance_cents=ana.get("pitch_tolerance_cents", 50.0),
        loop=ana.get("loop", False),
        loop_use_tempo=ana.get("loop_use_tempo", False),
        loop_quality_threshold=ana.get("loop_quality_threshold", 0.8),
        loop_crossfade_ms=ana.get("loop_crossfade_ms", 5.0),
        normalize=ana.get("normalize", "per_set"),
        classify_drums=ana.get("classify_drums", False),
    )

    out = raw["output"]
    output = OutputConfig(
        name=out["name"],
    )

    return RunConfig(
        source=source,
        capture=capture,
        analysis=analysis,
        output=output,
        name=raw.get("name", output.name),
        category=raw.get("category", "synth"),
    )

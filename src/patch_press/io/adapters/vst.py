from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

try:
    import patch_render as _patch_render

    _USE_EXTENSION = True
except ImportError:
    _USE_EXTENSION = False

from tqdm import tqdm

from ...config.schema import CaptureConfig, VSTSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_SAMPLE_RATE = 48000
# Lead-in silence before note-on so the sample starts with clean silence rather
# than a burst onset.
_INIT_LEAD_S = 5.0


class VSTAdapter:
    def __init__(
        self,
        config: VSTSourceConfig,
        state_map: dict[str, VSTSourceConfig] | None = None,
    ):
        if state_map is not None:
            self._state_map = state_map
        elif config.raw_state and config.preset:
            self._state_map = {config.preset: config}
        else:
            raise ValueError("VSTAdapter requires either state_map or a config with both raw_state and preset")
        self._config = config
        self._current_raw_state: str = ""

    def list_presets(self) -> list[str]:
        return list(self._state_map.keys())

    def _apply_preset(self, preset: str) -> None:
        self._current_raw_state = self._state_map[preset].raw_state or ""

    def _render(
        self,
        raw_state: str,
        note: int,
        velocity: int,
        hold_s: float,
        total_s: float,
        tempo_bpm: float = 120.0,
        sample_rate: int = _SAMPLE_RATE,
    ) -> AudioBuffer:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            output_path = f.name
        try:
            payload = {
                "plugin": str(self._config.plugin),
                "output": output_path,
                "duration": total_s + _INIT_LEAD_S,
                "sample_rate": float(sample_rate),
                "raw_state": raw_state or "",
                "bpm": tempo_bpm,
                "midi": [
                    {
                        "type": "note_on",
                        "pitch": note,
                        "vel": velocity,
                        "time": _INIT_LEAD_S,
                    },
                    {
                        "type": "note_off",
                        "pitch": note,
                        "vel": 0,
                        "time": _INIT_LEAD_S + hold_s,
                    },
                ],
            }
            if _USE_EXTENSION:
                _patch_render.render(payload)
            else:
                result = subprocess.run(
                    ["patch-render"],
                    input=json.dumps(payload),
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"patch-render failed (exit {result.returncode}): {result.stderr}")
            return AudioBuffer.from_file(output_path)
        finally:
            os.unlink(output_path)

    def render_note(
        self,
        note: int,
        velocity: int,
        hold_s: float,
        total_s: float,
        tempo_bpm: float = 120.0,
        sample_rate: int = _SAMPLE_RATE,
    ) -> AudioBuffer:
        return self._render(self._current_raw_state, note, velocity, hold_s, total_s, tempo_bpm, sample_rate)

    def probe_preset(
        self,
        preset: str,
        note: int = 60,
        velocity: int = 100,
        hold_s: float = 6.0,
        release_s: float = 4.0,
    ) -> AudioBuffer:
        self._apply_preset(preset)
        return self.render_note(note, velocity, hold_s, hold_s + release_s)

    def capture(self, capture: CaptureConfig, name: str | None = None) -> SampleSet:
        preset_name = self._config.preset or Path(str(self._config.plugin)).stem
        sset_name = name or preset_name

        self._apply_preset(preset_name)

        note_lo, note_hi = capture.note_range
        notes = range(note_lo, note_hi + 1, capture.note_step)
        total = len(notes) * len(capture.velocities) * capture.round_robins
        samples: list[Sample] = []

        with tqdm(total=total, desc="Capturing", unit="note", leave=False) as pbar:
            for note in notes:
                for vel in capture.velocities:
                    for rr in range(1, capture.round_robins + 1):
                        pbar.set_postfix(note=note, vel=vel, rr=rr)
                        audio = self._render(
                            self._current_raw_state,
                            note,
                            vel,
                            capture.duration_s,
                            capture.duration_s + capture.release_tail_s,
                            capture.tempo_bpm,
                            capture.sample_rate,
                        )
                        samples.append(
                            Sample(
                                note=note,
                                velocity=vel,
                                round_robin=rr,
                                audio=audio,
                            )
                        )
                        pbar.update(1)

        return SampleSet(
            name=sset_name,
            category=Category.SYNTH,
            samples=samples,
            source_metadata={
                "plugin": str(self._config.plugin),
                "preset": preset_name,
            },
            tempo_bpm=capture.tempo_bpm,
        )

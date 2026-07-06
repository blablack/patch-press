from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Generic, TypeVar

from ...progress import ProgressBar as tqdm
from ...config.schema import CaptureConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_SAMPLE_RATE = 48000
# Lead-in silence before note-on so the sample starts with clean silence rather
# than a burst onset.
_INIT_LEAD_S = 5.0

_ConfigT = TypeVar("_ConfigT")


class PluginAdapterBase(Generic[_ConfigT]):
    """Shared render/capture logic for one-preset-at-a-time VST/CLAP adapters.

    Subclasses plug in the parts that differ per plugin format: how a lone config
    becomes a one-entry state map, how a preset's raw state is obtained, what extra
    fields the render payload/source metadata need, and how the render is invoked.
    """

    def __init__(self, config: _ConfigT, state_map: dict[str, _ConfigT] | None = None):
        self._state_map = state_map if state_map is not None else self._single_preset_state_map(config)
        self._config = config
        self._current_raw_state: str = ""

    def _single_preset_state_map(self, config: _ConfigT) -> dict[str, _ConfigT]:
        raise NotImplementedError

    def _raw_state_for(self, config: _ConfigT) -> str:
        raise NotImplementedError

    def _render_payload_extra(self) -> dict:
        return {}

    def _invoke_render(self, payload: dict, output_path: str) -> None:
        raise NotImplementedError

    def _extra_source_metadata(self) -> dict:
        return {}

    def _apply_preset(self, preset: str) -> None:
        self._current_raw_state = self._raw_state_for(self._state_map[preset])

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
                **self._render_payload_extra(),
            }
            self._invoke_render(payload, output_path)
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

    def capture(self, capture: CaptureConfig, name: str | None = None, progress=None) -> SampleSet:
        preset_name = self._config.preset or Path(str(self._config.plugin)).stem
        sset_name = name or preset_name

        self._apply_preset(preset_name)

        note_lo, note_hi = capture.note_range
        notes = range(note_lo, note_hi + 1, capture.note_step)
        total = len(notes) * len(capture.velocities) * capture.round_robins
        samples: list[Sample] = []

        # When the caller supplies a bar (batch run), advance that shared per-note bar so the
        # outer progress moves note-by-note instead of once per preset. Standalone (scan,
        # single sample) keeps its own local "Capturing" bar.
        own_bar = progress is None
        pbar = tqdm(total=total, desc="Capturing", unit="note", leave=False) if own_bar else progress
        try:
            for note in notes:
                for vel in capture.velocities:
                    for rr in range(1, capture.round_robins + 1):
                        if own_bar:
                            pbar.set_postfix(note=note, vel=vel, rr=rr)
                        else:
                            pbar.set_postfix_str(f"{sset_name} n{note}")
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
                                note_off=int(round(capture.duration_s * capture.sample_rate)),
                            )
                        )
                        pbar.update(1)
        finally:
            if own_bar:
                pbar.close()

        return SampleSet(
            name=sset_name,
            category=Category.SYNTH,
            samples=samples,
            source_metadata={
                "plugin": str(self._config.plugin),
                "preset": preset_name,
                **self._extra_source_metadata(),
            },
            tempo_bpm=capture.tempo_bpm,
        )

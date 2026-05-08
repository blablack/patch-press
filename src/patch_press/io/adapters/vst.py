from __future__ import annotations

import numpy as np
from mido import Message
from pedalboard import load_plugin
from tqdm import tqdm

from ...config.schema import CaptureConfig, VSTSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_SAMPLE_RATE = 44100


class VSTAdapter:
    def __init__(
        self,
        config: VSTSourceConfig,
        state_map: dict[str, VSTSourceConfig] | None = None,
    ):
        self.plugin = load_plugin(str(config.plugin))
        if state_map is not None:
            self._state_map = state_map
        elif config.raw_state and config.preset:
            self._state_map = {config.preset: config}
        else:
            raise ValueError(
                "VSTAdapter requires either state_map or a config with both raw_state and preset"
            )
        self._config = config

    def list_presets(self) -> list[str]:
        return list(self._state_map.keys())

    def _apply_preset(self, preset: str) -> None:
        import base64

        self.plugin.raw_state = base64.b64decode(self._state_map[preset].raw_state)

    def render_note(
        self,
        note: int,
        velocity: int,
        hold_s: float,
        total_s: float,
    ) -> AudioBuffer:
        raw = self.plugin(
            [
                Message("note_on", note=note, velocity=velocity),
                Message("note_off", note=note, time=hold_s),
                Message("control_change", control=123, value=0, time=0),
            ],
            duration=total_s,
            sample_rate=_SAMPLE_RATE,
        )
        if raw.ndim == 1:
            data = np.stack([raw, raw]).astype(np.float32)
        else:
            data = raw.astype(np.float32)
        return AudioBuffer(data=data, sample_rate=_SAMPLE_RATE)

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
        preset_name = self._config.preset or self.plugin.name
        sset_name = name or preset_name
        duration = capture.duration_s + capture.release_tail_s

        note_lo, note_hi = capture.note_range
        samples: list[Sample] = []

        render_kwargs: dict = {"duration": duration, "sample_rate": _SAMPLE_RATE}

        self._apply_preset(preset_name)
        # Discard audio to let any deferred preset-init events (e.g. a C3 preview
        # that Odin2 schedules on state load) fire and decay before real captures.
        self.plugin([], **render_kwargs)
        self.plugin.reset()

        notes = range(note_lo, note_hi + 1, capture.note_step)
        total = len(notes) * len(capture.velocities) * capture.round_robins
        with tqdm(total=total, desc="Capturing", unit="note", leave=False) as pbar:
            for note in notes:
                for vel in capture.velocities:
                    for rr in range(1, capture.round_robins + 1):
                        pbar.set_postfix(note=note, vel=vel, rr=rr)
                        self.plugin.reset()
                        raw = self.plugin(
                            [
                                Message("note_on", note=note, velocity=vel),
                                Message("note_off", note=note, time=capture.duration_s),
                                # All Notes Off immediately after: clears mono/legato note
                                # stack so plugins don't retrigger a previously held note.
                                Message("control_change", control=123, value=0, time=0),
                            ],
                            **render_kwargs,
                        )
                        if raw.ndim == 1:
                            data = np.stack([raw, raw]).astype(np.float32)
                        else:
                            data = raw.astype(np.float32)
                        samples.append(
                            Sample(
                                note=note,
                                velocity=vel,
                                round_robin=rr,
                                audio=AudioBuffer(data=data, sample_rate=_SAMPLE_RATE),
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
        )

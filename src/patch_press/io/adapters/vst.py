import numpy as np
from mido import Message
from pedalboard import load_plugin

from ...config.schema import CaptureConfig, VSTSourceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet

_SAMPLE_RATE = 44100


class VSTAdapter:
    def __init__(self, config: VSTSourceConfig):
        self.plugin = load_plugin(str(config.plugin))
        if config.preset is not None:
            self.plugin.program = config.preset
        self._config = config

    def list_presets(self) -> list[str]:
        return list(self.plugin.parameters["program"].valid_values)

    def probe_preset(
        self,
        preset: str,
        note: int = 60,
        velocity: int = 100,
        hold_s: float = 6.0,
        release_s: float = 4.0,
    ) -> "AudioBuffer":
        self.plugin.program = preset
        raw = self.plugin(
            [
                Message("note_on", note=note, velocity=velocity),
                Message("note_off", note=note, time=hold_s),
            ],
            duration=hold_s + release_s,
            sample_rate=_SAMPLE_RATE,
        )
        if raw.ndim == 1:
            data = np.stack([raw, raw]).astype(np.float32)
        else:
            data = raw.astype(np.float32)
        return AudioBuffer(data=data, sample_rate=_SAMPLE_RATE)

    def capture(self, capture: CaptureConfig, name: str | None = None) -> SampleSet:
        preset_name = self._config.preset or self.plugin.name
        sset_name = name or preset_name
        duration = capture.duration_s + capture.release_tail_s

        note_lo, note_hi = capture.note_range
        samples: list[Sample] = []

        for note in range(note_lo, note_hi + 1, capture.note_step):
            for vel in capture.velocities:
                for rr in range(1, capture.round_robins + 1):
                    raw = self.plugin(
                        [
                            Message("note_on", note=note, velocity=vel),
                            Message("note_off", note=note, time=capture.duration_s),
                        ],
                        duration=duration,
                        sample_rate=_SAMPLE_RATE,
                    )
                    # pedalboard returns (channels, frames) or (frames,)
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

        return SampleSet(
            name=sset_name,
            category=Category.SYNTH,
            samples=samples,
            source_metadata={
                "plugin": str(self._config.plugin),
                "preset": preset_name,
            },
        )

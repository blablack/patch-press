import dataclasses
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
from lxml import etree

from ...analysis.drumkit import classify_instrument
from ...analysis.envelope import release_time_s
from ...config.schema import OutputConfig, PerformanceConfig
from ...model.audio import AudioBuffer
from ...model.sample import Category, Sample, SampleSet
from ._common import (
    safe_component,
    sample_wav_name,
    subfolder_parts,
    wav_frame_count,
    write_sample_wav,
    write_wavetable_wav,
)

_FIRMWARE = "4.1.3"
_MIN_FIRMWARE = "4.1.0-alpha"


def _deluge_path(path: Path) -> str:
    """Return SAMPLES-rooted relative path for the Deluge SD card."""
    parts = path.parts
    idx = next((i for i, p in enumerate(parts) if p == "SAMPLES"), None)
    return str(Path(*parts[idx:])) if idx is not None else str(path)


def _write_wavs(sset: SampleSet, samples: Iterable[Sample], output_dir: Path) -> dict[tuple[int, int, int], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[int, int, int], Path] = {}
    used_names: set[str] = set()
    for s in samples:
        p = output_dir / sample_wav_name(s, sset.tempo_bpm, used_names)
        if sset.category == Category.WAVETABLE:
            write_wavetable_wav(Path(s.metadata["source_file"]), p)
        else:
            write_sample_wav(s, p)
        paths[(s.note, s.velocity, s.round_robin)] = p
    return paths


def _velocity_pick(velocities: Iterable[int]) -> int:
    """The one velocity a melodic preset ships: closest to 100, the first on a tie.

    A <sampleRange> has no velocity zones, so this is the whole velocity story for a
    multisample — `velocities_used` asks it before capture and `_shipped` after, so the
    layer rendered and the layer exported can't drift apart.
    """
    return min(velocities, key=lambda v: abs(v - 100))


def _shipped(sset: SampleSet) -> dict[int, Sample]:
    """The one sample per note the XML will reference, and so the only WAVs written.

    Anything else in the set (other velocity layers, round robins beyond the first) is
    never loaded by the Deluge, so writing it would only cost card space and lengthen
    the folder every sample lookup scans (the firmware resolves each file path with a
    linear directory search).
    """
    if sset.category == Category.WAVETABLE:
        s = sset.samples[0]
        return {s.note: s}
    keep = sset.samples
    # A kit pad is its note's lowest round robin, whatever the velocity (kit sources
    # carry one per pad anyway); a multisample first narrows to its shipped velocity.
    if sset.category != Category.DRUM:
        velocities: dict[int, list[int]] = defaultdict(list)
        for s in sset.samples:
            velocities[s.note].append(s.velocity)
        keep = [s for s in sset.samples if s.velocity == _velocity_pick(velocities[s.note])]
    picked: dict[int, Sample] = {}
    for s in keep:
        if s.note not in picked or s.round_robin < picked[s.note].round_robin:
            picked[s.note] = s
    return picked


def _q31(frac: float) -> str:
    """Encode a 0..1 fraction as the Deluge's signed-32-bit param range.

    Cross-checked against existing template constants: lpfResonance=0x80000000 (off,
    frac=0), lpfFrequency=0x7FFFFFFF (wide open, frac=1), oscAVolume=0x19999999
    (frac~0.6) — all fit raw = round(-2**31 + frac*(2**32-1)).
    """
    frac = max(0.0, min(1.0, frac))
    raw = round(-(2**31) + frac * (2**32 - 1))
    return f"0x{raw & 0xFFFFFFFF:08X}"


def _cable(units: float) -> str:
    """Encode a patch-cable amount given in the Deluge's displayed -50..+50 units.

    Cables don't share the params' 0..1 → full-signed-range encoding (_q31): their range
    is ±2**30, centred on 0 — the default velocity→volume cable is 0x3FFFFFE8, i.e. +50.
    """
    units = max(-50.0, min(50.0, units))
    return f"0x{round(units / 50 * 2**30) & 0xFFFFFFFF:08X}"


# What the firmware gives a sound that carries no <modKnobs> (Sound::Sound(),
# processing/sound/sound.cpp), in file order: knob pair k is entries 2k, 2k+1. TAL and
# Rephazer write exactly this list; writing it out lets one pair be swapped.
_DEFAULT_MOD_KNOBS: list[tuple[str, str | None]] = [
    ("pan", None), ("volumePostFX", None),
    ("lpfResonance", None), ("lpfFrequency", None),
    ("env1Release", None), ("env1Attack", None),
    ("delayFeedback", None), ("delayRate", None),
    ("reverbAmount", None), ("volumePostReverbSend", "compressor"),
    ("pitch", "lfo1"), ("lfo1Rate", None),
    ("portamento", None), ("stutterRate", None),
    ("bitcrushAmount", None), ("sampleRateReduction", None),
]


def _mod_knobs(parent, overrides: dict[int, tuple[str, str | None]]) -> None:
    knobs = etree.SubElement(parent, "modKnobs")
    for i, default in enumerate(_DEFAULT_MOD_KNOBS):
        param, source = overrides.get(i, default)
        kw = {"controlsParam": param}
        if source:
            kw["patchAmountFromSource"] = source
        etree.SubElement(knobs, "modKnob", **kw)


# Velocity → brightness on a sample that can only ship one velocity layer: TAL's recipe,
# identical across all 49 of its velocity-filtered factory presets — a 12 dB lowpass at
# 40 with a velocity→cutoff cable at +20, so a soft hit closes the filter and a hard one
# opens it past where it sits.
_VEL_FILTER_MODE = "12dB"
_VEL_FILTER_CUTOFF = "0x4CCCCCA8"
_VEL_FILTER_CABLE = "0x19999990"
# A mod wheel the author says darkens the sound: the default Y→cutoff cable flipped. The
# capture is the wheel at rest, so the filter stays open there and the wheel closes it —
# the one filter direction a sample can follow (a wheel that *brightens* would need
# brightness the capture doesn't have). Starting value, not ear-tuned.
_Y_DARKEN = _cable(-20)
# Aftertouch vibrato: LFO1→pitch with its depth scaled by aftertouch at full. The
# firmware reads aftertouch as -1 at rest when it controls a depth (patcher.cpp,
# combineCablesLinearForRangeParam), so (1 + -1 × 1) silences the vibrato until the key
# is pressed, and full pressure doubles the base. The mod wheel can't do this — it reaches
# the `y` source unipolar, 0 at rest, so a wheel-scaled cable always plays at its base
# depth; a "MW = vibrato" preset is left to the default LFO1→pitch gold knob instead.
# Base depth is a starting value, not ear-tuned.
_VIBRATO_DEPTH = _cable(5)
_FULL_DEPTH = "0x3FFFFFE8"
_CHOKE_CATEGORIES = {"hat_closed", "hat_open"}
# Detune of the two wavetable oscillators, matching the Bento export's ±6 cents.
_WT_DETUNE_CENTS = 6


# Envelope 1 release for a sample whose release can't be measured (library sources, or a
# capture with nothing to time): knob ~25, ~2.4 s to silence.
_DEFAULT_RELEASE = "0x02000000"
# Seconds the envelope-1 release takes to fall 40 dB, per knob value 0..50. Worked out from
# the firmware rather than measured: the preset value is scaled by the param's 2**30 range
# (Patcher::combineCablesExp), looked up in releaseRateTable64 (lookupReleaseRate), scaled
# by the 140 << 9 neutral value, and the stage runs 2**23 of position along decayTableSmall8
# at 44.1 kHz — whose -40 dB point sits at 77% of the way. Knob 25 is ~1.9 s to -40 dB,
# ~2.4 s to silence.
_RELEASE_40DB_S = (
    0.004, 0.042, 0.085, 0.132, 0.179, 0.226, 0.276, 0.329, 0.383, 0.439,
    0.499, 0.562, 0.626, 0.694, 0.762, 0.836, 0.914, 1.001, 1.082, 1.177,
    1.268, 1.375, 1.486, 1.599, 1.711, 1.862, 1.988, 2.132, 2.299, 2.452,
    2.675, 2.829, 3.065, 3.344, 3.588, 3.872, 4.203, 4.458, 4.904, 5.254,
    5.885, 6.397, 7.006, 7.743, 8.654, 9.808, 11.317, 12.26, 14.712, 16.347,
    21.017,
)  # fmt: skip
# A library loop's tail counts as the author's recorded release when the file ends at least
# this far below the loop's level. Above it the tail is held sustain the recording simply
# stops on — at note-off the Deluge would play that and then cut out mid-sound.
_TAIL_RELEASE_DB = -20.0
_TAIL_END_S = 0.05


def _release_knob(seconds: float) -> int:
    """The release knob value whose fall to -40 dB is closest to `seconds` (on a log scale)."""
    seconds = max(seconds, _RELEASE_40DB_S[0])
    return min(range(len(_RELEASE_40DB_S)), key=lambda k: abs(math.log(_RELEASE_40DB_S[k] / seconds)))


def _capture_release(by_note: dict[int, Sample]) -> str:
    """Envelope 1 release matching the plugin's own, timed on the captures.

    A rendered capture knows its note-off, so its release can be measured; one knob
    covers the whole preset, so it's the median over the shipped notes. Library samples
    carry no note-off and keep the default.
    """
    times = [t for s in by_note.values() if (t := release_time_s(s.audio, s.note_off)) is not None]
    if not times:
        return _DEFAULT_RELEASE
    return _q31(_release_knob(float(np.median(times))) / 50)


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x**2))) if len(x) else 0.0


def _tail_is_release(sample: Sample) -> bool:
    """True when the audio after the loop end is a release worth playing at note-off.

    A capture's never is: it runs on from the loop end through seconds of held sustain
    before the plugin even sees its note-off. A library sample's is when the author
    recorded the release there — the file fades well below the loop's level by its end.
    """
    if sample.note_off is not None:
        return False
    start, end = sample.loop_points
    mono = sample.audio.to_mono()
    n = int(_TAIL_END_S * sample.audio.sample_rate)
    if len(mono) - end < n:
        return False
    return _rms(mono[-n:]) < _rms(mono[start:end]) * 10 ** (_TAIL_RELEASE_DB / 20)


def _loop_through(by_note: dict[int, Sample]) -> set[int] | None:
    """Notes whose range keeps looping through the release, or None to stay in cut mode.

    Loop mode is per oscillator, and in loop mode a range without loop points loops its
    whole sample — so only a preset that loops every note can switch. Within it, a range
    whose tail is a real release keeps its `endLoopPos` (left at note-off, release played);
    every other one loops up to `endSamplePos` for as long as the voice sounds.
    """
    if not by_note or any(s.loop_points is None for s in by_note.values()):
        return None
    return {n for n, s in by_note.items() if not _tail_is_release(s)}


def _cut_at_loop_end(sample: Sample) -> Sample:
    """A capture cut where its loop ends: in loop-through nothing past it is ever played."""
    end = sample.loop_points[1]
    return dataclasses.replace(sample, audio=AudioBuffer(data=sample.audio.data[:, :end], sample_rate=sample.audio.sample_rate))


def _zone_el(parent, path: Path, sample: Sample, loop_through: bool = False) -> None:
    frames = wav_frame_count(path)
    kw = {"startSamplePos": "0", "endSamplePos": str(frames)}
    if sample.loop_points:
        start, end = sample.loop_points
        kw["startLoopPos"] = str(start)
        if loop_through:
            kw["endSamplePos"] = str(min(end, frames))
        else:
            kw["endLoopPos"] = str(end)
    etree.SubElement(parent, "zone", **kw)


def _patch_cables(parent, extra: list[tuple] | None = None, y_lpf: str = "0x19999990") -> None:
    """The Deluge's three default cables plus `extra` ones.

    An extra is `(source, destination, amount)`, or `(source, destination, amount,
    (depth_source, depth_amount))` for a cable whose depth another source scales — the
    firmware's `depthControlledBy`, which multiplies the cable by (1 + source × amount).
    """
    cables = etree.SubElement(parent, "patchCables")
    etree.SubElement(cables, "patchCable", source="velocity", destination="volume", amount="0x3FFFFFE8")
    etree.SubElement(cables, "patchCable", source="aftertouch", destination="volume", amount="0x2A3D7094")
    etree.SubElement(cables, "patchCable", source="y", destination="lpfFrequency", amount=y_lpf)
    for source, destination, amount, *depth in extra or []:
        cable = etree.SubElement(cables, "patchCable", source=source, destination=destination, amount=amount)
        if depth:
            depth_source, depth_amount = depth[0]
            ctl = etree.SubElement(cable, "depthControlledBy")
            etree.SubElement(ctl, "patchCable", source=depth_source, amount=depth_amount)


def _write_xml(root, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True))


class DelugeExporter:
    @classmethod
    def expected_outputs(cls, output: OutputConfig, path: Path) -> list[Path]:
        """Paths whose existence means this preset is already exported.

        A config alone doesn't say kit-vs-synth (that's decided by the sample set's
        category at export time), so both candidate XML locations are listed —
        callers treat "any exists" as done.
        """
        safe = safe_component(output.name)
        sub = subfolder_parts(output.subfolder)
        return [
            path.parent.joinpath(folder, path.name, *sub, f"{safe}.xml")
            for folder in ("SYNTHS", "KITS")
        ]

    @classmethod
    def notes_used(cls, notes: Sequence[int]) -> list[int]:
        """Every captured note becomes its own keyzone, so the whole grid is used."""
        return list(notes)

    @classmethod
    def velocities_used(cls, velocities: Sequence[int]) -> list[int]:
        """The one velocity layer a melodic preset ships — no velocity zones to put more in.

        Asked before capture (runner/pipeline.py:velocities_to_capture), so a
        velocity-sensitive plugin preset renders one layer instead of three; that its
        timbre *does* change with velocity reaches the exporter as
        `source_metadata["velocity_sensitive"]` instead, for the velocity→filter cable.
        """
        return [_velocity_pick(velocities)]

    def export(self, sset: SampleSet, config: OutputConfig, path: Path) -> Path:
        safe_name = safe_component(config.name)
        # An optional subfolder tree (config.subfolder) is inserted after the collection
        # so presets can mirror their source's own folder organisation on the card.
        sub = subfolder_parts(config.subfolder)
        # Kits live in their own SD-card folder, browsed separately from single-sound
        # SYNTHS presets (confirmed against a real Deluge SD card backup) — a kit XML
        # written under SYNTHS/ wouldn't show up in the firmware's "load kit" browser.
        preset_folder = "KITS" if sset.category == Category.DRUM else "SYNTHS"
        wav_dir = path.parent.joinpath("SAMPLES", path.name, *sub, safe_name)
        xml_path = path.parent.joinpath(preset_folder, path.name, *sub, f"{safe_name}.xml")
        shipped = _shipped(sset)
        multisample = sset.category not in (Category.DRUM, Category.WAVETABLE)
        loop_through = release = None
        if multisample:
            # Timed before the cut below, which drops the very release being measured.
            release = _capture_release(shipped)
            loop_through = _loop_through(shipped)
            if loop_through:
                # Only captures are cut: library audio ships verbatim, its tail just goes unplayed.
                shipped = {
                    n: _cut_at_loop_end(s) if n in loop_through and s.note_off is not None else s for n, s in shipped.items()
                }
        wav_paths = _write_wavs(sset, shipped.values(), wav_dir)

        if sset.category == Category.DRUM:
            self._export_kit(sset, shipped, wav_paths, xml_path)
        elif sset.category == Category.WAVETABLE:
            self._export_wavetable(sset, wav_paths, xml_path)
        else:
            self._export_multisample(sset, shipped, wav_paths, xml_path, loop_through, release)

        return xml_path

    # ------------------------------------------------------------------
    def _export_multisample(
        self,
        sset: SampleSet,
        by_note: dict[int, Sample],
        wav_paths: dict[tuple[int, int, int], Path],
        xml_path: Path,
        loop_through: set[int] | None,
        release: str,
    ) -> None:
        sorted_notes = sorted(by_note)
        perf = sset.source_metadata.get("performance") or PerformanceConfig()
        # The scan judged this preset's timbre velocity-sensitive (capture.velocities,
        # analysis/velocity.py). The Deluge has no velocity zones to ship layers as, so
        # only one is rendered (velocities_used) and the brightness change is handed to
        # the filter instead.
        velocity_filter = bool(sset.source_metadata.get("velocity_sensitive"))
        sound = etree.Element(
            "sound",
            firmwareVersion=_FIRMWARE,
            earliestCompatibleFirmware=_MIN_FIRMWARE,
            polyphonic=perf.voice_mode,
            voicePriority="1",
            mode="subtractive",
            lpfMode=_VEL_FILTER_MODE if velocity_filter else "24dB",
            modFXType="none",
        )
        osc1 = etree.SubElement(
            sound,
            "osc1",
            type="sample",
            # Loop mode keeps a loop-through range looping while the release fades it, so
            # turning the release up on the device still works; cut mode would stop it dead
            # at endSamplePos. Rephazer's layout: 402 of its 446 sample presets.
            loopMode="0" if loop_through is None else "2",
            reversed="0",
            timeStretchEnable="0",
            timeStretchAmount="0",
        )
        sample_ranges = etree.SubElement(osc1, "sampleRanges")

        for i, note in enumerate(sorted_notes):
            s = by_note[note]
            wav = wav_paths[(s.note, s.velocity, s.round_robin)]
            top_note = sorted_notes[i + 1] - 1 if i + 1 < len(sorted_notes) else note + 6
            sr_el = etree.SubElement(
                sample_ranges,
                "sampleRange",
                rangeTopNote=str(top_note),
                fileName=_deluge_path(wav),
                transpose=str(48 - note),
            )
            _zone_el(sr_el, wav, s, loop_through=loop_through is not None and note in loop_through)

        etree.SubElement(sound, "osc2", type="square", transpose="0", cents="0", retrigPhase="-1")
        etree.SubElement(sound, "lfo1", type="triangle", syncLevel="0")
        etree.SubElement(sound, "lfo2", type="triangle")
        etree.SubElement(sound, "unison", num="1", detune="8")
        etree.SubElement(sound, "delay", pingPong="1", analog="0", syncLevel="7")
        etree.SubElement(sound, "compressor", syncLevel="6", attack="327244", release="936")

        dp = etree.SubElement(
            sound,
            "defaultParams",
            arpeggiatorGate="0x00000000",
            portamento=_q31(perf.glide) if perf.glide > 0 else "0x80000000",
            compressorShape="0xDC28F5B2",
            oscAVolume="0x19999999",
            oscAPulseWidth="0x00000000",
            oscBVolume="0x80000000",
            oscBPulseWidth="0x00000000",
            noiseVolume="0x80000000",
            volume="0x4CCCCCA8",
            pan="0x00000000",
            lpfFrequency=_VEL_FILTER_CUTOFF if velocity_filter else "0x7FFFFFFF",
            lpfResonance="0x80000000",
            hpfFrequency="0x80000000",
            hpfResonance="0x80000000",
            lfo1Rate="0x1999997E",
            lfo2Rate="0x00000000",
            modFXRate="0x00000000",
            modFXDepth="0x00000000",
            delayRate="0x00000000",
            delayFeedback="0x80000000",
            reverbAmount="0x80000000",
            arpeggiatorRate="0x00000000",
            stutterRate="0x00000000",
            sampleRateReduction="0x80000000",
            bitCrush="0x80000000",
            modFXOffset="0x00000000",
            modFXFeedback="0x00000000",
        )
        etree.SubElement(dp, "envelope1", attack="0x80000000", decay="0xE6666654", sustain="0x7FFFFFFF", release=release)
        etree.SubElement(dp, "envelope2", attack="0xE6666654", decay="0xE6666654", sustain="0xFFFFFFE9", release="0xE6666654")
        extra: list[tuple] = []
        if velocity_filter:
            extra.append(("velocity", "lpfFrequency", _VEL_FILTER_CABLE))
        if perf.aftertouch == "vibrato":
            extra.append(("lfo1", "pitch", _VIBRATO_DEPTH, ("aftertouch", _FULL_DEPTH)))
        _patch_cables(dp, extra, y_lpf=_Y_DARKEN if perf.mod_wheel == "darken" else "0x19999990")
        etree.SubElement(
            dp, "equalizer", bass="0x00000000", treble="0x00000000", bassFrequency="0x00000000", trebleFrequency="0x00000000"
        )
        etree.SubElement(sound, "arpeggiator", mode="off", numOctaves="2", syncLevel="7")

        _write_xml(sound, xml_path)

    # ------------------------------------------------------------------
    def _export_wavetable(
        self,
        sset: SampleSet,
        wav_paths: dict[tuple[int, int, int], Path],
        xml_path: Path,
    ) -> None:
        """The table on both oscillators, driven by the archetype scan_wavetables chose.

        Confirmed against the Deluge firmware source (processing/sound/sound.cpp,
        modulation/params/param.cpp): a single-file wavetable osc is a flat `type=
        "wavetable"` element (no zone/loopMode — those are sample-only), the scan
        position is the `oscA/BWavetablePosition` defaultParams attribute, and LFO2→
        position is just another patchCable to that same destination string.

        Same layout as the Bento export (io/exporters/bento.py `_build_wavetable`, what
        37 single-table Bento factory patches do): one table on two oscillators detuned
        ±6 cents, the position LFO inverted on the second so the pair sweep apart. Both
        positions sit on the last gold-knob pair, in place of bitcrush/sample-rate
        reduction — the slot every Boards of Deluge wavetable preset uses for them.
        """
        s = sset.samples[0]
        wav = wav_paths[(s.note, s.velocity, s.round_robin)]
        wt = sset.source_metadata["wavetable"]

        sound = etree.Element(
            "sound",
            firmwareVersion=_FIRMWARE,
            earliestCompatibleFirmware=_MIN_FIRMWARE,
            polyphonic="poly",
            voicePriority="1",
            mode="subtractive",
            lpfMode="24dB",
            modFXType="none",
        )
        for osc, cents in (("osc1", -_WT_DETUNE_CENTS), ("osc2", _WT_DETUNE_CENTS)):
            etree.SubElement(
                sound, osc, type="wavetable", transpose="0", cents=str(cents), retrigPhase="-1",
                fileName=_deluge_path(wav),
            )
        etree.SubElement(sound, "lfo1", type="triangle", syncLevel="0")
        etree.SubElement(sound, "lfo2", type="triangle", syncLevel="0")
        etree.SubElement(sound, "unison", num="1", detune="8")
        etree.SubElement(sound, "delay", pingPong="1", analog="0", syncLevel="7")
        etree.SubElement(sound, "compressor", syncLevel="6", attack="327244", release="936")

        dp = etree.SubElement(
            sound,
            "defaultParams",
            arpeggiatorGate="0x00000000",
            portamento="0x80000000",
            compressorShape="0xDC28F5B2",
            oscAVolume=_q31(1.0),
            oscAPulseWidth="0x00000000",
            oscAWavetablePosition=_q31(wt.wt_position),
            oscBVolume=_q31(1.0),
            oscBPulseWidth="0x00000000",
            oscBWavetablePosition=_q31(wt.wt_position),
            noiseVolume="0x80000000",
            volume="0x4CCCCCA8",
            pan="0x00000000",
            lpfFrequency=_q31(wt.filter_cutoff),
            lpfResonance="0x80000000",
            hpfFrequency="0x80000000",
            hpfResonance="0x80000000",
            lfo1Rate="0x1999997E",
            lfo2Rate=_q31(wt.lfo2_rate),
            modFXRate="0x00000000",
            modFXDepth="0x00000000",
            delayRate="0x00000000",
            delayFeedback="0x80000000",
            reverbAmount="0x80000000",
            arpeggiatorRate="0x00000000",
            stutterRate="0x00000000",
            sampleRateReduction="0x80000000",
            bitCrush="0x80000000",
            modFXOffset="0x00000000",
            modFXFeedback="0x00000000",
        )
        etree.SubElement(
            dp, "envelope1",
            attack=_q31(wt.attack), decay=_q31(wt.decay), sustain=_q31(wt.sustain), release=_q31(wt.release),
        )
        etree.SubElement(dp, "envelope2", attack="0xE6666654", decay="0xE6666654", sustain="0xFFFFFFE9", release="0xE6666654")
        # lfo2_depth is the fraction of the table to sweep, so it's a cable amount (0..+50),
        # not a param — encoding it as one (_q31) turned any depth under 0.5 into a
        # negative cable and 0.5 into none at all.
        depth = max(0.0, min(1.0, wt.lfo2_depth)) * 50
        _patch_cables(dp, extra=[
            ("lfo2", "oscAWavetablePosition", _cable(depth)),
            ("lfo2", "oscBWavetablePosition", _cable(-depth)),
        ])
        etree.SubElement(
            dp, "equalizer", bass="0x00000000", treble="0x00000000", bassFrequency="0x00000000", trebleFrequency="0x00000000"
        )
        etree.SubElement(sound, "arpeggiator", mode="off", numOctaves="2", syncLevel="7")
        _mod_knobs(sound, {14: ("oscBWavetablePosition", None), 15: ("oscAWavetablePosition", None)})

        _write_xml(sound, xml_path)

    # ------------------------------------------------------------------
    def _export_kit(
        self,
        sset: SampleSet,
        by_note: dict[int, Sample],
        wav_paths: dict[tuple[int, int, int], Path],
        xml_path: Path,
    ) -> None:
        kit = etree.Element(
            "kit",
            firmwareVersion=_FIRMWARE,
            earliestCompatibleFirmware=_MIN_FIRMWARE,
            lpfMode="24dB",
            modFXType="flanger",
            modFXCurrentParam="feedback",
            currentFilterType="lpf",
        )
        etree.SubElement(kit, "delay", pingPong="1", analog="0", syncLevel="7")
        etree.SubElement(kit, "compressor", syncLevel="6", attack="327244", release="936")

        dp = etree.SubElement(
            kit,
            "defaultParams",
            reverbAmount="0x80000000",
            volume="0x3504F334",
            pan="0x00000000",
            sidechainCompressorShape="0xDC28F5B2",
            modFXDepth="0x00000000",
            modFXRate="0xE0000000",
            stutterRate="0x00000000",
            sampleRateReduction="0x80000000",
            bitCrush="0x80000000",
            modFXOffset="0x00000000",
            modFXFeedback="0x80000000",
        )
        etree.SubElement(dp, "delay", rate="0x00000000", feedback="0x80000000")
        etree.SubElement(dp, "lpf", frequency="0x7FFFFFFF", resonance="0x80000000")
        etree.SubElement(dp, "hpf", frequency="0x80000000", resonance="0x80000000")
        etree.SubElement(
            dp, "equalizer", bass="0x00000000", treble="0x00000000", bassFrequency="0x00000000", trebleFrequency="0x00000000"
        )

        sources = etree.SubElement(kit, "soundSources")

        for note in sorted(by_note):
            s = by_note[note]  # Deluge kit pads have no round-robin/velocity-layer engine; one sample (_shipped)
            inst_name = s.metadata.get("instrument", f"note{note:03d}")
            category = classify_instrument(s.metadata.get("instrument", ""))

            # Per-pad roles as Synthstrom's factory kits assign them: only kicks feed the
            # sidechain (25 of 32 factory kicks, no snare/clap/tom — any non-zero send
            # registers a hit, so a hat ducking the mix is never what anyone wants), and
            # hi-hats choke each other (30 of 60 factory hat pads), so a closed hat cuts
            # the open one. The firmware's default send is 0, so other pads omit it.
            pad = {"sideChainSend": "2147483647"} if category == "kick" else {}
            sound = etree.SubElement(
                sources,
                "sound",
                name=inst_name,
                polyphonic="choke" if category in _CHOKE_CATEGORIES else "auto",
                voicePriority="1",
                **pad,
                mode="subtractive",
                lpfMode="24dB",
                modFXType="none",
            )
            wav = wav_paths[(s.note, s.velocity, s.round_robin)]
            osc1 = etree.SubElement(
                sound,
                "osc1",
                type="sample",
                # Play once: a hit runs to its end whatever the note length, as on the
                # factory kits (238 of 290 pads) — cut mode stopped an open hat, crash or
                # 808 tail at the length of a one-step sequencer note. A pad with loop
                # points still loops while held: the firmware obeys loopEnd in any mode.
                loopMode="1",
                reversed="0",
                timeStretchEnable="0",
                timeStretchAmount="0",
                fileName=_deluge_path(wav),
            )
            _zone_el(osc1, wav, s)
            etree.SubElement(
                sound,
                "osc2",
                type="sample",
                loopMode="0",
                reversed="0",
                timeStretchEnable="0",
                timeStretchAmount="0",
            )

            etree.SubElement(sound, "lfo1", type="triangle", syncLevel="0")
            etree.SubElement(sound, "lfo2", type="triangle")
            etree.SubElement(sound, "unison", num="1", detune="8")
            etree.SubElement(sound, "delay", pingPong="1", analog="0", syncLevel="7")
            etree.SubElement(sound, "compressor", syncLevel="6", attack="327244", release="936")

            sdp = etree.SubElement(
                sound,
                "defaultParams",
                arpeggiatorGate="0x00000000",
                portamento="0x80000000",
                compressorShape="0xDC28F5B2",
                oscAVolume="0x7FFFFFFF",
                oscAPulseWidth="0x00000000",
                oscBVolume="0x80000000",
                oscBPulseWidth="0x00000000",
                noiseVolume="0x80000000",
                volume="0x4CCCCCA8",
                pan="0x00000000",
                lpfFrequency="0x7FFFFFFF",
                lpfResonance="0x80000000",
                hpfFrequency="0x80000000",
                hpfResonance="0x80000000",
                lfo1Rate="0x1999997E",
                lfo2Rate="0x00000000",
                modFXRate="0x00000000",
                modFXDepth="0x00000000",
                delayRate="0x00000000",
                delayFeedback="0x80000000",
                reverbAmount="0x80000000",
                arpeggiatorRate="0x00000000",
                stutterRate="0x00000000",
                sampleRateReduction="0x80000000",
                bitCrush="0x80000000",
                modFXOffset="0x00000000",
                modFXFeedback="0x00000000",
            )
            etree.SubElement(
                sdp,
                "equalizer",
                bass="0x00000000",
                treble="0x00000000",
                bassFrequency="0x00000000",
                trebleFrequency="0x00000000",
            )
            etree.SubElement(sound, "arpeggiator", mode="off", numOctaves="2", syncLevel="7")

            cables = etree.SubElement(sdp, "patchCables")
            etree.SubElement(cables, "patchCable", source="velocity", destination="volume", amount="0x3FFFFFE8")
            etree.SubElement(cables, "patchCable", source="aftertouch", destination="volume", amount="0x2A3D7094")
            etree.SubElement(cables, "patchCable", source="y", destination="lpfFrequency", amount="0x19999990")

        etree.SubElement(kit, "selectedDrumIndex").text = "1"
        _write_xml(kit, xml_path)

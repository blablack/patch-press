import shutil
import wave
from collections import defaultdict
from pathlib import Path

import soundfile as sf
from lxml import etree

from ...config.schema import OutputConfig
from ...model.sample import Category, Sample, SampleSet

_FIRMWARE = "4.1.3"
_MIN_FIRMWARE = "4.1.0-alpha"


def _nframes(path: Path) -> int:
    with wave.open(str(path), "rb") as f:
        return f.getnframes()


def _deluge_path(path: Path) -> str:
    """Return SAMPLES-rooted relative path for the Deluge SD card."""
    parts = path.parts
    idx = next((i for i, p in enumerate(parts) if p == "SAMPLES"), None)
    return str(Path(*parts[idx:])) if idx is not None else str(path)


def _write_wavs(sset: SampleSet, output_dir: Path) -> dict[tuple[int, int, int], Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[int, int, int], Path] = {}
    bpm = int(round(sset.tempo_bpm))
    for s in sset.samples:
        if "source_file" in s.metadata:
            fname = Path(s.metadata["source_file"]).name
        else:
            fname = f"note{s.note:03d}_T{bpm:03d}_V{s.velocity:03d}_RR{s.round_robin}.wav"
        p = output_dir / fname
        if sset.category == Category.WAVETABLE:
            # Copy the raw bytes rather than decode/re-encode: the file is unmodified
            # by design (see WavetableAdapter), and re-encoding through soundfile risks
            # dropping the Serum `clm` metadata chunk the firmware may read.
            shutil.copy2(s.metadata["source_file"], p)
        else:
            sf.write(str(p), s.audio.data.T, s.audio.sample_rate)
        paths[(s.note, s.velocity, s.round_robin)] = p
    return paths


def _q31(frac: float) -> str:
    """Encode a 0..1 fraction as the Deluge's signed-32-bit param range.

    Cross-checked against existing template constants: lpfResonance=0x80000000 (off,
    frac=0), lpfFrequency=0x7FFFFFFF (wide open, frac=1), oscAVolume=0x19999999
    (frac~0.6) — all fit raw = round(-2**31 + frac*(2**32-1)).
    """
    frac = max(0.0, min(1.0, frac))
    raw = round(-(2**31) + frac * (2**32 - 1))
    return f"0x{raw & 0xFFFFFFFF:08X}"


def _zone_el(parent, path: Path, sample: Sample) -> None:
    kw = {"startSamplePos": "0", "endSamplePos": str(_nframes(path))}
    if sample.loop_points:
        kw["startLoopPos"] = str(sample.loop_points[0])
        kw["endLoopPos"] = str(sample.loop_points[1])
    etree.SubElement(parent, "zone", **kw)


def _patch_cables(parent, extra: list[tuple[str, str, str]] | None = None) -> None:
    cables = etree.SubElement(parent, "patchCables")
    etree.SubElement(cables, "patchCable", source="velocity", destination="volume", amount="0x3FFFFFE8")
    etree.SubElement(cables, "patchCable", source="aftertouch", destination="volume", amount="0x2A3D7094")
    etree.SubElement(cables, "patchCable", source="y", destination="lpfFrequency", amount="0x19999990")
    for source, destination, amount in extra or []:
        etree.SubElement(cables, "patchCable", source=source, destination=destination, amount=amount)


def _write_xml(root, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True))


class DelugeExporter:
    def export(self, sset: SampleSet, config: OutputConfig, path: Path) -> Path:
        safe_name = config.name.strip().replace("/", "_").replace("\\", "_")
        # Kits live in their own SD-card folder, browsed separately from single-sound
        # SYNTHS presets (confirmed against a real Deluge SD card backup) — a kit XML
        # written under SYNTHS/ wouldn't show up in the firmware's "load kit" browser.
        preset_folder = "KITS" if sset.category == Category.DRUM else "SYNTHS"
        wav_dir = path.parent / "SAMPLES" / path.name / safe_name
        xml_path = path.parent / preset_folder / path.name / f"{safe_name}.xml"
        wav_paths = _write_wavs(sset, wav_dir)

        if sset.category == Category.DRUM:
            self._export_kit(sset, wav_paths, xml_path)
        elif sset.category == Category.WAVETABLE:
            self._export_wavetable(sset, wav_paths, xml_path)
        else:
            self._export_multisample(sset, wav_paths, xml_path)

        return xml_path

    # ------------------------------------------------------------------
    def _export_multisample(
        self,
        sset: SampleSet,
        wav_paths: dict[tuple[int, int, int], Path],
        xml_path: Path,
    ) -> None:
        # One sample per MIDI note. Prefer velocity closest to 100, then lowest RR.
        by_note: dict[int, Sample] = {}
        for s in sset.samples:
            if s.note not in by_note or abs(s.velocity - 100) < abs(by_note[s.note].velocity - 100):
                by_note[s.note] = s

        sorted_notes = sorted(by_note)
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
        osc1 = etree.SubElement(
            sound,
            "osc1",
            type="sample",
            loopMode="0",
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
                loopMode="1" if s.loop_points else "0",
            )
            _zone_el(sr_el, wav, s)

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
            portamento="0x80000000",
            compressorShape="0xDC28F5B2",
            oscAVolume="0x19999999",
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
        etree.SubElement(dp, "envelope1", attack="0x80000000", decay="0xE6666654", sustain="0x7FFFFFFF", release="0x02000000")
        etree.SubElement(dp, "envelope2", attack="0xE6666654", decay="0xE6666654", sustain="0xFFFFFFE9", release="0xE6666654")
        _patch_cables(dp)
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
        """A single wavetable oscillator, driven by the archetype scan_wavetables chose.

        Confirmed against the Deluge firmware source (processing/sound/sound.cpp,
        modulation/params/param.cpp): a single-file wavetable osc is a flat `type=
        "wavetable"` element (no zone/loopMode — those are sample-only), the scan
        position is the `oscAWavetablePosition` defaultParams attribute, and LFO2→
        position is just another patchCable to that same destination string.
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
        etree.SubElement(
            sound, "osc1", type="wavetable", transpose="0", cents="0", retrigPhase="-1",
            fileName=_deluge_path(wav),
        )
        etree.SubElement(sound, "osc2", type="square", transpose="0", cents="0", retrigPhase="-1")
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
            oscBVolume=_q31(0.0),
            oscBPulseWidth="0x00000000",
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
        _patch_cables(dp, extra=[("lfo2", "oscAWavetablePosition", _q31(wt.lfo2_depth))])
        etree.SubElement(
            dp, "equalizer", bass="0x00000000", treble="0x00000000", bassFrequency="0x00000000", trebleFrequency="0x00000000"
        )
        etree.SubElement(sound, "arpeggiator", mode="off", numOctaves="2", syncLevel="7")

        _write_xml(sound, xml_path)

    # ------------------------------------------------------------------
    def _export_kit(
        self,
        sset: SampleSet,
        wav_paths: dict[tuple[int, int, int], Path],
        xml_path: Path,
    ) -> None:
        by_note: dict[int, list[Sample]] = defaultdict(list)
        for s in sset.samples:
            by_note[s.note].append(s)
        for note in by_note:
            by_note[note].sort(key=lambda s: s.round_robin)

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
            note_samples = by_note[note][:2]  # Deluge supports 2 oscillators per pad
            inst_name = note_samples[0].metadata.get("instrument", f"note{note:03d}")

            sound = etree.SubElement(
                sources,
                "sound",
                name=inst_name,
                polyphonic="auto",
                voicePriority="1",
                sideChainSend="2147483647",
                mode="subtractive",
                lpfMode="24dB",
                modFXType="none",
            )
            for i, s in enumerate(note_samples):
                wav = wav_paths[(s.note, s.velocity, s.round_robin)]
                osc = etree.SubElement(
                    sound,
                    f"osc{i + 1}",
                    type="sample",
                    loopMode="1" if s.loop_points else "0",
                    reversed="0",
                    timeStretchEnable="0",
                    timeStretchAmount="0",
                    fileName=_deluge_path(wav),
                )
                _zone_el(osc, wav, s)

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
                oscAVolume="0x00000000",
                oscAPulseWidth="0x00000000",
                oscBVolume="0x00000000",
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
            etree.SubElement(cables, "patchCable", source="velocity", destination="oscAVolume", amount="0x3FFFFFE8")
            etree.SubElement(cables, "patchCable", source="velocity", destination="oscBVolume", amount="0xC0000018")

        etree.SubElement(kit, "selectedDrumIndex").text = "1"
        _write_xml(kit, xml_path)

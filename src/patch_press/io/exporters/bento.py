"""1010music Bento patch exporter.

A Bento patch is a folder: a `patch.xml` describing one track, plus the WAVs it
references by bare filename sitting next to it. Everything below was read off a
real card's factory content (`Patches/`) and cross-checked against the strings in
the device firmware (`bento1.bin`) — no format guesswork:

- User content lives under `UserPatches\\<Type>\\`, the seven roots the firmware
  hardcodes (Granular, Loops, OneShots, SampInst, Slicer, Shredder, Wavetable);
  `Patches\\` is the read-only factory tree. Two of those roots are ours:
  `SampInst` (a `multisamtrack` — keyzoned multisample instrument) and `OneShots`
  (a `samtrack` — 16 independent one-shot pads).
- `samlen`, `loopstart` and `loopend` are frame indices into the WAV, verified
  against the actual `data` chunk sizes of the factory samples.
- A track holds one `<cell type="saminst">` of instrument parameters per voice,
  then the `<cell type="samasst">` sample assignments, then a `<cell
  type="noteseq">`. A multisample has a single `saminst` shared by every sample
  (`multisammode="1"`); a kit repeats the pair once per pad (`multisammode="0"`,
  `celldisppos` = pad slot).
- The device is happy with mono or stereo, 16- or 24-bit, 44.1 or 48 kHz WAVs —
  all four combinations appear in factory patches — so samples ship exactly as
  the pipeline produced them, with no resampling or downmix.
- A patch folder sits exactly one level below its type root (`Patches/SampInst/
  Afternoon Raver`, never a category subfolder) — the loader doesn't resolve a
  patch.xml nested any deeper, even though the device's own file browser will
  happily show it (confirmed on hardware: nested patches were visible but
  silently failed to load). So `output.name`, `output.subfolder` and the
  collection folder passed via `--path` all fold into one flat folder name
  (` - `-joined) instead of nested directories — see `_flat_patch_folder`.

Wavetables are deliberately NOT exported. The `wttrack` oscillator selects its
table with `wavesel`, a 0-based index into a fixed list of 103 table names
compiled into the firmware; across all 130 wavetable cells in the factory bank
that index matches the referenced filename exactly, with no exceptions. There is
no index a user-supplied table could claim, so a wavetable patch built from
patch-press content would name a file the oscillator cannot select. Category
WAVETABLE therefore raises rather than shipping a patch that looks right and
plays a factory table.
"""

import logging
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

import soundfile as sf
from lxml import etree

from ...analysis.drumkit import classify_instrument
from ...config.schema import OutputConfig
from ...model.sample import Category, Sample, SampleSet
from ._common import safe_component, sample_wav_name, subfolder_parts, wav_frame_count

log = logging.getLogger(__name__)

# The firmware's user-content root and the two patch types we target, spelled as the
# firmware spells them.
_PATCH_ROOT = "UserPatches"
_TYPE_MULTISAMPLE = "SampInst"
_TYPE_KIT = "OneShots"

# Every factory kit has exactly 16 pads (416 saminst cells across 26 kits), addressed
# by `celldisppos` 0-15.
_KIT_PADS = 16


def _strip_periods(text: str) -> str:
    """Drop `.` from a name the Bento's own browser will display.

    Confirmed on hardware: a folder like "01. Clean Kit 01" shows up truncated to
    "01" — the browser cuts the name at its first `.`, the same way a naive
    extension-stripping display would. No factory patch or kit name contains a
    period (`Afternoon Raver`, `1010 Bento Kit`, `Club Kit`, `EDM Kit`, `Collection
    Kit 1`, …), which is the strongest sign this is a real device quirk and not a
    one-off. Source folder/file names routinely carry a numbering prefix like
    "01. " or "808 From Mars 909." though, so this can't just be left alone.
    """
    return re.sub(r"\s+", " ", text.replace(".", " ")).strip()


def _flat_patch_folder(path: Path, sub: list[str], safe_name: str) -> str:
    """The single folder name a Bento patch lives in under SampInst/OneShots.

    A real card (`Patches/SampInst/*`, `Patches/OneShots/*`) never nests patches in
    category subfolders — every patch.xml sits exactly one level below the type root.
    A folder one level deeper is still visible in the device's file browser (it's
    just a directory), but the patch loader never resolves it, so patches placed
    there silently fail to load (confirmed on hardware). The collection/subfolder
    info that used to be separate nested folders is folded into the one folder name
    instead, so two different libraries' same-named preset still don't collide.
    """
    joined = " - ".join(part for part in (path.name, *sub, safe_name) if part)
    return _strip_periods(safe_component(joined))


def _params(parent, **kw) -> None:
    etree.SubElement(parent, "params", **kw)


def _cell(track, cell_type: str, **params):
    cell = etree.SubElement(track, "cell", type=cell_type)
    _params(cell, **params)
    return cell


def _saminst_params(*, samtrigtype: str, loopmodes: str, multisammode: str, celldisppos: int) -> dict:
    """The instrument cell's parameters, in the attribute order the device writes them.

    Everything not passed in is the factory's own untouched value: the pipeline has
    already normalised, trimmed and tuned the audio, so gain (millidecibels), pitch
    (millisemitones), pan, filter and drive all stay neutral, and `res` sits at its
    500 centre.
    """
    return {
        "gaindb": "0",
        "pitch": "0",
        "panpos": "0",
        "cellmode": "0",
        "samtrigtype": samtrigtype,
        "loopmodes": loopmodes,
        "reverse": "0",
        # Flat volume envelope: the capture already contains the source patch's own
        # attack and decay, so re-applying them here would stack the swell twice (the
        # same reason the .pti exporter ships a flat envelope). The 200/1000 release is
        # what the device itself writes for an untouched instrument.
        "envattack": "0",
        "envdecay": "0",
        "envsus": "1000",
        "envrel": "200",
        "velamount": "0",
        "outputbus": "0",
        "polymode": "0",
        "chokegrp": "0",
        "dualfilcutoff": "0",
        "res": "500",
        "fx1send": "0",
        "fx2send": "0",
        "overdrive": "0",
        "multisammode": multisammode,
        "interpqual": "0",
        # The pipeline bakes its crossfade into the audio itself, so the device's own
        # loop fade stays off — applying it again would smear an already-faded seam.
        "loopfadeamt": "0",
        "lfowave": "0",
        "lforate": "100",
        "lfoamount": "1000",
        "lfokeytrig": "0",
        "lfobeatsync": "0",
        "lforatebeatsync": "0",
        "legatomode": "0",
        "celldisppos": str(celldisppos),
    }


def _track_params(*, outputbus: bool) -> dict:
    """Track-level parameters, in the device's attribute order.

    `outputbus` is present on a `multisamtrack` and absent from a `samtrack` — that
    difference is the factory patches', not an oversight here.
    """
    params = {
        "selcellpos": "0",
        "celldisppos": "0",
        "cellname": "Track 1",
        "selseqpos": "0",
        "out3gain": "0",
        "fx1send": "0",
        "fx2send": "0",
    }
    if outputbus:
        params["outputbus"] = "0"
    params.update(
        midiinport="0",
        midiinchan="0",
        cc1inport="0",
        cc1inchan="0",
        cc2inport="0",
        midioutport="0",
        midioutchan="0",
        padrowoffset="0",
    )
    return params


def _noteseq(track, *, is_kit: bool) -> None:
    """The empty per-track note sequencer every factory patch carries.

    `notestepcount` is only present on a `samtrack` (kit) noteseq — a real
    `multisamtrack` patch.xml never has it, and adding it there is a schema
    deviation from every factory multisample patch (verified against real
    card content, e.g. `Patches/SampInst/*/patch.xml` has no `notestepcount`
    while `Patches/OneShots/*/patch.xml` does).
    """
    params = {
        "cellname": "",
        "notesteplen": "10",
    }
    if is_kit:
        params["notestepcount"] = "16"
    params.update(
        seqstepmode="1",
        selcellpos="0",
        seqplayenable="1",
        quantsizeseq="0",
    )
    cell = _cell(track, "noteseq", **params)
    etree.SubElement(cell, "sequence")


def _document(track_type: str, *, outputbus: bool):
    """<document><session><track …> — the outer shell shared by every patch type."""
    doc = etree.Element("document")
    session = etree.SubElement(doc, "session", version="1")
    track = etree.SubElement(session, "track", type=track_type)
    _params(track, **_track_params(outputbus=outputbus))
    return doc, track


def _write_wavs(sset: SampleSet, patch_dir: Path) -> dict[tuple[int, int, int], Path]:
    """Write every sample into the patch folder itself.

    Unlike the Deluge's separate SAMPLES tree, a Bento patch references its samples by
    bare filename with no path, so the WAVs have to be siblings of `patch.xml` and their
    names have to be unique within this one folder.
    """
    patch_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[tuple[int, int, int], Path] = {}
    used_names: set[str] = set()
    for s in sset.samples:
        p = patch_dir / sample_wav_name(s, sset.tempo_bpm, used_names)
        sf.write(str(p), s.audio.data.T, s.audio.sample_rate)
        paths[(s.note, s.velocity, s.round_robin)] = p
    return paths


def _select_pads(by_note: dict[int, list[Sample]], limit: int) -> list[int]:
    """Which of a kit's pads survive when it has more of them than the device has slots.

    Note order is already the canonical kick → snare → hats → … order the drum scan
    assigned (analysis/drumkit.py), so the obvious truncation is to keep the first
    `limit`. That fails badly at 16: a 32-pad kit with four kicks and four snares
    spends every slot before reaching a tom or a cymbal, and ships a kit with no
    cymbals at all. So the pick goes round-robin across instrument categories instead
    — every category places its first pad before any places its second — and the
    survivors are then put back in canonical order, which is what decides pad layout.
    """
    notes = sorted(by_note)
    if len(notes) <= limit:
        return notes

    by_category: dict[str, list[int]] = defaultdict(list)
    for note in notes:
        by_category[classify_instrument(by_note[note][0].metadata.get("instrument", ""))].append(note)

    # `notes` is canonically ordered, so category keys land in canonical order too and
    # a partial round (the one that hits the limit) still favours the kick over the tom.
    kept: list[int] = []
    for rank in range(max(len(v) for v in by_category.values())):
        for members in by_category.values():
            if rank < len(members):
                kept.append(members[rank])
                if len(kept) == limit:
                    return sorted(kept)
    return sorted(kept)


def _loop_bounds(sample: Sample, frames: int) -> tuple[int, int]:
    """Loop points for one sample, in frames.

    A sample the loop detector rejected still needs the two attributes; the factory
    patches fill them with the whole sample (see `GritBass_Junos_00`, the one unlooped
    note of an otherwise looped instrument), so that's what an unlooped sample gets.
    """
    if not sample.loop_points:
        return 0, frames
    start, end = sample.loop_points
    return max(0, min(start, frames)), max(0, min(end, frames))


class BentoExporter:
    @classmethod
    def expected_outputs(cls, output: OutputConfig, path: Path) -> list[Path]:
        """Paths whose existence means this preset is already exported.

        A config alone doesn't say kit-vs-multisample (that's decided by the sample
        set's category at export time), so both candidate patch folders are listed —
        callers treat "any exists" as done.
        """
        sub = subfolder_parts(output.subfolder)
        flat = _flat_patch_folder(path, sub, safe_component(output.name))
        return [
            path.parent.joinpath(_PATCH_ROOT, kind, flat, "patch.xml")
            for kind in (_TYPE_MULTISAMPLE, _TYPE_KIT)
        ]

    @classmethod
    def notes_used(cls, notes: Sequence[int]) -> list[int]:
        """A `multisamtrack` keyzones every sample, so the whole grid is used."""
        return list(notes)

    def export(self, sset: SampleSet, config: OutputConfig, path: Path) -> Path:
        if not sset.samples:
            raise ValueError(f"{config.name}: sample set is empty")
        if sset.category == Category.WAVETABLE:
            raise ValueError(
                f"{config.name}: the Bento can't play user wavetables. Its wavetable "
                f"oscillator picks a table by `wavesel`, an index into the 103 tables "
                f"built into the firmware, so there is no way to point it at this file "
                f"(see io/exporters/bento.py). Export wavetables with --format deluge "
                f"or --format pti."
            )

        kind = _TYPE_KIT if sset.category == Category.DRUM else _TYPE_MULTISAMPLE
        sub = subfolder_parts(config.subfolder)
        flat = _flat_patch_folder(path, sub, safe_component(config.name))
        patch_dir = path.parent.joinpath(_PATCH_ROOT, kind, flat)
        wav_paths = _write_wavs(sset, patch_dir)

        if sset.category == Category.DRUM:
            doc = self._build_kit(sset, wav_paths, config.name)
        else:
            doc = self._build_multisample(sset, wav_paths, config.name)

        # The declaration is written by hand rather than by lxml so it matches the
        # device's own byte-for-byte (lxml single-quotes the attribute values).
        xml_path = patch_dir / "patch.xml"
        xml_path.write_bytes(
            b'<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(doc, pretty_print=True)
        )
        return xml_path

    # ------------------------------------------------------------------
    def _build_multisample(
        self,
        sset: SampleSet,
        wav_paths: dict[tuple[int, int, int], Path],
        name: str,
    ):
        # One sample per MIDI note. Prefer velocity closest to 100, then lowest RR —
        # the same collapse the Deluge exporter does. The format does support velocity
        # zones, but nothing upstream produces layered sets worth shipping.
        by_note: dict[int, Sample] = {}
        for s in sset.samples:
            if s.note not in by_note or abs(s.velocity - 100) < abs(by_note[s.note].velocity - 100):
                by_note[s.note] = s
        notes = sorted(by_note)

        # `loopmodes` is a property of the instrument, not of the individual samples, so
        # a set where the loop detector succeeded on some notes and not others has to
        # pick one answer for all of them. Majority wins: turning looping off costs the
        # looped notes their sustain, while turning it on makes every unlooped note
        # repeat its whole length, which drones.
        looped = sum(1 for n in notes if by_note[n].loop_points)
        loop_on = looped * 2 >= len(notes)
        if looped and looped != len(notes):
            log.warning(
                "%s: %d of %d notes have loop points but the Bento loops per-instrument "
                "— shipping loopmodes=%d",
                name, looped, len(notes), int(loop_on),
            )

        doc, track = _document("multisamtrack", outputbus=True)
        # samtrigtype 1 (note/gate, 58 of 65 factory multisamples) sustains while the
        # key is held, rather than the one-shot trigger a drum pad uses.
        _cell(track, "saminst", **_saminst_params(
            samtrigtype="1",
            loopmodes="1" if loop_on else "0",
            multisammode="1",
            celldisppos=0,
        ))

        for i, note in enumerate(notes):
            s = by_note[note]
            wav = wav_paths[(s.note, s.velocity, s.round_robin)]
            frames = wav_frame_count(wav)
            # Zones are centred on their root: each boundary sits midway between two
            # neighbouring roots, so a note is repitched by at most half the capture
            # step in either direction instead of a full step upwards. The outermost
            # zones stretch to the ends of the keyboard so every key sounds.
            low = 0 if i == 0 else (notes[i - 1] + note + 1) // 2
            high = 127 if i == len(notes) - 1 else (note + notes[i + 1] - 1) // 2
            loop_start, loop_end = _loop_bounds(s, frames)
            cell = _cell(
                track, "samasst",
                filename=wav.name,
                rootnote=str(note),
                keyrangebottom=str(low),
                keyrangetop=str(high),
                velroot="63",
                velrangebottom="0",
                velrangetop="128",
                samstart="0",
                samlen=str(frames),
                loopstart=str(loop_start),
                loopend=str(loop_end),
            )
            etree.SubElement(cell, "slices")

        _noteseq(track, is_kit=False)
        return doc

    # ------------------------------------------------------------------
    def _build_kit(
        self,
        sset: SampleSet,
        wav_paths: dict[tuple[int, int, int], Path],
        name: str,
    ):
        by_note: dict[int, list[Sample]] = defaultdict(list)
        for s in sset.samples:
            by_note[s.note].append(s)
        for note in by_note:
            by_note[note].sort(key=lambda s: s.round_robin)

        pads = _select_pads(by_note, _KIT_PADS)
        if len(pads) < len(by_note):
            dropped = [
                by_note[n][0].metadata.get("instrument", f"note{n:03d}")
                for n in sorted(by_note) if n not in set(pads)
            ]
            log.warning(
                "%s: the Bento has %d pads, kit has %d — dropping %s",
                name, _KIT_PADS, len(by_note), ", ".join(dropped),
            )

        doc, track = _document("samtrack", outputbus=False)
        for slot, note in enumerate(pads):
            s = by_note[note][0]  # a pad plays one sample: no round-robin or velocity engine
            wav = wav_paths[(s.note, s.velocity, s.round_robin)]
            frames = wav_frame_count(wav)
            # samtrigtype 0 / loopmodes 0: a pad fires the hit once and lets it ring
            # out, which is what every factory kit does.
            _cell(track, "saminst", **_saminst_params(
                samtrigtype="0",
                loopmodes="0",
                multisammode="0",
                celldisppos=slot,
            ))
            cell = _cell(
                track, "samasst",
                filename=wav.name,
                # A pad is addressed by its slot, not by pitch: the factory kits leave
                # every one of these at 0 and let `celldisppos` do the mapping.
                rootnote="0",
                keyrangebottom="0",
                keyrangetop="0",
                velroot="0",
                velrangebottom="0",
                velrangetop="0",
                samstart="0",
                samlen=str(frames),
                loopstart="0",
                loopend=str(frames),
            )
            etree.SubElement(cell, "slices")

        _noteseq(track, is_kit=True)
        return doc

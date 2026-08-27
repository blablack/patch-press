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
from .bento_index import derive_tags, update_index

log = logging.getLogger(__name__)

# The firmware's user-content root and the two patch types we target, spelled as the
# firmware spells them.
_PATCH_ROOT = "UserPatches"
_TYPE_MULTISAMPLE = "SampInst"
_TYPE_KIT = "OneShots"

# Every factory kit has exactly 16 pads (416 saminst cells across 26 kits), addressed
# by `celldisppos` 0-15.
_KIT_PADS = 16

# `loopfadeamt` — the `Loop Fade` knob — for a patch whose loops this pipeline detected
# rather than read from the source file.
#
# This is the one number here that isn't derived, because the firmware doesn't say what
# its units are: the parameter table carries only the name pair, and the factory bank
# uses it in just 3 of the 481 cells that have the attribute (100, 200, 299 — everything
# else ships 0, because factory samples are looped clean by hand). 200 is what
# `SampInst/SciFi` uses, a 109-sample looped multisample and the closest factory analogue
# to what this exporter produces.
#
# It does not need to be exact. The whole point of letting the device fade the seam
# instead of baking it is that `Loop Fade` stays a knob on the front panel — this is a
# starting value to turn, not a setting to live with.
_LOOP_FADE = 200


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


# A folder label this short is already readable, so it ships as written; a longer
# multi-word one is reduced to its initials. 11 is where "Vinyl Synths" becomes "VS"
# while "Dream Synth" and "Third Party" stay spelled out — tuned by eye over the
# ~2200-preset corpus, like the loop-detection constants elsewhere in this codebase.
_VERBATIM_LIMIT = 11

_TOKEN_RE = re.compile(r"[0-9A-Za-z']+")
# A source folder's ordering prefix: "01. Bass", "1 BASS", "07 - FX". Capped at two
# digits so a folder that simply starts with a number keeps it ("808 From Mars",
# "2600 From Mars").
_NUMBERING_RE = re.compile(r"^\d{1,2}\s*[.\-_)]?\s+")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _normalise_label(part: str) -> str:
    """A source folder name as a browser label: no ordering prefix, no shouting."""
    part = _NUMBERING_RE.sub("", part).strip()
    # "1 BASS" -> "Bass", but short all-caps are acronyms — "Vinyl SP from Mars" and
    # "06. FX" mean something, while "Sp" and "Fx" just read like typos.
    return " ".join(
        w.capitalize() if w.isalpha() and w.isupper() and len(w) >= 4 else w
        for w in part.split()
    )


def _initials(part: str) -> str:
    return "".join(t if t[0].isdigit() else t[0].upper() for t in _tokens(part))


def _abbreviate(part: str) -> str:
    """Initials for a long multi-word label, verbatim for anything else.

    A single token is always kept whole — "BrontoScorpio" has no initials worth
    taking and would collapse to "B".
    """
    if len(part) <= _VERBATIM_LIMIT or len(_tokens(part)) < 2:
        return part
    return _initials(part)


def _prefix_labels(collection: str, sub: list[str]) -> list[str]:
    """The grouping labels for a patch, from its collection and source subfolder.

    Deliberately never looks at the preset's own name. This prefix is the only thing
    that keeps a bank together in the browser's single flat alphabetical list, so
    every preset out of one source folder has to get a byte-identical one — deriving
    any part of it from the preset name would file "HS Bass Nine" and "HS Boomer"
    under different headings.

    A bank name usually repeats its collection ("Samples from Mars" /
    "Vinyl Synths from Mars"); the repeat is dropped so it is said once.
    """
    labels, used = [], set()
    for raw in (collection, *sub):
        label = _normalise_label(raw)
        fresh = [t for t in _tokens(label) if t.lower() not in used]
        used.update(t.lower() for t in _tokens(label))
        if fresh:
            labels.append(" ".join(fresh))
    return labels


def _strip_label_echo(name: str, labels: list[str]) -> str:
    """Drop the part of a preset name that a prefix label already says.

    Libraries stamp their own name onto every preset they ship: "Orchestral Brass
    Full Ensemble Marcato" inside the `Orchestral Brass` folder, "MS20 Fuzz Mod Vinyl
    Synths C0" inside `Vinyl Synths from Mars`. A run of two or more tokens is an
    echo wherever it sits — that last one ends with the folder name *then* the root
    note, so head/tail matching alone would miss it. A single token only counts as an
    echo at the head or the tail: "HS Bass Nine" under `1 BASS` is naming itself.
    """
    for label in labels:
        ltoks = [t.lower() for t in _tokens(label)]
        for n in range(len(ltoks), 0, -1):
            run = ltoks[:n]
            ntoks = [t.lower() for t in _tokens(name)]
            if len(ntoks) <= len(run):
                continue
            pat = r"[^0-9A-Za-z']+".join(re.escape(t) for t in run)
            if len(run) > 1:
                stripped = re.sub(rf"(?<![0-9A-Za-z']){pat}(?![0-9A-Za-z'])", "", name, flags=re.I)
            elif ntoks[: len(run)] == run:
                stripped = re.sub(rf"^{pat}[^0-9A-Za-z']*", "", name, flags=re.I)
            elif ntoks[-len(run):] == run:
                stripped = re.sub(rf"[^0-9A-Za-z']*{pat}$", "", name, flags=re.I)
            else:
                continue
            stripped = re.sub(r"\s{2,}", " ", stripped).strip(" -_")
            # A run that didn't actually match leaves the name alone — fall through to
            # the shorter runs of the same label rather than giving up on it.
            if stripped and stripped != name:
                name = stripped
                break
    return name


def _flat_patch_folder(path: Path, sub: list[str], safe_name: str) -> str:
    """The single folder name a Bento patch lives in under SampInst/OneShots.

    A real card (`Patches/SampInst/*`, `Patches/OneShots/*`) never nests patches in
    category subfolders — every patch.xml sits exactly one level below the type root.
    A folder one level deeper is still visible in the device's file browser (it's
    just a directory), but the patch loader never resolves it, so patches placed
    there silently fail to load (confirmed on hardware). The collection/subfolder
    info that would have been nested directories is folded into the one folder name
    instead, so two different libraries' same-named preset still don't collide.

    That one name is also the *only* handle the device gives you: it is what the
    browser prints and the key it sorts a single flat list by. Spelling every level
    out in full ("Samples from Mars - Vinyl Synths from Mars - 02 Keys & Pads - …")
    pushes what distinguishes a preset past the visible width, and 1242 patches that
    all begin "Samples from Mars - " are indistinguishable on screen. So each level
    is abbreviated to a short stable label and the library's echo is dropped out of
    the preset name, leaving "SFM VS Keys Pads - Polaris Space Delay Soft D2".
    Factory patch names run 9.5 characters on average and never exceed 18, which is
    the yardstick this is aiming at.
    """
    labels = _prefix_labels(path.name, sub)
    name = _strip_label_echo(safe_name, labels) or safe_name
    prefix = " ".join(_abbreviate(label) for label in labels)
    joined = f"{prefix} - {name}" if prefix else name
    return _strip_periods(safe_component(joined))


def _params(parent, **kw) -> None:
    etree.SubElement(parent, "params", **kw)


def _cell(track, cell_type: str, **params):
    cell = etree.SubElement(track, "cell", type=cell_type)
    _params(cell, **params)
    return cell


def _saminst_params(
    *, samtrigtype: str, loopmodes: str, multisammode: str, celldisppos: int, loopfadeamt: int = 0
) -> dict:
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
        "loopfadeamt": str(loopfadeamt),
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

    @classmethod
    def bakes_loop_crossfade(cls) -> bool:
        """No. The Bento crossfades the loop seam itself, so the WAVs ship untouched.

        The device has a `Loop Fade` parameter per instrument, which makes baking the
        fade into the audio the wrong trade twice over: it would fade the same seam
        twice, and it would weld a choice into the samples that the front panel could
        otherwise adjust at any time. A baked crossfade is permanent — on the card there
        is no way back to the unfaded audio. So the pipeline works the length out and
        records it, ships the samples as captured, and `loopfadeamt` carries the job.
        """
        return False

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

        # The browser's tag filter is the only way to narrow a flat card, and the index
        # it reads is an ordinary file, so the build fills it in. Best-effort: a preset
        # that is on the card but not in the index still browses and loads fine, so a
        # read-only card or a lock we can't take must not fail the export.
        try:
            update_index(
                path.parent / _PATCH_ROOT,
                f"{_PATCH_ROOT}\\{kind}\\{flat}",
                derive_tags(sset.category, path.name, sub, config.name),
            )
        except OSError as exc:
            log.warning("%s: could not update the patch tag index (%s)", config.name, exc)
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

        # An authored loop (a library WAV's `smpl` chunk, a Bitwig zone's own loop) is
        # the author's own clean seam, and fading it would only smear the join they
        # chose — exactly why the pipeline ships those verbatim. A detected loop has no
        # such guarantee and wants the device's fade. Like `loopmodes`, this is one
        # value for the whole instrument, so the majority decides.
        authored = sum(
            1 for n in notes if by_note[n].analysis.get("loop_source") == "authored_smpl"
        )
        fade = _LOOP_FADE if loop_on and authored * 2 < len(notes) else 0

        doc, track = _document("multisamtrack", outputbus=True)
        # samtrigtype 1 (note/gate, 58 of 65 factory multisamples) sustains while the
        # key is held, rather than the one-shot trigger a drum pad uses.
        _cell(track, "saminst", **_saminst_params(
            samtrigtype="1",
            loopmodes="1" if loop_on else "0",
            multisammode="1",
            celldisppos=0,
            loopfadeamt=fade,
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

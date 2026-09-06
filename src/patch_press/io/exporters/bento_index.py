"""The Bento's patch tag index — `UserPatches/patchindex.xml`.

The device gives a flat card exactly one way to narrow a list: the browser's tag
filter. That filter's vocabulary is closed. `bento1.bin` holds a 17-entry pointer
table at file offset `0x14e460` — `All`, then 15 tag names in alphabetical order,
then `User` — and each of those 15 strings is referenced exactly once in the whole
binary, from that one table. There is no "new tag" UI string either (`Patch Tagger`
and `Instrument Tags` are the only tag-related strings in the firmware), and a
hand-written tag outside the 15 was confirmed ignored on hardware. So a preset can
only ever be filed under a name from `_TAGS`.

What *is* open is the index file. `PatchMetadataFile::LoadMetadata` reads
`patchindex.xml` from the content root, and the copy under `UserPatches/` is an
ordinary file the device honours whether the device or a build wrote it (confirmed on
hardware). Writing it here is what turns ~2200 flat folders into something filterable.

Formatting is not load-bearing: the factory `Patches/patchindex.xml` indents its first
entries with four spaces and its last with tabs, so the parser plainly does not care.
The head's spelling is what this module copies — no BOM, LF endings, four spaces,
backslash-separated paths.
"""

import fcntl
import logging
import re
from collections.abc import Collection
from pathlib import Path
from xml.sax.saxutils import escape, unescape

from ...model.sample import Category

log = logging.getLogger(__name__)

_INDEX_NAME = "patchindex.xml"

# The closed vocabulary, spelled and ordered as the firmware's table spells it.
_TAGS = (
    "Atmosphere", "Bass", "Drum", "Foley", "Guitar", "Keys", "Lead", "Orchestral",
    "Organ", "Pad", "Percussion", "SFX", "Strings", "Synth", "Vocal",
)

# What a word in a source folder or preset name says about the sound. Libraries label
# their own banks better than any audio analysis would ("01. Bass", "03. Leads",
# "Orchestral Woodwinds", "Distant Choir"), so this reads their vocabulary rather than
# re-deriving one. Every value is from `_TAGS`; anything else the device drops.
_KEYWORDS = {
    "Bass": ("bass", "basses", "sub", "subbass"),
    "Lead": ("lead", "leads"),
    "Pad": ("pad", "pads"),
    "Keys": ("key", "keys", "keyboard", "keyboards", "piano", "pianos", "rhodes",
             "wurlitzer", "wurli", "clav", "clavinet", "chord", "chords", "mallet",
             "bell", "bells", "harpsichord", "celeste"),
    "Organ": ("organ", "organs", "farfisa", "hammond", "leslie"),
    "Strings": ("string", "strings", "violin", "violins", "cello", "cellos", "viola",
                "pizzicato", "pizz"),
    "Orchestral": ("orchestral", "orchestra", "brass", "horn", "horns", "trumpet",
                   "trumpets", "trombone", "trombones", "tuba", "woodwind", "woodwinds",
                   "flute", "flutes", "clarinet", "clarinets", "oboe", "bassoon",
                   "bassoons", "ensemble", "timpani", "marcato", "staccato"),
    "Vocal": ("vocal", "vocals", "vox", "voice", "voices", "choir", "chant", "chants",
              "female", "male"),
    "Guitar": ("guitar", "guitars", "gtr"),
    "Percussion": ("perc", "percs", "percussion", "percussive", "conga", "congas",
                   "bongo", "bongos", "shaker", "tambourine", "cowbell", "claves",
                   "maracas"),
    "Drum": ("drum", "drums", "drumkit", "kit", "kits", "kick", "snare", "hat", "hats",
             "clap", "cymbal", "cymbals", "tom", "toms", "break", "breakbeat"),
    "SFX": ("fx", "sfx", "effect", "effects", "riser", "sweep", "impact", "noise",
            "scatter", "glitch"),
    "Atmosphere": ("atmos", "atmosphere", "atmospheric", "ambient", "ambience", "drone",
                   "texture", "textures", "evolving", "calm", "distant", "dream",
                   "dreams", "dreamy"),
    "Foley": ("foley", "field", "found"),
    "Synth": ("synth", "synths", "synthesizer", "poly", "polysynth", "mono", "analog",
              "analogue", "digital", "arp", "saw", "square", "pulse", "pwm", "acid",
              "rhythmic", "template", "templates"),
}

_WORD_FOR_TAG = {word: tag for tag, words in _KEYWORDS.items() for word in words}

# Factory patches carry 1–6 tags, but 2–3 is where the bulk sits (179 of 236) and past
# that a tag stops narrowing anything. Three keeps the strongest signals.
_MAX_TAGS = 3

_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
# Library preset names are routinely run together — "CalmBell", "SubOsc",
# "Fisherman'sFriend" — and a single glued token matches no keyword at all. Split where
# a lowercase or digit runs into a capital, which leaves device names ("MS20", "VP330",
# "DX100") whole because they have no such boundary.
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _words(text: str) -> list[str]:
    return _TOKEN_RE.findall(_CAMEL_RE.sub(" ", text).lower())


def derive_tags(category: Category, collection: str, sub: list[str], name: str) -> list[str]:
    """Pick up to three of the Bento's 15 tags for one preset.

    Read in order of how much the source trusted the label: the deepest source folder
    first (that's the library's own category — "01. Bass", "Orchestral Woodwinds"),
    then the shallower ones, then the collection, and only then the preset's own name.
    A kit is a `Drum` before anything else, since that is what the browser filter is
    for. Anything with no signal at all lands on `Synth`, the tag the factory bank
    itself uses for three quarters of its melodic patches.
    """
    tags: list[str] = []

    def add(tag: str) -> None:
        if tag not in tags and len(tags) < _MAX_TAGS:
            tags.append(tag)

    if category == Category.DRUM:
        add("Drum")

    for source in (*reversed(sub), collection, name):
        for token in _words(source):
            tag = _WORD_FOR_TAG.get(token)
            if tag:
                add(tag)

    if not tags:
        add("Synth")
    return tags


def _parse(raw: bytes) -> dict[str, list[str]]:
    """Existing entries, as {patch path: tags}.

    Deliberately regex-based rather than a real XML parse: this file is co-owned with
    the device, and the only thing we need out of it is which paths are already
    spoken for, so a stray attribute or a hand edit shouldn't cost the user their tags.

    But it MUST undo what `_render` did. `update_index` is a read-modify-write called
    once per preset, so parse and render sit in a loop with each other: a `_parse` that
    handed back `Long &amp; Saturated` where the folder is `Long & Saturated` would let
    `_render` escape it again on the next preset, and again on the one after — a name
    with `&` grows one `amp;` per preset in the build. That is not hypothetical; it is
    what the first Bento build did, turning 16 preset names into 168 KB of `&amp;` and
    leaving every one of them with a path the device cannot match to any folder, so
    they shipped untagged.
    """
    entries: dict[str, list[str]] = {}
    for block in re.findall(rb"<patch\s+path=\"(.*?)\"\s*>(.*?)</patch>", raw, re.S):
        path = unescape(block[0].decode("utf-8", "replace"), {"&quot;": chr(34)})
        entries[path] = [unescape(t.decode("utf-8", "replace"))
                         for t in re.findall(rb"<tag>(.*?)</tag>", block[1], re.S)]
    return entries


def _render(entries: dict[str, list[str]]) -> bytes:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<patchmetadata version="1">']
    for path in sorted(entries):
        lines.append(f'    <patch path="{escape(path, {chr(34): "&quot;"})}">')
        lines.extend(f"        <tag>{escape(tag)}</tag>" for tag in entries[path])
        lines.append("    </patch>")
    lines.append("</patchmetadata>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def update_index(patch_root: Path, patch_path: str, tags: list[str]) -> None:
    """Upsert one patch's tags into `<patch_root>/patchindex.xml`.

    Read-modify-write under an exclusive lock, because `build_presets.py --jobs K` runs
    several `patch-press batch` processes over one output tree at once and they all
    claim this single file. The cost is one rewrite of a ~200 KB file per preset,
    which is nothing next to the tens of megabytes of WAVs the same preset just wrote.

    Entries for patches this run didn't build — other collections, or tags the user set
    on the device — are read back and preserved, so a resumed or partial build never
    costs anyone their tagging.
    """
    index = patch_root / _INDEX_NAME
    index.parent.mkdir(parents=True, exist_ok=True)
    with open(index, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        if raw.strip() and b"<patchmetadata" not in raw:
            log.warning(
                "%s is not a patch index — leaving it alone and shipping %s untagged",
                index, patch_path,
            )
            return
        entries = _parse(raw)
        entries[patch_path] = tags
        fh.seek(0)
        fh.truncate()
        fh.write(_render(entries))


def sync_index(
    source_index: Path, dest_index: Path, wanted: Collection[str], *, execute: bool = True
) -> tuple[int, int]:
    """Copy tag entries for `wanted` patch paths from one patchindex.xml into another.

    `update_index` above assumes the caller ships everything it builds — `bento.export()`
    writes one entry per patch as it goes, so the index at the end of a run is complete for
    that run's own output tree. That assumption breaks for tooling that curates a subset of
    a build afterwards (a fixed "these patches go on this card" list, smaller than everything
    `sample`/`batch` produced): copying the whole source index onto the card would tag
    patches that were never written there, and `rsync --exclude patchindex.xml` — the
    workaround `docs/outputs/bento.md` documents for plain whole-tree syncs — throws every
    tag away instead, including ones already on the device.

    This is the third option: read-modify-write `dest_index` exactly like `update_index`
    does per-patch (lock held, entries this call doesn't touch — other collections, a
    previous sync, a tag set by hand on the device — read back and preserved), but apply it
    to `wanted` in bulk from a source index that was built once, off to the side.

    `execute=False` computes the same (updated, missing) counts a real run would without
    writing `dest_index`, for a caller with its own dry-run/plan convention.

    Returns (updated, missing) — `missing` counts `wanted` paths absent from `source_index`
    (nothing derived a tag for them, or the source is stale); those are left untouched in
    `dest_index` rather than cleared, the same best-effort stance as an unwritable index:
    an unsynced tag never blocks a patch from working.
    """
    source_entries = _parse(source_index.read_bytes()) if source_index.exists() else {}

    def _apply(entries: dict[str, list[str]]) -> tuple[int, int]:
        updated = missing = 0
        for path in wanted:
            if path in source_entries:
                entries[path] = source_entries[path]
                updated += 1
            else:
                missing += 1
        return updated, missing

    if not execute:
        raw = dest_index.read_bytes() if dest_index.exists() else b""
        if raw.strip() and b"<patchmetadata" not in raw:
            return 0, len(wanted)
        return _apply(_parse(raw))

    dest_index.parent.mkdir(parents=True, exist_ok=True)
    with open(dest_index, "a+b") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read()
        if raw.strip() and b"<patchmetadata" not in raw:
            log.warning("%s is not a patch index — leaving it alone", dest_index)
            return 0, len(wanted)
        entries = _parse(raw)
        updated, missing = _apply(entries)
        fh.seek(0)
        fh.truncate()
        fh.write(_render(entries))
    return updated, missing

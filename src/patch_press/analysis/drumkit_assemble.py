"""Assemble synthetic kits from a "bag of hits" library folder.

See CLAUDE.md / assemble-kits: some libraries (e.g. Samples From Mars'
909_from_mars/Individual Hits) aren't shipped as pre-made kits at all — they're a
browsing bank organized by instrument category (Bass Drum, Snare Drum, ...), each
subdivided by a descriptive "flavor" taxonomy that repeats *across* categories
(Clean, Color, Tape, Various, ...). A real vendor-shipped kit from the same library
(909_from_mars/Kits/01. Clean Kit) confirmed this taxonomy IS the vendor's own
recipe: that kit is built almost entirely from Clean-tagged files across every
category. So: generate one kit per shared flavor tag, picking one file per
instrument category from that flavor's subfolder.

Zero audio decoding here — this is pure filesystem/string analysis, unlike the
loop/pitch/archetype detectors elsewhere in analysis/.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from .drumkit import _PRIORITY, classify_folder_name, classify_instrument, normalize_tag

# Categories that are themselves a structural split of one instrument family, used
# only to avoid a kick-only tag like TUBE/TAPE (which never appears anywhere else)
# from counting as "shared" just because hat_closed and hat_open both happen to
# have it — collapse to one family before counting.
_FAMILY = {
    "hat_closed": "hat", "hat_open": "hat",
    "cymbal_crash": "cymbal", "cymbal_ride": "cymbal",
    "tom_low": "tom", "tom_mid": "tom", "tom_high": "tom",
    "conga_low": "conga", "conga_mid": "conga", "conga_high": "conga",
}


def _family(category: str) -> str:
    return _FAMILY.get(category, category)


@dataclass(frozen=True)
class HitFile:
    category: str
    tags: frozenset[str]
    path: Path


def walk_hit_tree(root: Path) -> list[HitFile]:
    """Recursively classify every WAV under `root` into (category, flavor tags).

    A subfolder is either a *structural* split (its name itself resolves to a
    drum-instrument category, e.g. CH/OH under Hi Hat, Crash/Ride under Cymbal —
    each becomes its own required pad slot) or a *flavor* split (its name doesn't
    resolve to any instrument, e.g. Clean/Color — accumulated as a tag on every
    file beneath it). Each file's own category comes from classifying its
    filename directly (same classify_instrument used for tier-1 flat kit folders),
    not from the folder it happens to sit in — a "Tom" folder that only splits
    into Clean/Color still contains individually-named Tom Hi/Mid/Lo files, and
    per-file classification is what recovers those three distinct pads.
    """
    hits: list[HitFile] = []

    def _walk(folder: Path, tags: frozenset[str]) -> None:
        subdirs = sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))
        for wav in sorted(folder.glob("*.wav", case_sensitive=False)):
            hits.append(HitFile(category=classify_instrument(wav.stem), tags=tags, path=wav))
        for sub in subdirs:
            resolved = classify_folder_name(sub.name)
            if resolved != "other":
                _walk(sub, tags)
            else:
                _walk(sub, tags | {normalize_tag(sub.name)})

    for top in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        _walk(top, frozenset())

    return hits


def discover_flavors(hits: list[HitFile], min_families: int = 2) -> list[str]:
    """Tags that appear across >= min_families distinct instrument families.

    Keeps a kick-only tag (TUBE/TAPE in 909_from_mars — never appears under any
    other category) from producing a "kit" that's only coherent for one pad.
    """
    tag_families: dict[str, set[str]] = defaultdict(set)
    for hit in hits:
        family = _family(hit.category)
        for tag in hit.tags:
            tag_families[tag].add(family)
    return sorted(tag for tag, families in tag_families.items() if len(families) >= min_families)


def assemble_kit(hits: list[HitFile], flavor: str) -> dict[str, tuple[Path, int]]:
    """Pick one file per category present anywhere in the tree, for one kit.

    tier 1: a file tagged with `flavor` for this category.
    tier 2: fall back to this category's own VARIOUS-tagged pool.
    tier 3: fall back to any file of this category (e.g. Cymbal in 909_from_mars,
    which carries no flavor tags at all — always tier 3, for every flavor).
    """
    by_category: dict[str, list[HitFile]] = defaultdict(list)
    for hit in hits:
        by_category[hit.category].append(hit)

    chosen: dict[str, tuple[Path, int]] = {}
    for category in sorted(by_category, key=lambda c: _PRIORITY.get(c, len(_PRIORITY))):
        entries = by_category[category]
        for tier, predicate in (
            (1, lambda h: flavor in h.tags),
            (2, lambda h: "VARIOUS" in h.tags),
            (3, lambda h: True),
        ):
            candidates = sorted((h.path for h in entries if predicate(h)), key=lambda p: p.name)
            if candidates:
                chosen[category] = (candidates[0], tier)
                break
    return chosen

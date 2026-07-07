"""Instrument classification for flat drum-kit folders (one WAV = one pad).

See CLAUDE.md / docs research: libraries like Samples From Mars' "808 From Mars"
ship one folder per kit, with loose one-shot WAVs directly inside — no shared
per-note structure to group by (unlike a multisample) and no per-instrument
subfolders (unlike scan-library's existing 'kit' shape). The only signal for what
each file is is the filename itself.

Category order below is not guessed: it matches the one invariant found across
Synthstrom's own factory kits (000 TR-808.XML, 003 TR-909.XML, 005 R-100.XML) —
kick, snare, closed hat, open hat, then a percussion-ish middle group, then
cymbals, then toms and congas each ordered low-to-high as their own contiguous
block.
"""

from __future__ import annotations

import re

_CATEGORY_ORDER = [
    "kick", "snare", "hat_closed", "hat_open",
    "clap", "snap", "rim", "cowbell", "claves", "maracas", "tambourine", "shaker",
    "triangle", "block", "guiro", "bell",
    "cymbal_crash", "cymbal_ride",
    "tom_low", "tom_mid", "tom_high",
    "conga_low", "conga_mid", "conga_high",
    "other",
]
_PRIORITY = {name: i for i, name in enumerate(_CATEGORY_ORDER)}

_TOKEN_RE = re.compile(r"[A-Za-z0-9#]+")

# Direct token → category. Checked first; wins outright except for the three
# multi-part families (hat/tom/conga) below, which need a High/Low/Open/Closed
# modifier token elsewhere in the filename to pick the specific bucket.
_DIRECT: dict[str, str] = {
    "BD": "kick", "KICK": "kick", "KCK": "kick", "BASSDRUM": "kick",
    "SD": "snare", "SNARE": "snare", "SNR": "snare", "SNAR": "snare", "SNAREDRUM": "snare",
    "CLAP": "clap",
    "SNAP": "snap",
    "HANDCLAP": "clap",
    "RIM": "rim", "RIMSHOT": "rim",
    "COWBELL": "cowbell", "COWB": "cowbell",
    "CLAVES": "claves", "CLAVE": "claves", "CLAV": "claves",
    "MARACA": "maracas", "MARACAS": "maracas",
    "TAMBOURINE": "tambourine", "TAMB": "tambourine",
    "SHAKER": "shaker", "CABASA": "shaker",
    "TRIANGLE": "triangle", "TRIA": "triangle",
    "BLOCK": "block", "WOODBLOCK": "block",
    "GUIRO": "guiro",
    "BELL": "bell",
    "CRASH": "cymbal_crash", "CRAS": "cymbal_crash",
    "RIDE": "cymbal_ride",
    "CYM": "cymbal_crash", "CYMBAL": "cymbal_crash",
}
_HAT_TOKENS = {"CH", "OH", "HH", "HAT", "HIHAT"}
_TOM_TOKENS = {"TOM", "TOML", "TOMM", "TOMH"}
_CONGA_TOKENS = {"CONGA", "CONGAS", "CONL", "CONM", "CONH", "BONGO", "BONGOS"}
_LOW_MOD = {"LOW", "LO"}
_HIGH_MOD = {"HIGH", "HI"}
_OPEN_MOD = {"OPEN", "OH"}


def classify_instrument(stem: str) -> str:
    """Return a category key (see _CATEGORY_ORDER) for a drum-hit filename.

    'other' means no known drum/percussion token was found (e.g. an FX one-shot
    bundled into an otherwise-normal kit folder) — it still gets a pad, just
    sorted last, rather than being dropped.
    """
    tokens = {t.upper() for t in _TOKEN_RE.findall(stem)}

    for tok in tokens:
        if tok in _DIRECT:
            return _DIRECT[tok]

    if tokens & _HAT_TOKENS:
        return "hat_open" if tokens & _OPEN_MOD else "hat_closed"
    if tokens & _TOM_TOKENS:
        if tokens & _LOW_MOD:
            return "tom_low"
        if tokens & _HIGH_MOD:
            return "tom_high"
        return "tom_mid"
    if tokens & _CONGA_TOKENS:
        if tokens & _LOW_MOD:
            return "conga_low"
        if tokens & _HIGH_MOD:
            return "conga_high"
        return "conga_mid"

    return "other"


def sort_key(stem: str) -> tuple[int, str]:
    """Kit-row sort key: category priority, then alphabetical stem as a stable
    tiebreak within a category (order among round-robin-ish variants of the same
    instrument doesn't carry meaning, so alphabetical is as good as any choice).
    """
    return (_PRIORITY[classify_instrument(stem)], stem)


_ORDINAL_PREFIX_RE = re.compile(r"^\d+\.\s*")
_TRAILING_DIGITS_RE = re.compile(r"\s*\d+$")


def classify_folder_name(name: str) -> str:
    """Classify a *folder* name (e.g. '01. Bass Drum', 'CH', '02. Cymbal') into a
    category, for walking a bag-of-hits library tree (see analysis/drumkit_assemble.py).

    Folder names are often two words ('Bass Drum', 'Hand Clap') where each word
    tokenizes separately and neither matches _DIRECT on its own — unlike a compact
    filename token (BD, SD), a folder name is normalized by stripping the leading
    'NN. ' ordinal and all remaining separators before classification, so 'Bass Drum'
    concatenates to one 'BASSDRUM' token. classify_instrument itself is untouched;
    this normalization would incorrectly merge multi-word *filenames* into one token.
    """
    core = _ORDINAL_PREFIX_RE.sub("", name)
    return classify_instrument(re.sub(r"[^A-Za-z0-9#]", "", core))


def normalize_tag(name: str) -> str:
    """Normalize a *flavor* subfolder name ('01. Clean', 'Color 03') to a canonical
    tag ('CLEAN', 'COLOR') — strips the leading ordinal and a trailing numbered-
    variant suffix, so 'Color 01'..'Color 05' all collapse to the same tag.
    """
    core = _ORDINAL_PREFIX_RE.sub("", name)
    core = _TRAILING_DIGITS_RE.sub("", core)
    return re.sub(r"[^A-Za-z0-9#]", "", core).upper()

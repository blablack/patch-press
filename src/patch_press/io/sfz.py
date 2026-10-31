"""Minimal SFZ reader: the key mapping, velocity layers and loops of an instrument.

SFZ is an open plain-text format (https://sfzformat.com). Only the opcodes that decide
*what plays where* are read; synthesis parameters (filters, envelopes, LFOs, effects) are
ignored, as they are for every other multisample source. Handled:

  * the `<control> / <global> / <master> / <group> / <region>` inheritance chain
  * `#define $NAME value` and `#include "file"`, `default_path`, comments
  * `sample` paths (Windows backslashes, case-insensitive match on Linux)
  * `key`, `lokey`/`hikey`, `pitch_keycenter` (numbers or note names), `lovel`/`hivel`
  * `offset`, `end`
  * `loop_mode`/`loopmode`, `loop_start`/`loopstart`, `loop_end`/`loopend`, `loop_crossfade`;
    a loop mode with no points falls back to the WAV's own `smpl` loop
  * `volume`, `pan`, `tune` + `transpose`, `pitch_keytrack`, `seq_position`
  * `trigger=release` regions and keyswitched (`sw_last`) articulations other than the
    default one are skipped — they are not the instrument's sustain sound
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .adapters.zonemap import Zone
from .smpl import read_loop_points

log = logging.getLogger(__name__)

_NOTE_PC = {"c": 0, "d": 2, "e": 4, "f": 5, "g": 7, "a": 9, "b": 11}
_NOTE_RE = re.compile(r"^([a-gA-G])([#b]?)(-?\d+)$")
_OPCODE_RE = re.compile(r"(?:^|(?<=\s))([A-Za-z_][\w$]*)=")
_HEADER_RE = re.compile(r"<(\w+)>")
_DEFINE_RE = re.compile(r"^\s*#define\s+(\$\w+)\s+(.*?)\s*$", re.MULTILINE)
_INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"\s*$', re.MULTILINE)

# Old names some vendors still write, mapped to the current opcode.
_ALIASES = {"loopmode": "loop_mode", "loopstart": "loop_start", "loopend": "loop_end", "loopcrossfade": "loop_crossfade"}
_LOOPING_MODES = {"loop_continuous", "loop_sustain"}


def _note(value: str) -> int:
    """A MIDI note number from '60' or a name like 'c4', 'F#3', 'db2' (c4 = 60)."""
    value = value.strip()
    try:
        return int(float(value))
    except ValueError:
        pass
    m = _NOTE_RE.match(value)
    if not m:
        raise ValueError(f"bad SFZ note {value!r}")
    pc = _NOTE_PC[m.group(1).lower()] + {"#": 1, "b": -1, "": 0}[m.group(2)]
    return (int(m.group(3)) + 1) * 12 + pc


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", text)


def _expand(path: Path, seen: frozenset[Path] = frozenset()) -> str:
    """Read an SFZ, inlining `#include` files and substituting `#define` variables."""
    text = path.read_text(encoding="utf-8", errors="replace")

    def include(m: re.Match) -> str:
        inc = (path.parent / m.group(1).replace("\\", "/")).resolve()
        if inc in seen or not inc.is_file():
            return ""
        return _expand(inc, seen | {path.resolve()})

    text = _INCLUDE_RE.sub(include, text)
    text = _strip_comments(text)
    defines = dict(_DEFINE_RE.findall(text))
    text = _DEFINE_RE.sub("", text)
    # longest names first so $KEY2 isn't eaten by $KEY
    for name in sorted(defines, key=len, reverse=True):
        text = text.replace(name, defines[name])
    return text


def _opcodes(segment: str) -> dict[str, str]:
    """`a=1 sample=my file.wav b=2` → {'a': '1', 'sample': 'my file.wav', 'b': '2'}.

    A value runs up to the next ` name=`, so paths with spaces need no quoting.
    """
    out: dict[str, str] = {}
    matches = list(_OPCODE_RE.finditer(segment))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(segment)
        name = _ALIASES.get(m.group(1), m.group(1))
        out[name] = segment[m.end() : end].strip()
    return out


def _resolve(base: Path, rel: str) -> Path | None:
    """Join `rel` onto `base`, matching each component case-insensitively if needed."""
    cur = base
    for part in [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]:
        if part == "..":
            cur = cur.parent
            continue
        nxt = cur / part
        if not nxt.exists():
            try:
                nxt = next((c for c in cur.iterdir() if c.name.lower() == part.lower()), None)
            except OSError:
                return None
            if nxt is None:
                return None
        cur = nxt
    return cur if cur.is_file() else None


def parse_sfz(path: Path) -> list[Zone]:
    """The instrument's zones, with `file` set to the resolved absolute WAV path."""
    path = Path(path)
    text = _expand(path)

    parts = _HEADER_RE.split(text)  # ['', 'region', ' opcodes ', 'group', ' ...']
    control: dict[str, str] = {}
    scopes: dict[str, dict[str, str]] = {"global": {}, "master": {}, "group": {}}
    regions: list[dict[str, str]] = []
    for i in range(1, len(parts), 2):
        header, body = parts[i].lower(), _opcodes(parts[i + 1])
        if header == "control":
            control.update(body)
        elif header == "global":
            scopes = {"global": body, "master": {}, "group": {}}
        elif header == "master":
            scopes["master"], scopes["group"] = body, {}
        elif header == "group":
            scopes["group"] = body
        elif header == "region":
            regions.append({**scopes["global"], **scopes["master"], **scopes["group"], **body})

    default_path = control.get("default_path", "").replace("\\", "/")
    base = path.parent
    sw_default = control.get("sw_default")

    zones: list[Zone] = []
    for n, r in enumerate(regions):
        sample = r.get("sample")
        if not sample or sample.startswith("*"):  # '*sine' etc. are generators, not files
            continue
        if r.get("trigger", "attack") == "release" or r.get("trigger") == "release_key":
            continue
        sw_last = r.get("sw_last")
        if sw_last is not None and (sw_default is None or _note(sw_last) != _note(sw_default)):
            continue

        file = _resolve(base, default_path + sample.replace("\\", "/"))
        if file is None:
            log.warning("%s: sample not found: %s", path.name, sample)
            continue

        try:
            lokey = _note(r.get("lokey", "0"))
            hikey = _note(r.get("hikey", "127"))
            if "key" in r:
                lokey = hikey = _note(r["key"])
            root = _note(r["pitch_keycenter"]) if "pitch_keycenter" in r else (_note(r["key"]) if "key" in r else None)
        except ValueError:
            continue
        if root is None:
            # SFZ's own default centre is 60; a single-key region is its own centre
            root = lokey if lokey == hikey else 60

        offset = int(float(r.get("offset", "0") or 0))
        end = int(float(r.get("end", "0") or 0))
        if end < 0:  # `end=-1` disables the region
            continue
        sample_stop = end + 1 if end > 0 else 0  # SFZ `end` is the last played frame (inclusive)

        loop_mode = r.get("loop_mode", "no_loop")
        loop_start = loop_stop = None
        if loop_mode in _LOOPING_MODES:
            if "loop_start" in r and "loop_end" in r:
                loop_start, loop_stop = int(float(r["loop_start"])), int(float(r["loop_end"]))
            else:
                embedded = read_loop_points(file)  # the file's own `smpl` loop
                if embedded is not None:
                    loop_start, loop_stop = embedded

        zones.append(
            Zone(
                file=str(file),
                root=root,
                vel_low=int(float(r.get("lovel", "0"))),
                vel_high=int(float(r.get("hivel", "127"))),
                track=float(r.get("pitch_keytrack", "100")) != 0.0,
                gain_db=float(r.get("volume", "0") or 0),
                sample_start=offset,
                sample_stop=sample_stop,
                reverse=r.get("direction", "forward") == "reverse",
                loop_start=loop_start,
                loop_stop=loop_stop,
                loop_fade_s=float(r.get("loop_crossfade", "0") or 0) if loop_start is not None else 0.0,
                group=int(float(r.get("seq_position", "0") or 0)) * 1000 + n,
                key_low=lokey,
                key_high=hikey,
                pan=max(-1.0, min(1.0, float(r.get("pan", "0") or 0) / 100.0)),
                tune_cents=float(r.get("tune", "0") or 0) + 100.0 * float(r.get("transpose", "0") or 0),
            )
        )
    return zones

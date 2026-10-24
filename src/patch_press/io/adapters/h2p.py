"""Read how a u-he `.h2p` preset is meant to be *played* (voice mode, glide, what the mod
wheel and aftertouch do) — the part of a preset a capture can't hear.

An `.h2p` is plain text: a `/*@Meta ... */` header of quoted fields (`Usage:` is the
author's own note on the controllers, e.g. `'AT = Vibrato\\r\\nMW = Filter'`), then
`#cm=<module>` sections of `key=value` lines. Voice settings live in `#cm=VCC`, with the
same numbering in Diva, Zebra2 and ZebraHZ for the modes that matter here — checked
against each preset's own `Features:` tags (1 and 2 are tagged `Mono`, 0 `Poly`):

    Mode=0 poly, 1 mono (retrigger), 2 legato, 3 duo (Diva) / arp (Zebra), 4 a second
    poly mode (Diva)

so 0/1/2 map straight across and anything else falls back to poly, the closest thing a
sampler voice can do. `Porta` is the glide knob, 0..100 in all three.
"""

from __future__ import annotations

import re
from pathlib import Path

from ...config.schema import PerformanceConfig

_MODES = {0: "poly", 1: "mono", 2: "legato"}

# `Usage:` lines are free text ("MW = cutoff down", "AT = tone / vibrato"), so they are
# reduced to the few intents an exporter can act on. Order matters: a line naming
# vibrato is a vibrato line whatever else it says, and "cutoff down" must be read as
# darken before the bare "cutoff" of brighten sees it.
_VIBRATO = re.compile(r"vibrato|waver|warble")
_DARKEN = re.compile(r"cutoff down|close filter|darken|soften|softer|dampen|warmer")
_BRIGHTEN = re.compile(r"cutoff|filter|bright|presence")
_NOT_BRIGHTEN = re.compile(r"filter (fm|mod|lfo|env)")
_USAGE_LINE = re.compile(r"^\s*(MW|AT)\s*=\s*(.+?)\s*$", re.IGNORECASE)


def _section(text: str, name: str) -> dict[str, str]:
    """`key=value` pairs of one `#cm=<name>` section (first occurrence)."""
    out: dict[str, str] = {}
    inside = False
    for line in text.splitlines():
        if line.startswith("#cm="):
            if inside:
                break
            inside = line[4:].strip() == name
            continue
        if inside and "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out.setdefault(key.strip(), value.strip())
    return out


def _usage(text: str) -> str:
    """The `Usage:` meta field's quoted value, or '' when the preset has none."""
    m = re.search(r"^Usage:\s*\n'(.*?)'\s*$", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def _intent(value: str, controller: str) -> str | None:
    v = value.lower()
    if _VIBRATO.search(v):
        return "vibrato"
    if controller == "MW":
        if _DARKEN.search(v):
            return "darken"
        if _BRIGHTEN.search(v) and not _NOT_BRIGHTEN.search(v):
            return "brighten"
    return None


def read_performance(path: Path) -> PerformanceConfig | None:
    """The playing settings of a u-he preset, or None when `path` isn't a readable `.h2p`."""
    if path.suffix.lower() != ".h2p":
        return None
    try:
        text = path.read_text(encoding="latin-1")
    except OSError:
        return None

    perf = PerformanceConfig()
    vcc = _section(text, "VCC")
    try:
        perf.voice_mode = _MODES.get(int(vcc.get("Mode", "0")), "poly")
    except ValueError:
        pass
    try:
        perf.glide = round(max(0.0, min(100.0, float(vcc.get("Porta", "0")))) / 100.0, 3)
    except ValueError:
        pass

    for line in re.split(r"\\r|\\n|\r|\n", _usage(text)):
        m = _USAGE_LINE.match(line)
        if not m:
            continue
        controller = m.group(1).upper()
        intent = _intent(m.group(2), controller)
        if controller == "MW" and perf.mod_wheel is None:
            perf.mod_wheel = intent
        elif controller == "AT" and perf.aftertouch is None:
            perf.aftertouch = intent
    return perf


def performance_yaml(perf: PerformanceConfig | None) -> str:
    """The config's `performance:` block, or '' when nothing differs from the defaults."""
    if perf is None or perf == PerformanceConfig():
        return ""
    lines = ["performance:\n", "  # from the preset file: VCC Mode/Porta and the author's Usage note\n"]
    if perf.voice_mode != "poly":
        lines.append(f"  voice_mode: {perf.voice_mode}\n")
    if perf.glide:
        lines.append(f"  glide: {perf.glide}\n")
    if perf.mod_wheel:
        lines.append(f"  mod_wheel: {perf.mod_wheel}\n")
    if perf.aftertouch:
        lines.append(f"  aftertouch: {perf.aftertouch}\n")
    return "".join(lines)

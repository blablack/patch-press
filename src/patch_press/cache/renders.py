"""Shared render cache: one plugin note, rendered once, reused by every project.

Lives in a user-level directory (not in any repo) so patch-press, prepare-sd-cards and
patch-ear all hit the same entries. The key names *what was rendered*, never who asked:
plugin identity + build, preset state hash, note, velocity, note-off time, tempo, sample
rate and the adapter's payload extras.

Storage is `diskcache` (SQLite index + blob files, LRU eviction, multi-process safe).

Superset reuse: the render duration is NOT in the key. An entry stores the audio for the
longest duration rendered so far; a request for a shorter one is served by cutting the
stored buffer. That is exact for an offline render (the first N samples don't depend on
how long the render runs) provided note-off is in the key, which it is — a note released
at 2 s can't be derived from one held for 4 s.

Env: PATCH_PRESS_CACHE=0 disables; PATCH_PRESS_CACHE_DIR overrides the location
(default $XDG_CACHE_HOME/patch-press, i.e. ~/.cache/patch-press); PATCH_PRESS_CACHE_GB is the
one size cap for the whole cache (renders + analysis + source store, default 150), enforced by
`cache.evict.trim()` at the end of a run, least recently used first. diskcache's own culling is
switched off (size_limit huge) so one layer can't evict on its own behind the shared cap.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import diskcache
import numpy as np

# Bump when render semantics change (payload shape, lead-in, anything that alters audio
# for an unchanged key). Old entries then simply stop matching.
RENDER_CACHE_VERSION = 1


def enabled() -> bool:
    return os.environ.get("PATCH_PRESS_CACHE", "1") not in ("0", "false", "off", "")


def cache_root() -> Path:
    override = os.environ.get("PATCH_PRESS_CACHE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "patch-press"


def cap_bytes() -> int:
    """The one size cap for the whole cache dir (renders + analysis + source store)."""
    return int(float(os.environ.get("PATCH_PRESS_CACHE_GB", "150")) * 1e9)


# diskcache's own size_limit: never reached, so it never culls by itself. The shared cap is
# applied across every layer by cache.evict.trim().
NO_SELF_CULL = 2**62


def _render_dir() -> Path:
    return cache_root() / "render-dc"


def _plugin_identity(plugin: str) -> list:
    """Path-independent-ish build identity: size + mtime of the plugin binary(ies).

    A plugin update changes the audio, so it must change the key. A VST3 bundle is a
    directory; stat every file in it (a handful).
    """
    p = Path(plugin)
    try:
        files = [p] if p.is_file() else sorted(f for f in p.rglob("*") if f.is_file())
        return [[f.name, f.stat().st_size, f.stat().st_mtime_ns] for f in files]
    except OSError:
        return []


def render_key(payload: dict, hold_s: float) -> str:
    """Hash of everything that determines the audio, excluding duration and output path."""
    ident = {k: v for k, v in payload.items() if k not in ("output", "duration", "raw_state", "midi")}
    midi = [(m["type"], m["pitch"], m["vel"], round(m["time"], 6)) for m in payload["midi"]]
    material = {
        "v": RENDER_CACHE_VERSION,
        "payload": ident,
        "build": _plugin_identity(str(payload["plugin"])),
        "state": hashlib.sha256((payload.get("raw_state") or "").encode()).hexdigest(),
        "midi": midi,
    }
    if payload.get("fx_chain"):
        # Marker for amped renders only, so dry keys are untouched. Bumped to 2 when patch-render
        # started honouring `fx_chain` in render_clap(): an older wheel ignored it silently and
        # stored dry audio under these keys.
        material["fx_v"] = 2
    blob = json.dumps(material, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:32]


_caches: dict[str, diskcache.Cache] = {}


def _cache() -> diskcache.Cache:
    """The process-wide diskcache.Cache for renders (SQLite index + blob files; safe across
    threads, forks and concurrent processes). The LRU policy keeps access_time current on
    every hit; cache.evict.trim() reads it to evict across the whole cache."""
    d = str(_render_dir())
    c = _caches.get(d)
    if c is None:
        c = _caches[d] = diskcache.Cache(d, size_limit=NO_SELF_CULL, eviction_policy="least-recently-used")
    return c


def count(c: diskcache.Cache, name: str) -> None:
    """Bump a hit/miss counter kept inside the cache itself, so it spans processes
    (read by tools/cache_stats.py). Never lets bookkeeping break a render."""
    try:
        c.incr(f"~{name}")
    except Exception:
        pass


def _log_miss(key: str, why: str, duration_s: float, stored_s: float | None, info: dict | None) -> None:
    """Debug aid: PATCH_PRESS_CACHE_LOG=<file> appends one JSON line per miss (why it missed)."""
    path = os.environ.get("PATCH_PRESS_CACHE_LOG")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(json.dumps({"key": key, "why": why, "want_s": duration_s, "stored_s": stored_s, **(info or {})}) + "\n")
    except OSError:
        pass


def lookup(key: str, duration_s: float, sample_rate: int, info: dict | None = None) -> np.ndarray | None:
    """Stored audio (2, N) covering `duration_s`, cut to exactly that length, or None."""
    if not enabled():
        return None
    want_frames = int(round(duration_s * sample_rate))
    try:
        c = _cache()
        entry = c.get(key)
    except Exception:  # a broken cache must never break a render
        return None
    if entry is not None:
        stored_s, data = entry
        if stored_s >= duration_s - 1e-6 and data.ndim == 2 and data.shape[1] >= want_frames - 2:  # the renderer can come up a frame short (float truncation); a fresh render would too
            count(c, "hits")
            return data[:, :want_frames].copy()
        _log_miss(key, "stored entry too short", duration_s, stored_s, info)
    else:
        _log_miss(key, "no entry", duration_s, None, info)
    count(c, "misses")
    return None


def store(key: str, duration_s: float, data: np.ndarray) -> None:
    """Keep the longest render per key: a shorter one is cut from it at lookup."""
    if not enabled():
        return
    try:
        c = _cache()
        with c.transact():
            old = c.get(key)
            if old is None or old[0] < duration_s:
                c.set(key, (duration_s, np.ascontiguousarray(data, dtype=np.float32)))
    except Exception:
        return

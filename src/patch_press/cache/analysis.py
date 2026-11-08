"""Per-sample analysis cache: trim / envelope / pitch / loop detection, once per sample.

`analysis.pipeline._analyze_one` is a pure function of one sample and the AnalysisConfig;
it never looks at other samples or at the output format (the format only decides *which*
samples exist, and whether the loop crossfade is baked later, outside this function). So
Bento, Deluge and any other run that analyses the same audio with the same settings can
share the result. Deluge's smaller sample set is simply a subset of Bento's entries.

Only the small result is stored — trim bounds, loop points, the analysis dict, metadata,
note-off — never the audio: the trimmed audio is a slice of the input, rebuilt on a hit.

The key covers the audio bytes + sample fields + every AnalysisConfig field + a hash of
the `analysis/` source files and the numeric libraries, so editing the analyzers (loop
detection, envelope, ...) invalidates old entries by itself; no manual version bump, no
stale results while tuning. Storage is diskcache under <cache root>/analysis-dc.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

import diskcache
import numpy as np

from .renders import cache_root, count
from .renders import enabled as _cache_enabled


def enabled() -> bool:
    """PATCH_PRESS_CACHE=0 disables every cache; PATCH_PRESS_ANALYSIS_CACHE=0 only this one."""
    return _cache_enabled() and os.environ.get("PATCH_PRESS_ANALYSIS_CACHE", "1") not in ("0", "false", "off", "")

_caches: dict[str, diskcache.Cache] = {}


@lru_cache(maxsize=1)
def code_version() -> str:
    """Hash of everything that can change an analysis result without changing its inputs."""
    h = hashlib.sha256()
    here = Path(__file__).resolve().parent.parent / "analysis"
    for f in sorted(here.glob("*.py")):
        h.update(f.name.encode())
        h.update(f.read_bytes())
    for mod in ("numpy", "scipy", "librosa", "soxr"):
        try:
            h.update(f"{mod}={__import__(mod).__version__}".encode())
        except Exception:
            h.update(f"{mod}=?".encode())
    return h.hexdigest()[:16]


def _cache() -> diskcache.Cache:
    d = str(cache_root() / "analysis-dc")
    c = _caches.get(d)
    if c is None:
        cap = int(float(os.environ.get("PATCH_PRESS_ANALYSIS_GB", "5")) * 1e9)
        c = _caches[d] = diskcache.Cache(d, size_limit=cap, eviction_policy="least-recently-used")
    return c


def make_key(sample, config) -> str:
    """Sample = patch_press.model.sample.Sample; config = the AnalysisConfig actually used."""
    h = hashlib.sha256()
    h.update(code_version().encode())
    h.update(np.ascontiguousarray(sample.audio.data).tobytes())
    meta = {
        "sr": sample.audio.sample_rate,
        "shape": list(sample.audio.data.shape),
        "note": sample.note,
        "vel": sample.velocity,
        "rr": sample.round_robin,
        "loop": sample.loop_points,
        "analysis": sample.analysis,
        "metadata": sample.metadata,
        "note_off": sample.note_off,
        "config": dataclasses.asdict(config),
    }
    h.update(json.dumps(meta, sort_keys=True, default=repr).encode())
    return h.hexdigest()


def lookup(key: str) -> dict | None:
    if not enabled():
        return None
    try:
        c = _cache()
        hit = c.get(key)
    except Exception:  # a broken cache must never break an analysis
        return None
    count(c, "hits" if hit is not None else "misses")
    return hit


def store(key: str, record: dict) -> None:
    if not enabled():
        return
    try:
        _cache().set(key, record)
    except Exception:
        return

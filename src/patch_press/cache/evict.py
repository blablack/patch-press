"""One size cap for the whole cache dir: renders, analysis results and the source store.

`trim()` ranks every evictable item by when it was last used and removes the oldest until the
cache is under PATCH_PRESS_CACHE_GB (renders.cap_bytes()). "Used" means:

- render / analysis entry: its diskcache access_time (the LRU policy updates it on every hit);
- source-store entry: the last place_* call that asked for it (sources.index()), which every
  fetch makes, "already there" included. An entry the index has never seen (fetched before the
  index existed) is recorded as used now, so the first trim never mistakes it for old.

A source entry is a file that other places hardlink to. Removing it frees disk only once every
link is gone, so:

- links under library/ go with the entry (that tree is a view of the store; the next fetch puts
  them back);
- a link anywhere else (a device staging dir, the card copy) means a device is still using the
  file: the entry is PINNED, kept and reported, and frees nothing until that dir is deleted
  (`just bento-done` / `just deluge-done`).

Analysis entries are tiny and live inside its SQLite file, so they are counted but never picked.
Run trim between runs (the end of a build or a patch-ear analysis), not while another process is
fetching: an entry removed under a running fetch makes that fetch fail.
"""

from __future__ import annotations

import fcntl
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import analysis, renders, sources


@dataclass
class TrimReport:
    cap: int
    before: int = 0
    after: int = 0
    freed: dict[str, int] = field(default_factory=lambda: {"render": 0, "source": 0})
    removed: dict[str, int] = field(default_factory=lambda: {"render": 0, "source": 0})
    pinned_bytes: int = 0
    pinned_entries: int = 0
    adopted: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        gb = lambda b: f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"
        verb = "would remove" if self.dry_run else "removed"
        out = [f"cache {gb(self.before)} -> {gb(self.after)} (cap {gb(self.cap)})"]
        for layer in ("render", "source"):
            if self.removed[layer]:
                out.append(
                    f"  {verb} {self.removed[layer]} {layer} entr{'y' if self.removed[layer] == 1 else 'ies'}, {gb(self.freed[layer])}"
                )
        if self.pinned_entries:
            out.append(f"  kept {self.pinned_entries} source entries ({gb(self.pinned_bytes)}) still linked from a staging dir")
        if self.adopted:
            out.append(f"  indexed {self.adopted} source entries fetched before the index existed (as used now)")
        if self.after > self.cap:
            out.append("  still over the cap: the rest is pinned by staging dirs (delete one with `just <device>-done`)")
        return out


def _source_entries() -> list[tuple[str, os.stat_result]]:
    store = sources._store()
    out = []
    if not store.is_dir():
        return out
    for sub in os.scandir(store):
        if not sub.is_dir(follow_symlinks=False):
            continue
        for e in os.scandir(sub.path):
            if e.is_file(follow_symlinks=False) and not e.name.endswith(".part"):
                out.append((e.name, e.stat(follow_symlinks=False)))
    return out


def _library_links(inodes: set[tuple[int, int]]) -> dict[tuple[int, int], list[Path]]:
    """Every path under library/ that is a hardlink to one of `inodes`."""
    found: dict[tuple[int, int], list[Path]] = {}
    root = sources.library_root()
    if not root.is_dir():
        return found
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    ident = (st.st_dev, st.st_ino)
                    if ident in inodes:
                        found.setdefault(ident, []).append(Path(e.path))
    return found


def _prune_empty_dirs(paths: list[Path]) -> None:
    root = sources.library_root()
    for d in sorted({p.parent for p in paths}, key=lambda p: len(p.parts), reverse=True):
        while d != root and root in d.parents:
            try:
                d.rmdir()
            except OSError:
                break
            d = d.parent


def usage() -> dict[str, int]:
    """Bytes per layer, each file counted once."""
    return {
        "render": renders._cache().volume() if (renders.cache_root() / "render-dc").is_dir() else 0,
        "analysis": analysis._cache().volume() if (renders.cache_root() / "analysis-dc").is_dir() else 0,
        "source": sum(st.st_size for _, st in _source_entries()),
    }


def trim(cap: int | None = None, dry_run: bool = False, log: Callable[[str], None] | None = None) -> TrimReport:
    """Evict least-recently-used renders and source entries until the cache is under `cap`."""
    cap = renders.cap_bytes() if cap is None else cap
    report = TrimReport(cap=cap, dry_run=dry_run)
    root = renders.cache_root()
    if not root.is_dir():
        return report
    with open(root / ".trim.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        report = _trim_locked(report, dry_run)
    if log is not None:
        for line in report.lines():
            log(line)
    return report


def _trim_locked(report: TrimReport, dry_run: bool) -> TrimReport:
    now = time.time()
    src = _source_entries()
    idx = sources.index()
    used = dict(idx.execute("SELECT key, last_used FROM entries"))
    missing = [(k, st.st_size) for k, st in src if k not in used]
    if missing:
        report.adopted = len(missing)
        if not dry_run:
            with idx:  # one transaction
                idx.executemany(
                    "INSERT OR IGNORE INTO entries(key, size, origin, last_used) VALUES (?, ?, 'unknown (fetched before the index)', ?)",
                    [(k, size, now) for k, size in missing],
                )
        used.update((k, now) for k, _ in missing)

    sizes = usage()
    report.before = report.after = sum(sizes.values())
    if report.after <= report.cap:
        return report

    # (last_used, layer, key, size, stat) — one ranking across layers.
    items: list[tuple[float, str, str, int, os.stat_result | None]] = [(used[k], "source", k, st.st_size, st) for k, st in src]
    rdb = renders.cache_root() / "render-dc" / "cache.db"
    if rdb.is_file():
        conn = sqlite3.connect(f"file:{rdb}?mode=ro", uri=True, timeout=60)
        try:
            items += [
                (t, "render", k, size, None)
                for k, t, size in conn.execute("SELECT key, access_time, size FROM Cache WHERE raw = 1 AND size > 0")
            ]
        finally:
            conn.close()
    items.sort(key=lambda it: it[0])

    lib_links: dict[tuple[int, int], list[Path]] | None = None
    removed_links: list[Path] = []
    rcache = renders._cache()
    for _, layer, key, size, st in items:
        if report.after <= report.cap:
            break
        if layer == "render":
            if dry_run or rcache.delete(key):
                report.removed["render"] += 1
                report.freed["render"] += size
                report.after -= size
            continue
        assert st is not None  # every source item carries its stat
        if lib_links is None:  # one walk of library/, only once something has to go
            lib_links = _library_links({(s.st_dev, s.st_ino) for _, s in src})
        links = lib_links.get((st.st_dev, st.st_ino), [])
        if st.st_nlink - 1 - len(links) > 0:
            report.pinned_entries += 1
            report.pinned_bytes += size
            continue
        if not dry_run:
            for p in links:
                p.unlink(missing_ok=True)
            sources.entry_path(key).unlink(missing_ok=True)
            idx.execute("DELETE FROM entries WHERE key = ?", (key,))
            removed_links += links
        report.removed["source"] += 1
        report.freed["source"] += size
        report.after -= size
    if removed_links:
        _prune_empty_dirs(removed_links)
    return report

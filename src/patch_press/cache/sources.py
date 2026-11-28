"""Shared source store: a library file is fetched from the NAS (or a zip) once, ever.

`place_file` / `place_stream` put a source's bytes at `dest` as a HARDLINK to a store entry
under <cache root>/source/, so a second consumer asking for the same source (Deluge after
Bento, patch-ear after either, a second dest for the same file) costs no NAS traffic and no
extra disk. Same filesystem is required for a hardlink; if it isn't, this falls back to a
plain copy, so behaviour never gets worse than before.

Identity is the source's own stat (path, size, mtime) — or for a zip member, the zip's stat
plus the member name and CRC — never the destination. An existing `dest` of the right size
is adopted into the store (linked, not copied) or, if the store already holds the content
under a different inode, atomically replaced by a link: that is how a rerun reclaims
duplicate copies. Size is trusted as "same content", exactly as fetch.py always did.

Store entries and their links are made read-only (0444): anything that writes a fetched file
in place fails loudly instead of silently rewriting every project's copy. Unlinking (clean_up,
rmtree) is unaffected.

Every place_* call records the entry in <store>/index.db (key, size, origin, last_used),
"already there" included: that is the use a device build or a patch-ear fetch leaves behind,
and what cache.evict.trim() ranks by. An entry evicted there is re-fetched from its origin
the next time anyone asks for it.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .renders import cache_root, enabled

SOURCE_CACHE_VERSION = 1


def _store() -> Path:
    return cache_root() / "source"


def library_root() -> Path:
    """Where the browsable library trees live (the old prepare-sd-cards `input/`).

    The source store holds hashed blobs; scans, configs and the stagers need real
    folders with real names, so each fetched/staged library is a tree of hardlinks to
    store entries under <cache root>/library/<name>/. It is part of the cache, not of
    any repo: every project that fetches or stages a library puts it here, and
    PATCH_PRESS_CACHE_DIR moves the whole thing. Same filesystem as the store (hardlinks).
    """
    return cache_root() / "library"


def _key(*parts: object) -> str:
    h = hashlib.sha256(repr((SOURCE_CACHE_VERSION, *parts)).encode())
    return h.hexdigest()[:40]


_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    key TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    origin TEXT NOT NULL,       -- what the bytes are: a NAS path, or <zip>::<member>
    last_used REAL NOT NULL     -- unix time of the last place_* that asked for it
)"""
_index_local = threading.local()  # one connection per thread (sqlite3 objects are bound to their thread)
_index_warned = False


def index() -> sqlite3.Connection:
    """This thread's connection to <store>/index.db (re-opened after a fork or a cache-dir change)."""
    path = str(_store() / "index.db")
    held = getattr(_index_local, "conn", None)
    if held is None or held[0] != os.getpid() or held[1] != path:
        _store().mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=120, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(_INDEX_SCHEMA)
        held = _index_local.conn = (os.getpid(), path, conn)
    return held[2]


def _origin(ident: tuple) -> str:
    if ident[0] == "file":
        return str(ident[1])
    if ident[0] == "zip":
        return f"{ident[1]}::{ident[4]}"
    return repr(ident)


def _touch(key: str, size: int, origin: str) -> None:
    """Record a use. Bookkeeping never breaks a fetch: an entry missing from the index is
    picked up by trim() as if used at that moment."""
    global _index_warned
    try:
        index().execute(
            "INSERT INTO entries(key, size, origin, last_used) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET size=excluded.size, origin=excluded.origin, last_used=excluded.last_used",
            (key, size, origin, time.time()),
        )
    except sqlite3.Error as e:
        if not _index_warned:
            _index_warned = True
            print(
                f"WARNING: source-store index not updated ({e}); eviction will treat these entries as just used",
                file=sys.stderr,
            )


def _file_ident(p: Path) -> tuple:
    st = p.stat()
    return ("file", str(p.resolve()), st.st_size, st.st_mtime_ns)


def _link_atomic(entry: Path, dest: Path) -> None:
    """Make `dest` a hardlink to `entry`, replacing whatever is there, never half-written."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.lnk{os.getpid()}")
    tmp.unlink(missing_ok=True)
    os.link(entry, tmp)
    os.replace(tmp, dest)


def entry_path(key: str) -> Path:
    return _store() / key[:2] / key


def _place(key: str, dest: Path, size: int, fill: Callable[[Path], None], origin: str) -> str:
    """Records the use, then places. Returns 'skipped' (dest already was this content),
    'linked' (store hit) or 'written' (fetched into the store now)."""
    result, in_store = _place_bytes(key, dest, size, fill)
    if in_store:
        _touch(key, size, origin)
    return result


def _place_bytes(key: str, dest: Path, size: int, fill: Callable[[Path], None]) -> tuple[str, bool]:
    """(result, whether the bytes live in the store). Not in the store = the plain-copy fallback."""
    entry = entry_path(key)
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        have = entry.is_file() and entry.stat().st_size == size
        dest_ok = dest.is_file() and dest.stat().st_size == size
        if have:
            if dest_ok and os.path.samefile(dest, entry):
                return "skipped", True
            _link_atomic(entry, dest)
            return ("skipped" if dest_ok else "linked"), True
        if dest_ok:  # adopt what is already on disk: link it into the store, copy nothing
            os.link(dest, entry)
            os.chmod(entry, 0o444)
            return "skipped", True
        fd, tmp = tempfile.mkstemp(dir=entry.parent, suffix=".part")
        os.close(fd)
        tmp_path = Path(tmp)
        try:
            fill(tmp_path)
            os.chmod(tmp_path, 0o444)
            os.replace(tmp_path, entry)
        finally:
            tmp_path.unlink(missing_ok=True)
        _link_atomic(entry, dest)
        return "written", True
    except OSError as e:
        # No hardlink possible here (other filesystem, no permission): plain copy.
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EACCES, errno.ENOTSUP):
            raise
        if dest.is_file() and dest.stat().st_size == size:
            return "skipped", False
        dest.parent.mkdir(parents=True, exist_ok=True)
        fill(dest)
        return "written", False


def place_file(src: Path, dest: Path) -> str:
    """Put the bytes of the file `src` at `dest` via the store. See module docstring."""
    if not enabled():
        raise RuntimeError("source store disabled")
    ident = _file_ident(src)
    return _place(_key(*ident), dest, src.stat().st_size, lambda out: shutil.copy2(src, out), _origin(ident))


def place_stream(ident: tuple, size: int, dest: Path, write_to: Callable[[Path], None]) -> str:
    """Same, for bytes that aren't a plain file: `ident` is whatever uniquely names them
    (for a zip member: zip path/size/mtime + member name + CRC), `write_to(path)` produces them."""
    if not enabled():
        raise RuntimeError("source store disabled")
    return _place(_key(*ident), dest, size, write_to, _origin(ident))


def zip_member_ident(zip_path: Path, info) -> tuple:
    st = zip_path.stat()
    return ("zip", str(zip_path.resolve()), st.st_size, st.st_mtime_ns, info.filename, info.CRC, info.file_size)

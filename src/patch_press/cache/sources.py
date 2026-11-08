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
rmtree) is unaffected. No eviction: an entry only costs disk until its last link goes.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import tempfile
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


def _place(key: str, dest: Path, size: int, fill: Callable[[Path], None]) -> str:
    """Core. Returns 'skipped' (dest already was this content), 'linked' (store hit),
    or 'written' (fetched into the store now)."""
    entry = _store() / key[:2] / key
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        have = entry.is_file() and entry.stat().st_size == size
        dest_ok = dest.is_file() and dest.stat().st_size == size
        if have:
            if dest_ok and os.path.samefile(dest, entry):
                return "skipped"
            _link_atomic(entry, dest)
            return "skipped" if dest_ok else "linked"
        if dest_ok:  # adopt what is already on disk: link it into the store, copy nothing
            os.link(dest, entry)
            os.chmod(entry, 0o444)
            return "skipped"
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
        return "written"
    except OSError as e:
        # No hardlink possible here (other filesystem, no permission): plain copy.
        if e.errno not in (errno.EXDEV, errno.EPERM, errno.EMLINK, errno.EACCES, errno.ENOTSUP):
            raise
        if dest.is_file() and dest.stat().st_size == size:
            return "skipped"
        dest.parent.mkdir(parents=True, exist_ok=True)
        fill(dest)
        return "written"


def place_file(src: Path, dest: Path) -> str:
    """Put the bytes of the file `src` at `dest` via the store. See module docstring."""
    if not enabled():
        raise RuntimeError("source store disabled")
    return _place(_key(*_file_ident(src)), dest, src.stat().st_size, lambda out: shutil.copy2(src, out))


def place_stream(ident: tuple, size: int, dest: Path, write_to: Callable[[Path], None]) -> str:
    """Same, for bytes that aren't a plain file: `ident` is whatever uniquely names them
    (for a zip member: zip path/size/mtime + member name + CRC), `write_to(path)` produces them."""
    if not enabled():
        raise RuntimeError("source store disabled")
    return _place(_key(*ident), dest, size, write_to)


def zip_member_ident(zip_path: Path, info) -> tuple:
    st = zip_path.stat()
    return ("zip", str(zip_path.resolve()), st.st_size, st.st_mtime_ns, info.filename, info.CRC, info.file_size)

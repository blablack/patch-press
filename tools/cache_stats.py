"""Report on the shared patch-press cache: entries, size, hit/miss counts per layer.

    uv run python tools/cache_stats.py            # current numbers
    uv run python tools/cache_stats.py --reset    # zero the hit/miss counters (before a run you want to measure)
    uv run python tools/cache_stats.py --trim [--dry-run] [--cap-gb N]   # evict least recently used to the cap

Run it before and after a build / patch-ear run: on a repeat run the misses should not grow.
Counters are shared by every process using the cache dir (PATCH_PRESS_CACHE_DIR or ~/.cache/patch-press).
"""

import argparse
import sys

import diskcache

from patch_press.cache import evict
from patch_press.cache.renders import cache_root, cap_bytes


def layer(name: str, reset: bool) -> None:
    d = cache_root() / name
    if not d.is_dir():
        print(f"{name:12} (not created yet)")
        return
    c = diskcache.Cache(str(d))
    hits, misses = c.get("~hits", 0), c.get("~misses", 0)
    total = hits + misses
    rate = f"{100 * hits / total:.0f}%" if total else "-"
    n = len(c) - sum(1 for k in ("~hits", "~misses") if k in c)
    print(f"{name:12} {n:7d} entries  {c.volume() / 1e9:7.2f} GB   hits {hits:7d}  misses {misses:7d}  hit rate {rate}")
    if reset:
        c.delete("~hits")
        c.delete("~misses")
    c.close()


def sources() -> None:
    d = cache_root() / "source"
    if not d.is_dir():
        print("source       (not created yet)")
        return
    n = size = single = 0
    for p in d.rglob("*"):
        if p.is_file():
            st = p.stat()
            n += 1
            size += st.st_size
            single += st.st_nlink == 1
    print(f"{'source':12} {n:7d} files    {size / 1e9:7.2f} GB   not linked from anywhere else: {single}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="zero the hit/miss counters after printing")
    ap.add_argument("--trim", action="store_true", help="evict least recently used renders/sources down to the cap")
    ap.add_argument("--dry-run", action="store_true", help="with --trim: report what would go, remove nothing")
    ap.add_argument("--cap-gb", type=float, help="with --trim: cap for this run (default PATCH_PRESS_CACHE_GB, else 150)")
    args = ap.parse_args()
    print(f"cache root: {cache_root()}   cap: {cap_bytes() / 1e9:.0f} GB (PATCH_PRESS_CACHE_GB)")
    for name in ("render-dc", "analysis-dc"):
        layer(name, args.reset)
    sources()
    if args.trim:
        cap = int(args.cap_gb * 1e9) if args.cap_gb is not None else None
        evict.trim(cap=cap, dry_run=args.dry_run, log=print)


if __name__ == "__main__":
    sys.exit(main())

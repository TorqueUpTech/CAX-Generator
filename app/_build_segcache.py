"""Pre-build segment_ranges.json from every GM bin available.

For each controller subfolder under GM BIN, resolve each bin's segment ranges via
the bundled controller XML and cache them keyed by the OS id parsed from the
filename. Run once to ship a self-contained cache; safe to re-run.
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import segmap

OSID = re.compile(r"\d{8}")


def os_ids(name: str):
    return OSID.findall(name)


def main():
    if not os.path.isdir(segmap.BIN_DIR):
        print("GM BIN folder not found:", segmap.BIN_DIR)
        return
    built = 0
    skipped = []
    for ctrl in sorted(os.listdir(segmap.BIN_DIR)):
        cdir = os.path.join(segmap.BIN_DIR, ctrl)
        if not os.path.isdir(cdir):
            continue
        if not segmap.segment_pointers(ctrl):
            continue  # no segment definition for this controller
        for dp, _dn, files in os.walk(cdir):
            for fn in files:
                if not fn.lower().endswith(".bin"):
                    continue
                ids = os_ids(fn)
                if not ids:
                    continue
                binp = os.path.join(dp, fn)
                try:
                    segs = segmap.resolve_ranges(ctrl, binp)
                except Exception as e:
                    skipped.append(f"{fn}: {e}")
                    continue
                if not segs:
                    skipped.append(f"{fn}: no ranges resolved")
                    continue
                for oid in set(ids):
                    if segmap.cached_segments(oid) is None:
                        segmap.cache_segments(oid, ctrl, segs)
                        built += 1
                        print(f"  {ctrl} {oid}: " +
                              ", ".join(f"{s.name} {s.ranges[0][0]:X}-{s.ranges[0][1]:X}"
                                        for s in segs if s.ranges))
    print(f"\nCached {built} OS entries -> {segmap.CACHE_PATH}")
    cache = segmap._load_cache()
    print(f"Total OS entries in cache: {len(cache)}")
    print(f"Controllers: {sorted(set(v['controller'] for v in cache.values()))}")
    if skipped:
        print(f"Skipped {len(skipped)} bins (first few): {skipped[:5]}")


if __name__ == "__main__":
    main()

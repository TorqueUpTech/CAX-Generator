"""Consolidate the per-controller SegmentConfig XMLs into one bundled JSON
(`segment_pointers.json`) so the app doesn't carry 73 XML files. Stores exactly
what segmap needs: per controller, the segment names + bin pointer offsets.

    { "<controller>": [ ["<name>", [[offset, count], ...]], ... ], ... }
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import segmap

XMLDIR = segmap.XML_DIR   # rebuild from the UniversalPatcher Drive source
OUT = os.path.join(os.path.dirname(__file__), "segment_pointers.json")

out = {}
for fn in sorted(os.listdir(XMLDIR)):
    if not fn.lower().endswith(".xml") or fn.lower().startswith("segmentseek"):
        continue
    try:
        ptrs = segmap.parse_segment_pointers(os.path.join(XMLDIR, fn))
    except Exception as e:
        print(f"  skip {fn}: {e}")
        continue
    if ptrs:
        stem = os.path.splitext(fn)[0].lower()
        out[stem] = [[name, [[off, cnt] for off, cnt in plist]]
                     for name, plist in ptrs]

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(out, f, separators=(",", ":"), sort_keys=True)

print(f"Wrote {len(out)} controllers -> {OUT} "
      f"({os.path.getsize(OUT) / 1024:.0f} KB)")
print("sample T42:", out.get("t42"))

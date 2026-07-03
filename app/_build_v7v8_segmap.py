"""Learn the V7->V8 segment-number mapping per controller from EFILive's own
converted output (the authoritative source — V7toV8_Segments.ini does NOT match).

For each EFILive .cax8 in 'User Defined Cax8/<CTRL>/', find the matching V7 .cax,
match calibrations by ID, and vote on V7 VIEW.Seg -> V8 Calibration.Seg. Write the
majority mapping to app/v7v8_segmap.json (bundled, used by the converter).
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import caxformat as cf
import caxtree as ct
import cax8format as c8

V7DIR = r"C:\Program Files (x86)\EFILive\V7.5\Calibrations"
V8ROOT = os.path.join(os.path.expanduser("~"), r"Documents\EFILive\V8\User Defined Cax8")
OUT = os.path.join(os.path.dirname(__file__), "v7v8_segmap.json")


def base_id(fn):
    return re.sub(r"-\d+$", "", os.path.splitext(fn)[0])


votes = defaultdict(Counter)   # (controller, v7seg) -> Counter(v8seg)
pairs = 0
for dp, _dn, files in os.walk(V8ROOT):
    ctrl = os.path.basename(dp)
    for fn in files:
        if not fn.lower().endswith(".cax8"):
            continue
        v7p = os.path.join(V7DIR, base_id(fn) + ".cax")
        if not os.path.exists(v7p):
            continue
        try:
            v7 = cf.parse_file(v7p)
            v8 = c8.parse_file(os.path.join(dp, fn))
        except Exception:
            continue
        pairs += 1
        v7seg = {}
        for sec in v7.sections:
            cal = ct.cal_from_section(sec)
            if cal:
                v7seg[sec.name] = cal.seg
        for cid, crow in v8.calibration.items():
            if cid in v7seg and crow.get("Seg"):
                votes[(ctrl, v7seg[cid])][crow["Seg"]] += 1

# majority map
segmap = defaultdict(dict)
for (ctrl, v7s), c in votes.items():
    v8s, n = c.most_common(1)[0]
    total = sum(c.values())
    segmap[ctrl][v7s] = v8s
    if n != total:
        print(f"  note {ctrl} v7 seg {v7s}: {dict(c)} -> picked {v8s}")

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(segmap, f, indent=2, sort_keys=True)

print(f"\nLearned from {pairs} V7/V8 pairs -> {OUT}")
for ctrl in sorted(segmap):
    print(f"  {ctrl}: " + ", ".join(f"V7 {k}->V8 {v}" for k, v in sorted(segmap[ctrl].items())))

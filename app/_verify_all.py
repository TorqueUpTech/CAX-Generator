import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import verify

V7DIR = r"C:\Program Files (x86)\EFILive\V7.5\Calibrations"
V8DIR = r"C:\Users\david\Documents\EFILive\V8\User Defined Cax8\T42"
IDS = ["24231674", "24232637", "24235341", "24236195", "24238857"]

grand_dropped = 0
for cid in IDS:
    v7 = os.path.join(V7DIR, cid + ".cax")
    v8 = os.path.join(V8DIR, cid + ".cax8")
    rpt = verify.verify_paths(v7, v8)
    print("=" * 78)
    print(f"{cid}.cax  ->  {cid}.cax8")
    print("  " + rpt.summary())
    grand_dropped += len(rpt.dropped)
    if rpt.dropped:
        print(f"  DROPPED ({len(rpt.dropped)}):")
        for did, cap, cause in rpt.dropped:
            print(f"    - {did:10} {cap[:34]:34} | {cause}")
    if rpt.changed:
        # summarize change types
        from collections import Counter
        kinds = Counter(d.field for cd in rpt.changed for d in cd.diffs)
        print(f"  CHANGED fields across {len(rpt.changed)} cals: {dict(kinds)}")
        # show a couple examples
        for cd in rpt.changed[:3]:
            ex = ", ".join(f"{d.field}:{d.v7}->{d.v8}" for d in cd.diffs)
            print(f"    e.g. {cd.cal_id}: {ex}")

print("=" * 78)
print(f"TOTAL dropped across {len(IDS)} files: {grand_dropped}")

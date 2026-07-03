import os
import re
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import verify
import caxformat as cf
import caxtree as ct


def _v7ids(path):
    cax = cf.parse_file(path)
    return [c.id for c in (ct.cal_from_section(s) for s in cax.sections) if c]

V8ROOT = r"C:\Users\david\Documents\EFILive\V8\User Defined Cax8"
V7DIRS = [r"C:\Program Files (x86)\EFILive\V7.5\Calibrations",
          os.path.join(os.path.expanduser("~"), "Documents")]


def find_v7(calid):
    for d in V7DIRS:
        p = os.path.join(d, calid + ".cax")
        if os.path.exists(p):
            return p
    return None


def base_id(fn):
    stem = os.path.splitext(fn)[0]
    return re.sub(r"-\d+$", "", stem)  # strip "-1" alt-conversion suffix


pairs = []
for dirpath, _, files in os.walk(V8ROOT):
    for fn in files:
        if not fn.lower().endswith(".cax8") or fn == "template.cax8":
            continue
        if re.search(r"-\d+\.cax8$", fn, re.I):  # skip alternate conversions (e.g. -1)
            continue
        ctrl = os.path.basename(dirpath)
        v7 = find_v7(base_id(fn))
        if v7:
            pairs.append((ctrl, base_id(fn), v7, os.path.join(dirpath, fn)))

print(f"Matched {len(pairs)} V7/V8 pairs\n")

per_ctrl = defaultdict(lambda: [0, 0, 0])  # files, v7cals, dropped
change_kinds = Counter()
addr_changes = []
# track per-id survival to prove per-OS collision
drop_count = Counter()
keep_count = Counter()

for ctrl, cid, v7, v8 in sorted(pairs):
    try:
        r = verify.verify_paths(v7, v8)
    except Exception as e:
        print(f"  ERR {cid}: {e}")
        continue
    per_ctrl[ctrl][0] += 1
    per_ctrl[ctrl][1] += r.v7_count
    per_ctrl[ctrl][2] += len(r.dropped)
    for cd in r.changed:
        for d in cd.diffs:
            change_kinds[d.field] += 1
            if d.field == "Address":
                addr_changes.append((cid, cd.cal_id, d.v7, d.v8))
    dropped_ids = {d[0] for d in r.dropped}
    for did in dropped_ids:
        drop_count[did] += 1
    for did in (set(_v7ids(v7)) - dropped_ids):
        keep_count[did] += 1

print("Per-controller:")
print(f"  {'ctrl':6} {'files':>5} {'v7cals':>7} {'dropped':>7} {'drop%':>6}")
tot = [0, 0, 0]
for ctrl in sorted(per_ctrl):
    f, c, d = per_ctrl[ctrl]
    tot[0] += f; tot[1] += c; tot[2] += d
    print(f"  {ctrl:6} {f:5} {c:7} {d:7} {100*d/max(c,1):5.1f}%")
print(f"  {'TOTAL':6} {tot[0]:5} {tot[1]:7} {tot[2]:7} {100*tot[2]/max(tot[1],1):5.1f}%")

print("\nField-change pattern (count across all changed cals):")
for k, v in change_kinds.most_common():
    print(f"  {k:12} {v}")

print(f"\nAddress changes (relocations, not reformatting): {len(addr_changes)}")
for cid, calid, a, b in addr_changes[:15]:
    print(f"  {cid}/{calid}: {a} -> {b}")

# IDs that drop in some files but survive in others => per-OS collision, not structural
ambiguous = sorted([i for i in drop_count if keep_count.get(i, 0) > 0],
                   key=lambda i: -drop_count[i])
print(f"\nCal IDs that DROP in some OS but SURVIVE in others "
      f"(=> per-OS factory collision, not a structural defect): {len(ambiguous)}")
for i in ambiguous[:20]:
    print(f"  {i}: dropped in {drop_count[i]}, kept in {keep_count[i]}")

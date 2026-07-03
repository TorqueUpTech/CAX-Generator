"""Build a bundled OS-number -> controller table from EFILive's Cam folders, so
controller lookup works without EFILive installed (and without scanning folders
each time). Cam\\<CTRL>\\<OSID>.cam -> {osid: controller}."""
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

CAM = r"C:\Program Files (x86)\EFILive\V8\Cam"
OUT = os.path.join(os.path.dirname(__file__), "os_controllers.json")

table = {}
if os.path.isdir(CAM):
    for ctrl in sorted(os.listdir(CAM)):
        d = os.path.join(CAM, ctrl)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.lower().endswith(".cam"):
                osid = os.path.splitext(fn)[0]
                table[osid] = ctrl   # later controllers don't override; first wins

with open(OUT, "w", encoding="utf-8") as f:
    json.dump(table, f, indent=0, sort_keys=True)

from collections import Counter
print(f"Wrote {len(table)} OS->controller entries to {OUT}")
print("by controller:", dict(Counter(table.values())))

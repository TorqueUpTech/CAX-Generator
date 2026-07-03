import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import caxformat as cf
import segments as seg
import convert as cv

src = r"C:\Program Files (x86)\EFILive\V7.5\Calibrations\24236195.cax"
cax = cf.parse_file(src)

controllers = seg.parse_segments_ini()
print("controllers parsed:", len(controllers))
ctrl = seg.controller_for_cam("24236195")
print("controller for 24236195:", ctrl)
cmap = controllers.get(ctrl)
print("T42 mapping:", cmap.mapping if cmap else None)

provider = {"Version": "8.3.1.0", "Date": "2026.06.23", "Name": "CAX Editor",
            "UniqueID": "U01", "Description": "Converted from 24236195.cax"}
text, report = cv.convert(cax, ctrl, cmap, provider)

print("\n" + report.summary())
print("\nDROPPED (%d):" % len(report.dropped))
for c in report.dropped:
    print(f"  {c.cal_id} '{c.caption}' (seg {c.v7_seg}) — {c.reasons[0]}")
print("\nREVIEW (%d):" % len(report.review))
for c in report.review[:8]:
    print(f"  {c.cal_id} '{c.caption}' — {'; '.join(c.reasons)}")

# write the cax8 out for inspection
outp = os.path.join(os.path.dirname(__file__), "_out_24236195.cax8")
open(outp, "w", encoding="latin-1", newline="\r\n").write(text)
print("\nwrote", outp, "(%d bytes)" % len(text))
print("\n--- first 35 lines of output ---")
for ln in text.splitlines()[:35]:
    print(ln)

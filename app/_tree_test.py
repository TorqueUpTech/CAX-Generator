import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import caxformat as cf
import caxtree as ct

src = os.path.join(os.path.expanduser("~"), "Documents", "01250003.cax")
cax = cf.parse_file(src)
root = ct.build_tree(cax)


def show(b, depth=0):
    print("  " * depth + "[+] " + b.label)
    for c in b.children.values():
        show(c, depth + 1)
    for cal in b.cals:
        print("  " * (depth + 1) + f"{ct.DISPLAY_KIND.get('','')}leaf: {cal.caption} [{cal.id}] disp={cal.display} seg={cal.seg}")


show(root)

# Edit round-trip: change a VIEW caption and confirm serialize reflects it.
sec = cax.section("B9021")
v = sec.get("VIEW")
v.set_fields(["2", "Renamed Param", "Fuel", "Injectors", "1", "1", "0", "1"])
out = cf.serialize(cax)
print("EDIT REFLECTED:", "Renamed Param" in out)
print("rendered VIEW line:", [l for l in out.splitlines() if l.startswith("VIEW")])

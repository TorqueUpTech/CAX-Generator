import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))
import caxformat as cf
import caxtree as ct

src = r"C:\Program Files (x86)\EFILive\V7.5\Calibrations\24236195.cax"
orig = open(src, encoding="latin-1").read()
cax = cf.parse(orig, src)
out = cf.serialize(cax)

a = orig.replace("\r\n", "\n").rstrip("\n")
b = out.replace("\r\n", "\n").rstrip("\n")
print("sections:", len(cax.sections))
print("ROUND-TRIP IDENTICAL:", a == b)
if a != b:
    al, bl = a.split("\n"), b.split("\n")
    print("line counts:", len(al), len(bl))
    shown = 0
    for i, (x, y) in enumerate(zip(al, bl)):
        if x != y:
            print(f"  diff line {i}:")
            print("    orig:", repr(x))
            print("    out :", repr(y))
            shown += 1
            if shown >= 5:
                break

errs = cax.errors
print("validation errors:", len(errs))
for name, ln in errs[:15]:
    print("  [%s] %s -> %s" % (name, ln.key, ln.errors))

root = ct.build_tree(cax)
print("top-level branches:", [b.label for b in root.children.values()])

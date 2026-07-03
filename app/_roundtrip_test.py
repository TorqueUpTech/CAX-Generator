import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import caxformat as c

src = os.path.join(os.path.expanduser("~"), "Documents", "01250003.cax")
orig = open(src, encoding="latin-1").read()
cax = c.parse(orig, src)
out = c.serialize(cax)

a = orig.replace("\r\n", "\n").rstrip("\n")
b = out.replace("\r\n", "\n").rstrip("\n")

print("sections:", len(cax.sections), [s.name for s in cax.sections][:8])
b9 = cax.section("B9021")
print("records in B9021:", [r.key for r in (b9.records() if b9 else [])])
print("ROUND-TRIP IDENTICAL:", a == b)
if a != b:
    al, bl = a.split("\n"), b.split("\n")
    for i, (x, y) in enumerate(zip(al, bl)):
        if x != y:
            print(f"  first diff at line {i}")
            print("   orig:", repr(x))
            print("   out :", repr(y))
            break
    print("  line counts orig/out:", len(al), len(bl))

errs = cax.errors
print("validation errors:", len(errs))
for name, ln in errs[:5]:
    print("  [%s] %s -> %s" % (name, ln.key, ln.errors))

# sanity: structured field parse of VIEW/ADDR/SI_SLOT in B9021
if b9:
    for k in ("VIEW", "ADDR", "SI_SLOT"):
        r = b9.get(k)
        if r:
            print(f"  {k} fields:", r.fields)

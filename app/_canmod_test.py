"""Headless test for canmod.py (throwaway dev script).

Proves the Python CANDISP.DB writer is byte-identical to the Can-Display C++
generator/parser: builds the same demo record set and diffs it against the
C++-produced CANDISP.DB, and checks the CRC-16/ARC canonical value.

Run:  python app/_canmod_test.py       (from the repo root, or from app/)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import canmod

CPP_DB = r"C:\TouchGFXProjects\Can-Display\CM7\TouchGFX\CANDISP.DB"

fails = 0
def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1

# 1) CRC-16/ARC canonical check value (must match FlashChecksum::crc16Arc).
check(canmod.crc16_arc(b"123456789") == 0xBB3D, "crc16_arc('123456789') == 0xBB3D")

# 2) Build the same demo DB the C++ gen_candisp_db produced.
records = [{
    "kind": canmod.MODULE_KIND["T43"], "osid": "24243170", "label": "TCM T43 24243170",
    "mods": [
        {"name": "Rev Limit Bump (demo)",  "patches": [{"offset": 0x1C0200, "bytes": bytes([0xAA, 0x55])}]},
        {"name": "Torque Mgmt Off (demo)", "patches": [{"offset": 0x1C0210, "bytes": bytes([0x00])}]},
    ],
}]
locators = [{"kind": 0, "osid_len": 8, "osid_format": "ascii", "osid_offset": 0x6000C}]
db = canmod.build_candisp_db(records, locators)
print(f"python DB = {len(db)} bytes")

# 3) Byte-for-byte vs the C++ generator's output.
if os.path.exists(CPP_DB):
    with open(CPP_DB, "rb") as f:
        cpp = f.read()
    check(db == cpp, f"python DB byte-identical to C++ CANDISP.DB ({len(cpp)} bytes)")
    if db != cpp:
        for i in range(min(len(db), len(cpp))):
            if db[i] != cpp[i]:
                print(f"    first diff @{i}: py={db[i]:02X} cpp={cpp[i]:02X}")
                break
else:
    print(f"  (skip byte-diff: {CPP_DB} not present)")

# 4) value_to_bytes sanity (big-endian GM).
f16 = {"id": "X", "bit_offset": 0, "bit_count": 16, "datatype": "UNM", "inverse": False,
       "si": {"factor": 0.25, "offset": 0.0}}
check(canmod.value_to_bytes(f16, 1600, "big") == bytes([0x19, 0x00]), "UNM 16b 1600@0.25 -> 19 00 (raw 6400)")
f8s = {"id": "Y", "bit_offset": 0, "bit_count": 8, "datatype": "SNM", "inverse": False,
       "si": {"factor": 1.0, "offset": 0.0}}
check(canmod.value_to_bytes(f8s, -2, "big") == bytes([0xFE]), "SNM 8b -2 -> FE")
try:
    canmod.value_to_bytes({"id": "Z", "bit_offset": 3, "bit_count": 4, "datatype": "UNM",
                           "si": {"factor": 1, "offset": 0}}, 1)
    check(False, "sub-byte field raises")
except ValueError:
    check(True, "sub-byte field raises")

# 4b) sub-byte flag -> RMW patch (kind 1, mask+value); byte-aligned -> kind 0
flag = {"id": "F", "name": "Flag", "addr": [0x1234], "bit_offset": 3, "bit_count": 1,
        "datatype": "SED", "inverse": False, "si": {"factor": 1.0, "offset": 0.0}}
fp = canmod.field_patch(flag, 1, "big")
check(fp["kind"] == 1 and fp["offset"] == 0x1234 and fp["mask"] == bytes([0x08])
      and fp["value"] == bytes([0x08]), "field_patch sub-byte flag -> RMW mask=08 val=08")
fp0 = canmod.field_patch({**f16, "addr": [0x2000]}, 1600, "big")
check(fp0["kind"] == 0 and fp0["bytes"] == bytes([0x19, 0x00]), "field_patch byte-aligned -> kind 0")

# 5) manifest renders.
mtxt = canmod.manifest([{**records[0], "osid": b"24243170"}])
check("Rev Limit Bump (demo)" in mtxt and "0x1C0200" in mtxt, "manifest lists mods + offsets")

print("\n" + ("%d CHECK(S) FAILED" % fails if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)

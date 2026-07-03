"""Headless test for the Can-Display dialogs + full extract->export->build path.

Runs Qt offscreen and monkeypatches the file/message dialogs so the Build flow
executes without interaction. Run:  python app/_canmoddlg_test.py
"""
import os
import sys
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import caxformat
import canmod
import canmoddlg

fails = 0
def check(cond, msg):
    global fails
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        fails += 1

SYNTH = """\
[OS]
PCM_OSID  = 6000C
[B0001]
VIEW      = 4  "Rev Limiter"  "Limits"  .  $01  1  0  1
ADDR      = 1A420  0  16  UNM  0
SI_SLOT   = rpm  0.25  0  0  0  8000  .  .
[B0002]
VIEW      = 4  "Torque Mgmt"  "Torque"  .  $01  1  0  1
ADDR      = 1C0F0  3  1  SED  0
SI_SLOT   = none  1  0  0  0  1  .  .
"""

app = QApplication.instance() or QApplication([])
cax = caxformat.parse(SYNTH)

# 1) extraction + export
fd = canmod.extract_field(cax, "B0001")
check(fd and fd["addr"] == [0x1A420] and fd["bit_count"] == 16 and fd["datatype"] == "UNM",
      "extract_field B0001 (addr/bits/type)")
cm = canmod.export_canmod(cax, ["B0001", "B0002"], "T43", osid="24243170")
check(cm["osid_locator"]["offset"] == 0x6000C, "export picks OSID offset from [OS] PCM_OSID")
check(len(cm["fields"]) == 2, "export captured 2 fields")

# 2) value -> patch
p = canmod.field_patch(fd, 6000, "big")   # raw=6000/0.25=24000=0x5DC0
check(p["offset"] == 0x1A420 and p["bytes"] == bytes([0x5D, 0xC0]), "field_patch 6000rpm -> 5D C0 @0x1A420")

# 3) Export dialog constructs + lists both fields
dlg = canmoddlg.ExportToCanDisplayDialog(cax)
check(dlg.list.count() == 2, "ExportDialog lists 2 exportable calibrations")

# 4) Build dialog: load a canmod (bypass file dialog), tick + value, drive _build
bdlg = canmoddlg.BuildCanDisplayDbDialog()
bdlg._canmods.append((cm, "24243170"))
bdlg._append_rows(cm, "24243170")
check(bdlg.table.rowCount() == 2, "BuildDialog populated 2 rows")

# tick row 0 (Rev Limiter, byte-aligned) + row 1 (Torque Mgmt, sub-byte flag)
bdlg.table.item(0, 0).setCheckState(Qt.Checked)
bdlg.table.item(0, 4).setText("6000")
bdlg.table.item(0, 5).setText("Rev Limit 6000")
bdlg.table.item(1, 0).setCheckState(Qt.Checked)
bdlg.table.item(1, 4).setText("1")          # SED flag on (bit 3 of 0x1C0F0)
bdlg.table.item(1, 5).setText("Torque Mgmt Off")

out_db = os.path.join(tempfile.gettempdir(), "CANDISP_test.DB")
if os.path.exists(out_db):
    os.remove(out_db)
canmoddlg.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (out_db, ""))
canmoddlg.QMessageBox = types.SimpleNamespace(
    information=lambda *a, **k: None, warning=lambda *a, **k: None, critical=lambda *a, **k: None)

bdlg._build()
check(os.path.exists(out_db), "Build wrote CANDISP.DB")

data = open(out_db, "rb").read()
check(data[:4] == b"CDMD", "DB magic 'CDMD'")
# recompute payload CRC and compare to stored (self-consistency of the writer)
stored = data[8] | (data[9] << 8)
check(canmod.crc16_arc(data[12:]) == stored, "DB payload CRC self-consistent")
# the patch bytes 5D C0 must appear (16-bit rev limiter raw)
check(b"\x5d\xc0" in data, "overwrite patch (rev limiter 5D C0) present in DB")
# the RMW flag patch for B0002 @0x1C0F0: kind=01, offset LE, len=1, mask=08, value=08
rmw = bytes([0x01, 0xF0, 0xC0, 0x01, 0x00, 0x01, 0x00, 0x08, 0x08])
check(rmw in data, "RMW bit-flag patch (kind1 mask=08 val=08 @0x1C0F0) present in DB")
txt = os.path.join(os.path.dirname(out_db), "CANDISP.TXT")
check(os.path.exists(txt), "manifest CANDISP.TXT written alongside")

print("\n" + ("%d CHECK(S) FAILED" % fails if fails else "ALL CHECKS PASSED"))
sys.exit(1 if fails else 0)

"""Headless test for the Can-Display EXPORT path (.cax -> .canmod).

CAX-Generator owns the .cax -> .canmod half; the .canmod -> CANDISP.DB half (and its
tests) live in the standalone CANDISP-DB project. Runs Qt offscreen.
Run:  python app/_export_test.py
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
[B0003]
VIEW      = 4  "DTC Mode"  "Diag"  .  $01  1  0  1
ADDR      = 1D000  0  8  UNM  0
SI_SLOT   = none  1  0  0  0  2  .  .
SI_ENUM   = "0=No,1=Yes,2=Auto"
[TS073]
VIEW      = 7  "Tx Msg Object"  "CAN"  .  1  4  0  0
ADDR      = 1C03C4  0  16  SED  0
SI_SLOT   = none  1.0  0  0  0  255  0  255
SI_ENUM   = "0=MsgObj00,1=MsgObj01,255=Illegal"
SI_ROW    = switch  "0C9,0F1,0F9"
"""

app = QApplication.instance() or QApplication([])
cax = caxformat.parse(SYNTH)

# 1) extraction
fd = canmod.extract_field(cax, "B0001")
check(fd and fd["addr"] == [0x1A420] and fd["bit_count"] == 16 and fd["datatype"] == "UNM",
      "extract_field B0001 (addr/bits/type)")

# 1b) SI_ENUM parsing -> labelled options (raw=label pairs)
fd3 = canmod.extract_field(cax, "B0003")
check(fd3["enum"] == [{"value": 0, "label": "No"}, {"value": 1, "label": "Yes"},
                      {"value": 2, "label": "Auto"}],
      "extract_field parses SI_ENUM raw=label pairs")

# 2) export -> .canmod definition dict
cm = canmod.export_canmod(cax, ["B0001", "B0002", "B0003"], "T43", osid="24243170")
check(cm["osid_locator"]["offset"] == 0x6000C, "export picks OSID offset from [OS] PCM_OSID")
check(cm["module"] == "T43" and cm["osid"] == "24243170", "export carries module + osid")
check(len(cm["fields"]) == 3, "export captured 3 fields")

# 3) Export dialog constructs + lists all exportable calibrations
dlg = canmoddlg.ExportToCanDisplayDialog(cax)
check(dlg.list.count() == 4, "ExportDialog lists 4 exportable calibrations (incl. TS073 table)")

# 3b) multi-label calibration expands to one field per label at strided addresses
fdt = canmod.extract_field(cax, "TS073")
check(fdt["labels"] == ["0C9", "0F1", "0F9"], "extract parses SI_ROW label list")
exp = canmod.expand_field(fdt)
check(len(exp) == 3, "multi-label expands to 3 per-label fields")
check([e["addr"][0] for e in exp] == [0x1C03C4, 0x1C03C6, 0x1C03C8], "16-bit stride (base + i*2)")
check(exp[1]["id"] == "TS073[0F1]" and exp[1]["label"] == "0F1", "per-label id + label carried")
check(exp[0]["enum"][0]["label"] == "MsgObj00", "each expanded cell keeps the SI_ENUM options")
cm2 = canmod.export_canmod(cax, ["TS073"], "E38", osid="12639270")
check(len(cm2["fields"]) == 3, "export of a multi-label cal yields 3 fields")

# 4) osid_from_filename (used by main to prefill the export dialog)
check(canmod.osid_from_filename("T43_24256125.cax") == "24256125", "osid_from_filename picks the 8-digit OS")
check(canmod.osid_from_filename("e40_12598977_GTO.cax") == "12598977", "osid_from_filename skips module prefix")

print(f"\n{'FAILED ' + str(fails) if fails else 'OK'}")
sys.exit(1 if fails else 0)

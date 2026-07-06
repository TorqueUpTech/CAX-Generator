"""
canmoddlg — PySide6 Export dialog for the Can-Display cal-mod pipeline.

  * ExportToCanDisplayDialog: tick the calibrations to expose as modifications ->
    write a `.canmod` definition file (field defs, no values).

`.canmod` is the handoff to the standalone CANDISP-DB tool, which owns the
`.canmod` -> CANDISP.DB half. All extraction logic is in the Qt-free `canmod`
module (unit-tested); this is a thin GUI wrapper.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout)

import canmod


def _exportable(fd: dict) -> bool:
    """One address, and either byte-aligned (kind-0 overwrite) or a sub-byte field
    within one byte (kind-1 RMW bit-flag). Multi-column tables and cross-byte
    bitfields are deferred."""
    if len(fd.get("addr", [])) != 1:
        return False
    bo, bc = fd["bit_offset"], fd["bit_count"]
    if bo == 0 and bc % 8 == 0:
        return True
    return bo + bc <= 8


# --------------------------------------------------------------------------- #
#  Export -> To Can-Display
# --------------------------------------------------------------------------- #
class ExportToCanDisplayDialog(QDialog):
    def __init__(self, cax, parent=None, module=None, osid=""):
        super().__init__(parent)
        self.cax = cax
        self.setWindowTitle("Export -> To Can-Display")
        self.resize(560, 520)

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Module:"))
        self.module = QComboBox()
        self.module.addItems(["T43", "E38"])
        if module:                              # inferred from the loaded controller
            i = self.module.findText(module.upper())
            if i >= 0:
                self.module.setCurrentIndex(i)
        row.addWidget(self.module)
        row.addWidget(QLabel("OSID (optional):"))
        self.osid = QLineEdit(osid)                    # pre-filled from the cax file name (the OS number)
        self.osid.setPlaceholderText("dial in later if blank")
        row.addWidget(self.osid, 1)
        v.addLayout(row)

        v.addWidget(QLabel("Tick the calibrations to expose as modifications:"))
        self.list = QListWidget()
        v.addWidget(self.list, 1)

        # populate: every calibration with a single byte-aligned address (Val).
        self._skipped = 0
        for sec in cax.sections:
            if sec.name == "OS":
                continue
            fd = canmod.extract_field(cax, sec.name)
            if not fd or not fd.get("addr"):
                continue
            if not _exportable(fd):
                self._skipped += 1
                continue
            units = fd["si"]["units"]
            label = f"{fd['id']}  —  {fd['name']}" + (f"  [{units}]" if units else "")
            nlabels = len(fd.get("labels") or [])
            if nlabels > 1:                          # multi-label table -> N per-label rows
                label += f"  ·  {nlabels} labels"
            if fd.get("desc"):
                label += f"\n      {fd['desc']}"
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, fd["id"])
            self.list.addItem(it)

        note = QLabel(f"{self.list.count()} exportable · {self._skipped} skipped "
                      "(2D tables / cross-byte bitfields — not supported yet). "
                      "1D label-lists expand to one row per label.")
        note.setStyleSheet("color: gray;")
        v.addWidget(note)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _checked_names(self):
        return [self.list.item(i).data(Qt.UserRole)
                for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]

    def _save(self):
        names = self._checked_names()
        if not names:
            QMessageBox.information(self, "Export", "Tick at least one calibration.")
            return
        data = canmod.export_canmod(self.cax, names, self.module.currentText(),
                                    osid=self.osid.text().strip())
        default = f"{self.module.currentText()}_{self.osid.text().strip() or 'mods'}.canmod"
        path, _ = QFileDialog.getSaveFileName(self, "Save .canmod", default,
                                              "Can-Display mod defs (*.canmod)")
        if not path:
            return
        try:
            canmod.write_canmod(data, path)
        except OSError as e:
            QMessageBox.critical(self, "Export", f"Write failed:\n{e}")
            return
        QMessageBox.information(self, "Export",
                                f"Wrote {len(names)} field(s) to\n{path}")
        self.accept()

"""
canmoddlg — PySide6 dialogs for the Can-Display cal-mod workflow.

  * ExportToCanDisplayDialog: tick the calibrations to expose as modifications ->
    write a `.canmod` definition file (no values).
  * BuildCanDisplayDbDialog: load one or more `.canmod` files, dial in a target
    VALUE per field, name the mods, and write CANDISP.DB (+ CANDISP.TXT) for the SD
    card.

All the format/conversion logic is in the Qt-free `canmod` module (unit-tested);
these are thin GUI wrappers.
"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

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
    def __init__(self, cax, parent=None):
        super().__init__(parent)
        self.cax = cax
        self.setWindowTitle("Export -> To Can-Display")
        self.resize(560, 520)

        v = QVBoxLayout(self)
        row = QHBoxLayout()
        row.addWidget(QLabel("Module:"))
        self.module = QComboBox()
        self.module.addItems(["T43", "E38"])
        row.addWidget(self.module)
        row.addWidget(QLabel("OSID (optional):"))
        self.osid = QLineEdit()
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
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Unchecked)
            it.setData(Qt.UserRole, fd["id"])
            self.list.addItem(it)

        note = QLabel(f"{self.list.count()} exportable · {self._skipped} skipped "
                      "(multi-column tables / cross-byte bitfields — not supported yet)")
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


# --------------------------------------------------------------------------- #
#  Build Can-Display DB (combiner)
# --------------------------------------------------------------------------- #
_COL = ["Use", "OS", "Field", "Units", "Value", "Mod name"]


class BuildCanDisplayDbDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Build Can-Display DB")
        self.resize(860, 560)
        self._canmods = []   # list of (canmod_dict, osid_str)

        v = QVBoxLayout(self)
        top = QHBoxLayout()
        add = QPushButton("Add .canmod…")
        add.clicked.connect(self._add)
        top.addWidget(add)
        top.addStretch(1)
        top.addWidget(QLabel("Endianness:"))
        self.endian = QComboBox()
        self.endian.addItems(["big (GM)", "little"])
        top.addWidget(self.endian)
        v.addLayout(top)

        self.table = QTableWidget(0, len(_COL))
        self.table.setHorizontalHeaderLabels(_COL)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        v.addWidget(self.table, 1)

        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.build_btn = QPushButton("Build CANDISP.DB…")
        self.build_btn.clicked.connect(self._build)
        bb.addButton(self.build_btn, QDialogButtonBox.AcceptRole)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    # --- loading ----------------------------------------------------------- #
    def _add(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add .canmod files", "",
                                                "Can-Display mod defs (*.canmod);;All files (*)")
        for p in paths:
            try:
                cm = canmod.read_canmod(p)
            except (OSError, ValueError) as e:
                QMessageBox.warning(self, "Load", f"{os.path.basename(p)}:\n{e}")
                continue
            osid = (cm.get("osid") or "").strip()
            if not osid:
                osid, ok = QInputDialog.getText(
                    self, "OSID", f"OSID for {cm.get('module', '?')} "
                    f"({os.path.basename(p)}):")
                if not ok or not osid.strip():
                    continue
                osid = osid.strip()
            self._canmods.append((cm, osid))
            self._append_rows(cm, osid)

    def _append_rows(self, cm, osid):
        os_label = f"{cm.get('module', '?')}:{osid}"
        for fd in cm.get("fields", []):
            r = self.table.rowCount()
            self.table.insertRow(r)

            use = QTableWidgetItem()
            use.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            use.setCheckState(Qt.Unchecked)
            self.table.setItem(r, 0, use)

            os_item = QTableWidgetItem(os_label)
            os_item.setFlags(Qt.ItemIsEnabled)          # read-only
            os_item.setData(Qt.UserRole, (len(self._canmods) - 1, fd["id"]))
            self.table.setItem(r, 1, os_item)

            name_item = QTableWidgetItem(f"{fd['id']}  {fd['name']}")
            name_item.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 2, name_item)

            u = QTableWidgetItem(fd["si"].get("units", ""))
            u.setFlags(Qt.ItemIsEnabled)
            self.table.setItem(r, 3, u)

            self.table.setItem(r, 4, QTableWidgetItem(""))          # Value (editable)
            self.table.setItem(r, 5, QTableWidgetItem(fd["name"]))  # Mod name (editable)

    # --- building ---------------------------------------------------------- #
    def _field_for(self, cm_index, field_id):
        cm, _ = self._canmods[cm_index]
        for fd in cm.get("fields", []):
            if fd["id"] == field_id:
                return fd
        return None

    def _build(self):
        endian = "big" if self.endian.currentIndex() == 0 else "little"
        # per-OS accumulation: (cm_index, osid) -> record dict
        records_by_os = {}
        locators = {}
        errors = []

        for r in range(self.table.rowCount()):
            if self.table.item(r, 0).checkState() != Qt.Checked:
                continue
            value_txt = (self.table.item(r, 4).text() or "").strip()
            if value_txt == "":
                continue
            cm_index, field_id = self.table.item(r, 1).data(Qt.UserRole)
            cm, osid = self._canmods[cm_index]
            fd = self._field_for(cm_index, field_id)
            if not fd:
                continue
            try:
                value = float(value_txt)
                patch = canmod.field_patch(fd, value, endian)
            except (ValueError, KeyError) as e:
                errors.append(f"{fd['id']}: {e}")
                continue

            kind = canmod.MODULE_KIND.get(cm.get("module", "T43"), 0)
            key = (cm_index, osid)
            rec = records_by_os.get(key)
            if rec is None:
                rec = {"kind": kind, "osid": osid,
                       "label": f"{cm.get('module', '?')} {osid}", "mods": []}
                records_by_os[key] = rec
            mod_name = (self.table.item(r, 5).text() or fd["name"]).strip()
            rec["mods"].append({"name": mod_name, "patches": [patch]})

            loc = dict(cm.get("osid_locator") or canmod.DEFAULT_OSID_LOCATOR.get(cm.get("module", "T43")))
            if loc.get("offset") is not None and kind not in locators:
                locators[kind] = {"kind": kind, "osid_len": loc.get("len", len(osid)),
                                  "osid_format": loc.get("format", "ascii"),
                                  "osid_offset": loc["offset"]}

        records = [r for r in records_by_os.values() if r["mods"]]
        if errors:
            QMessageBox.warning(self, "Build",
                                "Some fields were skipped:\n\n" + "\n".join(errors[:12]))
        if not records:
            QMessageBox.information(self, "Build",
                                    "Nothing to build — tick fields and enter values.")
            return
        if not locators:
            QMessageBox.critical(self, "Build",
                                 "No OSID locator (offset) for the selected module(s).")
            return

        db = canmod.build_candisp_db(records, list(locators.values()))
        txt = canmod.manifest(records)
        path, _ = QFileDialog.getSaveFileName(self, "Save CANDISP.DB", "CANDISP.DB",
                                              "Can-Display DB (*.DB)")
        if not path:
            return
        try:
            with open(path, "wb") as f:
                f.write(db)
            with open(os.path.join(os.path.dirname(path), "CANDISP.TXT"), "w",
                      encoding="ascii", errors="replace") as f:
                f.write(txt)
        except OSError as e:
            QMessageBox.critical(self, "Build", f"Write failed:\n{e}")
            return
        n_mods = sum(len(r["mods"]) for r in records)
        QMessageBox.information(
            self, "Build",
            f"Wrote {path}\n({len(records)} OS record(s), {n_mods} mod(s), "
            f"{len(db)} bytes) + CANDISP.TXT")
        self.accept()

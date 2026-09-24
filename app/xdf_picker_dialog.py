"""
XDF Picker Dialog — select which TunerPro XDF definitions to import into a .cax.

Mirrors a2l_picker_dialog.A2lPickerDialog so XDF import behaves like A2L import:
browse a file, parse it, tick the calibrations you want (with search + preview),
then import only the selection. Unlike A2L (which augments an existing .cax), XDF
import can also START a new .cax, so `cax` may be None -> one is created on import.
"""

import os
import re
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QSplitter, QTextEdit, QProgressDialog, QWidget
)
from PySide6.QtCore import Qt

import caxformat as cf
import segmap
import xdfimport


class XdfPickerDialog(QDialog):
    """Select + import TunerPro XDF calibrations into a .cax."""

    def __init__(self, parent=None, cax=None):
        super().__init__(parent)
        self.cax = cax                  # may be None -> created on import
        self.items = []                 # all parsed XdfItem
        self.segs = None                # segment ranges for the picked file
        self.controller = ""
        self.os_id = ""
        self.imported = 0
        self.report = None

        self.setWindowTitle("Import XDF Definitions")
        self.setGeometry(100, 100, 900, 600)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout()

        # File selector
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("XDF File:"))
        self.file_button = QPushButton("Browse...")
        self.file_button.clicked.connect(self._select_file)
        file_layout.addWidget(self.file_button)
        self.file_label = QLabel("(no file selected)")
        file_layout.addWidget(self.file_label)
        file_layout.addStretch()
        layout.addLayout(file_layout)

        # Search + select all/clear
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Type to filter definitions...")
        self.search_field.textChanged.connect(self._on_search_changed)
        search_layout.addWidget(self.search_field)
        search_layout.addStretch()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        search_layout.addWidget(self.select_all_btn)
        self.clear_btn = QPushButton("Clear All")
        self.clear_btn.clicked.connect(self._clear_all)
        search_layout.addWidget(self.clear_btn)
        layout.addLayout(search_layout)

        # List | preview
        splitter = QSplitter(Qt.Horizontal)
        self.cal_list = QListWidget()
        self.cal_list.itemChanged.connect(lambda _i: self._update_import_button())
        self.cal_list.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.cal_list)

        preview_layout = QVBoxLayout()
        preview_layout.addWidget(QLabel("Preview:"))
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)
        pw = QWidget(); pw.setLayout(preview_layout)
        splitter.addWidget(pw)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        # Status
        layout.addWidget(QLabel("Status:"))
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMaximumHeight(72)
        layout.addWidget(self.status_text)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        self.import_btn = QPushButton("Import Selected")
        self.import_btn.clicked.connect(self._import)
        self.import_btn.setEnabled(False)
        button_layout.addWidget(self.import_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)
        layout.addLayout(button_layout)

        self.setLayout(layout)

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select XDF File", "", "XDF Files (*.xdf);;All Files (*)")
        if not path:
            return

        progress = QProgressDialog("Parsing XDF file...", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        self.repaint()
        try:
            self.items = xdfimport.parse_xdf(path)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to parse XDF file:\n{e}")
            self.file_label.setText("(error)")
            return

        # Segment ranges from the XDF filename (controller + 8-digit OS), for
        # address classification -- same derivation the old direct import used.
        stem = os.path.basename(path)
        parts = re.split(r"[_\-.]", stem)
        self.controller = parts[0].upper() if parts else ""
        m = re.search(r"\d{8}", stem)
        self.os_id = m.group(0) if m else ""
        self.segs = (segmap.cached_segments(self.os_id) or
                     segmap.cached_segments_for_controller(self.controller)[0])

        self.file_label.setText(Path(path).name)
        self._populate_list()
        progress.close()
        seg_note = "" if self.segs else " (no segment ranges — nothing will be skipped)"
        self.status_text.setText(
            f"Parsed {len(self.items)} definition(s) from {stem} "
            f"— controller {self.controller or '?'}, OS {self.os_id or '?'}.{seg_note}")

    def _populate_list(self):
        self.cal_list.clear()
        self.cal_list.setUpdatesEnabled(False)
        for i, it in enumerate(self.items):
            li = QListWidgetItem()
            addr = f"0x{it.address:X}" if it.address is not None else "no-addr"
            li.setText(f"{addr} — [{it.kind}] {it.title}")
            li.setFlags(li.flags() | Qt.ItemIsUserCheckable)
            li.setCheckState(Qt.Unchecked)
            li.setData(Qt.UserRole, it)
            self.cal_list.addItem(li)
            if (i + 1) % 500 == 0:
                self.repaint()
        self.cal_list.setUpdatesEnabled(True)

    def _on_search_changed(self):
        q = self.search_field.text().lower()
        for i in range(self.cal_list.count()):
            item = self.cal_list.item(i)
            self.cal_list.setRowHidden(i, bool(q) and q not in item.text().lower())

    def _on_selection_changed(self):
        sel = self.cal_list.selectedItems()
        if not sel:
            self.preview_text.setText("")
            return
        it = sel[0].data(Qt.UserRole)
        if not it:
            return
        addr = f"0x{it.address:X}" if it.address is not None else "N/A"
        shape = ""
        if it.kind == "table":
            shape = f"\nShape: {it.rowcount} rows x {it.colcount} cols"
        enum = f"\nEnum: {it.enum}" if it.enum else ""
        note = f"\n\n⚠ {it.note}" if it.note else ""
        self.preview_text.setText(
            f"Name: {it.title}\n"
            f"Kind: {it.kind}\n"
            f"Address: {addr}\n"
            f"Bits: {it.bits}  (bitoffset {it.bitoff})  signed={it.signed}\n"
            f"Units: {it.units or '-'}   Precision: {it.prec}\n"
            f"Factor: {it.factor}   Offset: {it.offset}"
            f"{shape}{enum}\n\n{it.desc}{note}".strip())

    def _update_import_button(self):
        n = sum(1 for i in range(self.cal_list.count())
                if self.cal_list.item(i).checkState() == Qt.Checked)
        self.import_btn.setEnabled(n > 0)

    def _select_all(self):
        for i in range(self.cal_list.count()):
            if not self.cal_list.isRowHidden(i):
                self.cal_list.item(i).setCheckState(Qt.Checked)
        self._update_import_button()

    def _clear_all(self):
        for i in range(self.cal_list.count()):
            self.cal_list.item(i).setCheckState(Qt.Unchecked)
        self._update_import_button()

    def _import(self):
        selected = [self.cal_list.item(i).data(Qt.UserRole)
                    for i in range(self.cal_list.count())
                    if self.cal_list.item(i).checkState() == Qt.Checked]
        selected = [s for s in selected if s]
        if not selected:
            QMessageBox.warning(self, "Error", "No definitions selected")
            return

        progress = QProgressDialog(
            f"Importing {len(selected)} definition(s)...", None, 0, 0, self)
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        try:
            if self.cax is None:
                self.cax = cf.CaxFile()
            self.imported, self.report = xdfimport.import_selected_to_cax(
                self.cax, selected, self.segs)
            progress.close()
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Import failed:\n{e}")
            return

        msg = f"Imported {self.imported} definition(s)."
        rpt = self.report
        if rpt is not None:
            if rpt.skipped:
                msg += (f"\n\nSkipped {len(rpt.skipped)} outside the flash image "
                        "(RAM / out-of-range):\n")
                msg += "\n".join(
                    f"  • {t}" + (f"  @{a:X}" if a is not None else "  (no address)")
                    for t, a in rpt.skipped[:10])
                if len(rpt.skipped) > 10:
                    msg += f"\n  ... and {len(rpt.skipped) - 10} more"
            if rpt.review:
                msg += f"\n\nNeeds review ({len(rpt.review)}):\n"
                msg += "\n".join(f"  • {t}: {n}" for t, n in rpt.review[:10])
        QMessageBox.information(self, "Import Complete", msg)
        self.accept()

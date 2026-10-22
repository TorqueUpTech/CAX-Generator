"""
Ultimate Cax Generator — editor for EFILive .cax (V7) / .cax8 (V8) files.

Tree (Segment > Major > Minor > Caption) on the left, schema-driven record
editor on the right. Core parse/validate/write lives in caxformat.py.
"""
from __future__ import annotations

import copy
import os
import re
import subprocess
import sys

from PySide6.QtCore import QFileInfo, Qt, QTimer
from PySide6.QtGui import (QAction, QKeySequence, QShortcut, QStandardItem,
                           QStandardItemModel)
# NOTE: never call _show_section() synchronously from a child widget's own
# signal (e.g. the ADDR Data Type combo's currentTextChanged) — it rebuilds the
# detail pane and deletes that widget mid-emission, which crashes. Defer with
# QTimer.singleShot(0, ...). See _on_datatype_changed.
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog,
    QFileIconProvider, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QSplitter, QStyle,
    QTabWidget, QToolBar, QTreeView, QVBoxLayout, QWidget,
)

import addrcompare
import backup as backupmod
import catalog as catalogmod
import caxformat as cf
import caxtree as ct
import convert
import formats
from hexview import HexView
import idgen
import segmap
import segments as segmod
import themes
import units as unitsmod
import xdfimport
import settings as appsettings
import verify as verifymod

APP_NAME = "Ultimate Cax Generator"

# Shared across all open windows: copied calibration sections + live window refs.
_CLIPBOARD: list = []          # list[cf.Section] (deep copies)
_FOLDER_CUT: dict | None = None  # {"seg": str, "major": str, "minor": str} for cut folders
_OPEN_WINDOWS: list = []       # keep MainWindow refs alive

ROLE_SECTION = Qt.UserRole + 1
ROLE_KIND = Qt.UserRole + 2     # 'seg' | 'major' | 'minor' | 'leaf'
ROLE_SEG = Qt.UserRole + 3      # segment label of this node's branch
ROLE_MAJOR = Qt.UserRole + 4
ROLE_MINOR = Qt.UserRole + 5


_OVERLAP_ID_RE = re.compile(r"user calibration:\s*([A-Za-z]{1,5}\d[A-Za-z0-9]*)",
                            re.IGNORECASE)


def parse_overlap_warnings(text: str):
    """Parse pasted EFILive overlap warnings into (efilive_subjects, pairs).

    Two shapes appear, e.g.:
        User calibration: TC0001 overlaps one or more EFILive calibration(s)
        User calibration: TC0003 overlaps user calibration: TC0179

    - The first names only a subject that overlaps a built-in EFILive cal; the
      only fix is to remove the subject -> returned in ``efilive_subjects``.
    - The second names two of the user's own calibrations that overlap; either
      one may be kept -> returned in ``pairs`` as (subject, counterpart).

    ``efilive_subjects`` preserves first-seen order and is de-duplicated (and
    excludes ids that appear in a pair). ``pairs`` is de-duplicated as unordered
    pairs, keeping the first-seen orientation.
    """
    efilive, pairs, pair_seen = [], [], set()
    for line in text.splitlines():
        ids = _OVERLAP_ID_RE.findall(line)
        if not ids:
            continue
        if len(ids) >= 2:
            a, b = ids[0], ids[1]
            if a != b and frozenset((a, b)) not in pair_seen:
                pair_seen.add(frozenset((a, b)))
                pairs.append((a, b))
        else:
            if ids[0] not in efilive:
                efilive.append(ids[0])
    paired = {i for p in pairs for i in p}
    efilive = [i for i in efilive if i not in paired]
    return efilive, pairs


def _valid_os_name(name: str) -> bool:
    """A plausible OS-number filename stem: non-empty, no path separators or
    characters Windows forbids in filenames, and not just dots."""
    if not name or name.strip(".") == "":
        return False
    return not any(c in name for c in '\\/:*?"<>|')


# ---- Editor schema: how each record key is presented --------------------------
# Each entry: list of (label, kind, options). kind: 'line' | 'combo' | 'text'.
COMBO = {
    "visibility": [("0", "0 Hidden"), ("1", "1 Demo/All"),
                   ("2", "2 Personal+"), ("3", "3 Workshop+")],
    "display":    [("1", "1 Val"), ("2", "2 Col"), ("3", "3 Map"), ("4", "4 List")],
    "force2d":    [("0", "0 No"), ("1", "1 Force 2D")],
    "group":      [("0", "0 Basic"), ("1", "1 Standard"), ("2", "2 Expert")],
    "datatype":   [("UNM", "UNM unsigned"), ("SNM", "SNM signed"),
                   ("SED", "SED enum"), ("FLT", "FLT float")],
    "invert":     [("0", "0 As-is"), ("1", "1 Inverse 1/N")],
}

SLOT_FIELDS = [("Units", "units"), ("Factor", "line"), ("Offset", "line"),
               ("Prec", "line"), ("Min Limit", "line"), ("Max Limit", "line"),
               ("Min User", "line"), ("Max User", "line")]

SCHEMA: dict[str, list[tuple]] = {
    "VIEW": [
        ("Segment", "line"), ("Caption", "line"), ("Major", "line"),
        ("Minor", "line"), ("Visibility", "combo", "visibility"),
        ("Display Type", "combo", "display"), ("Force 2D", "combo", "force2d"),
        ("Group", "combo", "group"),
    ],
    "ADDR": [
        ("Address(es)", "line"), ("Bit Offset", "line"), ("Bit Count", "line"),
        ("Data Type", "combo", "datatype"), ("Inverse", "combo", "invert"),
    ],
    "SI_SLOT": SLOT_FIELDS,
    "IM_SLOT": SLOT_FIELDS,
    "SI_ENUM": [("Metric labels (comma list)", "line")],
    "IM_ENUM": [("Imperial labels (comma list)", "line")],
    "SI_ROW": [("Units", "units"), ("Labels (comma list)", "line")],
    "IM_ROW": [("Units", "units"), ("Labels (comma list)", "line")],
    "SI_COL": [("Units", "units"), ("Labels (comma list)", "line")],
    "IM_COL": [("Units", "units"), ("Labels (comma list)", "line")],
    "ULABELS": [("Body", "line"), ("Col", "line"), ("Row", "line")],
}

# VIEW fields managed by the tree (segment branch / leaf caption / folder path),
# so they're hidden from the per-calibration editor form. Values are preserved.
HIDDEN_FIELDS: dict[str, set[int]] = {
    "VIEW": {0, 1, 2, 3},   # Segment, Caption, Major, Minor
}

ICON_GLYPH = {"val": "•", "col": "▥", "map": "▦", "unknown": "▢"}


def _label_count(line) -> int:
    """Number of comma-separated labels in an SI_COL/SI_ROW record (0 if none)."""
    if line and len(line.fields) > 1 and line.fields[1] not in (".", ""):
        return len([x for x in line.fields[1].split(",") if x.strip()])
    return 0

# Option descriptions taken from EFILive's Template.cax header (for the
# Preferences ▸ VIEW defaults tab).
VIEW_OPTION_HELP = {
    "visibility": (
        "<b>License level</b> (low bits):<br>"
        "&nbsp;$00 = Not visible to the user, but still available to the Tuner Tool software<br>"
        "&nbsp;$01 = Demo — visible to All (incl. unlicensed) license levels<br>"
        "&nbsp;$02 = Personal — visible to Personal and Workshop licenses only<br>"
        "&nbsp;$03 = Workshop — visible to Workshop and Commercial only<br>"
        "<b>Bit flags</b> (OR with the level):<br>"
        "&nbsp;$10 = not visible on PCs with a USA Location ID<br>"
        "&nbsp;$20 = value shown in the [Calibration] tab when a tune is first opened<br>"
        "&nbsp;$40 = editable but never saved back to the tune (read-only)<br>"
        "&nbsp;$80 = cannot be modified by the user (read-only)"),
    "group": (
        "Default group the calibration appears in:<br>"
        "&nbsp;0 = Basic &nbsp;·&nbsp; 1 = Standard &nbsp;·&nbsp; 2 = Expert"),
}


class CalTreeView(QTreeView):
    """Tree that lets calibrations be dragged onto a segment/folder to re-home
    them. The actual VIEW.Seg/Major/Minor update is done by drop_handler."""

    def __init__(self):
        super().__init__()
        self.setHeaderHidden(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.drop_handler = None

    def dragEnterEvent(self, e):
        e.accept()

    def dragMoveEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        e.accept() if idx.isValid() else e.ignore()

    def dropEvent(self, e):
        m = self.model()
        idx = self.indexAt(e.position().toPoint())
        if not idx.isValid() or self.drop_handler is None:
            e.ignore()
            return
        target = m.itemFromIndex(idx)
        names = []
        for i in self.selectedIndexes():
            it = m.itemFromIndex(i)
            if it and it.data(ROLE_SECTION):
                names.append(it.data(ROLE_SECTION))
        if names:
            self.drop_handler(target, names)
        e.accept()


class DescriptionEditor(QGroupBox):
    """Multi-line Description bound to a section's DESC.NNN records.

    One text line per DESC record; pressing Enter adds a line, which becomes the
    next DESC.NNN on save. The records are rewritten on every edit.
    """

    def __init__(self, section: cf.Section, on_change):
        super().__init__("Description")
        self._section = section
        self._on_change = on_change
        lay = QVBoxLayout(self)
        self._edit = QPlainTextEdit()
        self._edit.setPlaceholderText("One line per paragraph (DESC.001, DESC.002, …). "
                                      "Press Enter for a new line.")
        self._edit.setPlainText("\n".join(self._current_lines()))
        self._edit.setFixedHeight(96)
        self._edit.textChanged.connect(self._commit)
        lay.addWidget(self._edit)

    def _desc_lines(self):
        out = []
        for i, ln in enumerate(self._section.lines):
            if ln.is_record and (ln.key or "").startswith("DESC"):
                out.append((i, ln))
        return out

    def _current_lines(self):
        return [(ln.fields[0] if ln.fields else "") for _i, ln in self._desc_lines()]

    def _commit(self):
        texts = self._edit.toPlainText().split("\n")
        while len(texts) > 1 and texts[-1] == "":   # trim trailing blank lines
            texts.pop()
        existing = self._desc_lines()
        insert_at = existing[0][0] if existing else 0
        # Remove old DESC lines, splice in freshly numbered ones at the same spot.
        self._section.lines = [
            ln for ln in self._section.lines
            if not (ln.is_record and (ln.key or "").startswith("DESC"))]
        new = [cf.Line(cf.LineKind.RECORD, "", key=f"DESC.{n:03d}",
                       fields=[t], edited=True)
               for n, t in enumerate(texts, start=1)]
        self._section.lines[insert_at:insert_at] = new
        self._on_change()


class RecordGroup(QGroupBox):
    """An editable group of widgets bound to one record Line."""

    def __init__(self, line: cf.Line, on_change, collapsible=False, expanded=True,
                 title=None, addr_goto=None, sibling_si=None, units_locked=False,
                 on_datatype=None):
        super().__init__(title or line.key)
        self._line = line
        self._on_change = on_change
        self._addr_goto = addr_goto
        self._sibling_si = sibling_si      # SI_SLOT Line (set when this is IM_SLOT)
        self._units_locked = units_locked  # True -> units fixed (e.g. SED enum)
        self._on_datatype = on_datatype    # ADDR: called when Data Type changes
        self._widgets: list = []
        self._unit_warns: dict = {}        # field index -> (combo, warn_label, fix_btn)
        schema = SCHEMA.get((line.key or "").split(".")[0], None)
        if collapsible:
            # Checkbox in the title acts as an expander; contents live in an
            # inner widget that's hidden when collapsed.
            outer = QVBoxLayout(self)
            outer.setContentsMargins(6, 2, 6, 2)
            inner = QWidget()
            form = QFormLayout(inner)
            outer.addWidget(inner)
            self.setCheckable(True)
            self.setChecked(expanded)
            inner.setVisible(expanded)
            self.toggled.connect(inner.setVisible)
        else:
            form = QFormLayout(self)
        if schema is None:
            # Unknown / DESC / free record: single raw line edit.
            w = QLineEdit("  ".join(line.fields))
            w.textChanged.connect(lambda _t: self._commit_raw())
            form.addRow(line.key, w)
            self._widgets.append((0, "__raw__", w))
            return
        hidden = HIDDEN_FIELDS.get((line.key or "").split(".")[0], set())
        for i, spec in enumerate(schema):
            if i in hidden:        # managed by the tree (segment/caption/major/minor)
                continue
            label, kind = spec[0], spec[1]
            val = line.fields[i] if i < len(line.fields) else ""
            if kind == "units":
                w, row = self._build_units_field(i, val)
                self._widgets.append((i, kind, w))
                form.addRow(label, row)
                continue
            if kind == "combo":
                w = QComboBox()
                w.setEditable(True)
                for code, text in COMBO[spec[2]]:
                    w.addItem(text, code)
                self._set_combo(w, val)
                w.currentTextChanged.connect(lambda _t: self._commit())
                w.currentIndexChanged.connect(lambda _i: self._commit())
                # ADDR Data Type drives the SED -> "none" slot normalization.
                if (line.key or "").startswith("ADDR") and spec[2] == "datatype" \
                        and self._on_datatype is not None:
                    w.currentTextChanged.connect(lambda _t: self._on_datatype())
            else:
                w = QLineEdit(val)
                w.textChanged.connect(lambda _t: self._commit())
            self._widgets.append((i, kind, w))
            form.addRow(label, w)
        if (line.key or "").startswith("IM_SLOT") and self._sibling_si is not None:
            btn = QPushButton("Fill Factor/Offset from SI (× conversion)")
            btn.clicked.connect(self._fill_im_from_si)
            self._im_fill_btn = btn
            form.addRow("", btn)
            self._refresh_im_fill()
        if self._addr_goto and (line.key or "").startswith("ADDR") and self._widgets:
            go = QPushButton("Go to in hex →")
            go.clicked.connect(
                lambda: self._addr_goto(self._widgets[0][2].text()))
            form.addRow("", go)
        if line.errors:
            err = QLabel("⚠ " + "; ".join(line.errors))
            err.setStyleSheet("color:#b00; font-weight:bold;")
            form.addRow(err)

    @staticmethod
    def _set_combo(w: QComboBox, val: str):
        idx = w.findData(val)
        if idx >= 0:
            w.setCurrentIndex(idx)
        else:
            w.setEditText(val)

    def _build_units_field(self, idx: int, val: str):
        """Editable combo of canonical EFILive units (grouped), plus an inline
        non-standard-unit warning + one-click Sanitize. Returns (combo, row)."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(4)
        w = QComboBox()
        w.setEditable(True)
        w.setInsertPolicy(QComboBox.NoInsert)
        w.addItem("", "")          # allow clearing the unit
        for group, items in unitsmod.grouped():
            w.insertSeparator(w.count())
            for sym, name in items:
                w.addItem(sym, sym)
                if name and name != sym:
                    w.setItemData(w.count() - 1, f"{group} · {name}", Qt.ToolTipRole)
        self._set_combo(w, val or "")
        warn = QLabel("")
        warn.setStyleSheet("color:#b07000;")
        fix = QPushButton("Sanitize")
        fix.setVisible(False)
        fix.clicked.connect(lambda: self._sanitize_unit(idx))
        h.addWidget(w, 1)
        h.addWidget(warn)
        h.addWidget(fix)
        self._unit_warns[idx] = (w, warn, fix)
        w.currentTextChanged.connect(lambda _t, i=idx: self._on_unit_changed(i))
        w.currentIndexChanged.connect(lambda _i: self._commit())
        if self._units_locked:
            # SED enum: units are fixed to "none" and not user-editable.
            w.setEnabled(False)
            warn.setText("enum (SED) — unitless")
            warn.setStyleSheet("color:#777;")
            fix.setVisible(False)
        else:
            self._refresh_unit_warn(idx)
        return w, row

    def _on_unit_changed(self, idx: int):
        self._commit()
        self._refresh_unit_warn(idx)
        self._refresh_im_fill()

    def _refresh_unit_warn(self, idx: int):
        w, warn, fix = self._unit_warns[idx]
        sym = w.currentText().strip()
        if not sym or unitsmod.is_canonical(sym):
            warn.setText("")
            fix.setVisible(False)
            w.setStyleSheet("")
            return
        w.setStyleSheet("QComboBox{background:#fff6e0;}")
        canon = unitsmod.sanitize(sym)
        if canon:
            warn.setText(f"⚠ not a V8 unit → {canon}")
            fix.setVisible(True)
        else:
            warn.setText("⚠ unknown unit (user-defined?)")
            fix.setVisible(False)

    def _sanitize_unit(self, idx: int):
        w, _warn, _fix = self._unit_warns[idx]
        canon = unitsmod.sanitize(w.currentText().strip())
        if canon:
            self._set_combo(w, canon)   # triggers _on_unit_changed -> commit

    # ---- IM_SLOT factor/offset auto-fill from the SI slot --------------------
    def _refresh_im_fill(self):
        btn = getattr(self, "_im_fill_btn", None)
        if btn is None or self._sibling_si is None:
            return
        si_u = (self._sibling_si.fields[0] if self._sibling_si.fields else "").strip()
        im_w = self._unit_warns.get(0)
        im_u = im_w[0].currentText().strip() if im_w else ""
        conv = unitsmod.conversion(si_u, im_u)
        ok = conv is not None and unitsmod.compute_im_slot(
            self._sibling_si.fields[1] if len(self._sibling_si.fields) > 1 else "1",
            self._sibling_si.fields[2] if len(self._sibling_si.fields) > 2 else "0",
            conv) is not None
        btn.setEnabled(bool(ok))
        btn.setToolTip("" if ok else
                       "No EFILive conversion between the SI and IM units "
                       "(or it's non-linear).")

    def _fill_im_from_si(self):
        si = self._sibling_si
        if si is None:
            return
        si_u = (si.fields[0] if si.fields else "").strip()
        im_u = self._unit_warns[0][0].currentText().strip()
        conv = unitsmod.conversion(si_u, im_u)
        res = unitsmod.compute_im_slot(
            si.fields[1] if len(si.fields) > 1 else "1",
            si.fields[2] if len(si.fields) > 2 else "0", conv)
        if res is None:
            return
        factor, offset = res
        for idx, _kind, w in self._widgets:
            if idx == 1 and isinstance(w, QLineEdit):
                w.setText(factor)
            elif idx == 2 and isinstance(w, QLineEdit):
                w.setText(offset)

    def _commit(self):
        # Preserve hidden field values; overwrite only the visible ones by index.
        schema = SCHEMA.get((self._line.key or "").split(".")[0], [])
        fields = list(self._line.fields)
        while len(fields) < len(schema):
            fields.append(cf.PLACEHOLDER)
        for idx, _kind, w in self._widgets:
            if isinstance(w, QComboBox):
                data = w.currentData()
                txt = w.currentText()
                fields[idx] = data if data is not None and w.findText(txt) >= 0 else txt
            else:
                fields[idx] = w.text()
        self._line.set_fields(fields)
        self._on_change()

    def _commit_raw(self):
        _idx, _kind, w = self._widgets[0]
        self._line.set_fields(cf.split_fields(w.text()))
        self._on_change()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cax: cf.CaxFile | None = None
        self.path: str | None = None
        self.version: str = appsettings.working_format()
        self._dirty = False
        # User-created empty folders: set of (seg_label, major, minor).
        self.folders: set[tuple[str, str, str]] = set()
        self.bin_path = None
        _OPEN_WINDOWS.append(self)
        self._build_ui()
        self._update_title()

    # ---- UI scaffold ---------------------------------------------------------
    def _build_ui(self):
        self.resize(1480, 820)
        splitter = QSplitter(self)
        self.setCentralWidget(splitter)

        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(4, 4, 4, 4)
        self.filter = QLineEdit(placeholderText="Search…")
        self.filter.textChanged.connect(self._apply_filter)
        lv.addWidget(self.filter)
        self.tree = CalTreeView()
        self.tree.drop_handler = self._on_tree_drop
        self.tree.setModel(QStandardItemModel())
        self.tree.selectionModel().selectionChanged.connect(self._on_select)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._tree_menu)
        del_sc = QShortcut(QKeySequence.Delete, self.tree)
        del_sc.activated.connect(self._delete_selected)
        copy_sc = QShortcut(QKeySequence.Copy, self.tree)
        copy_sc.activated.connect(self._copy_selected)
        paste_sc = QShortcut(QKeySequence.Paste, self.tree)
        paste_sc.activated.connect(lambda: self._paste_cals())
        rename_sc = QShortcut(QKeySequence(Qt.Key_F2), self.tree)
        rename_sc.activated.connect(self._rename_selected)
        lv.addWidget(self.tree)
        splitter.addWidget(left)

        self.detail = QScrollArea()
        self.detail.setWidgetResizable(True)
        self._set_detail_placeholder("Open a .cax file to begin.")
        splitter.addWidget(self.detail)

        # Hex view of the matching bin (right pane).
        self.hex = HexView()
        self.hex.addressPicked.connect(self._set_addr_from_hex)
        self.hex.referencesRequested.connect(self._find_references)
        self.hex.createCalRequested.connect(self._create_cal_from_hex)
        splitter.addWidget(self.hex)
        splitter.setSizes([320, 480, 460])
        self._current_section = None

        self._build_menu()
        self._build_toolbar()
        self.status = self.statusBar()

    def _build_toolbar_refresh(self):
        if getattr(self, "_toolbar", None) is not None:
            self.removeToolBar(self._toolbar)
            self._toolbar.deleteLater()
            self._toolbar = None
        self._build_toolbar()

    def _build_toolbar(self):
        tb = QToolBar("Launch")
        self.addToolBar(tb)
        ip = QFileIconProvider()
        # Reload the current file from disk (discards unsaved edits after confirm).
        reload_act = QAction(self.style().standardIcon(QStyle.SP_BrowserReload),
                             "Reload", self)
        reload_act.setToolTip("Reload the open file from disk (Ctrl+R)")
        reload_act.triggered.connect(self.reload_file)
        tb.addAction(reload_act)
        tb.addSeparator()
        for ver, label in (("v7", "EFILive V7.5 Tune"), ("v8", "EFILive V8 Scan&&Tune")):
            exe = appsettings.efilive_exe(ver)
            if exe and os.path.exists(exe):
                icon = ip.icon(QFileInfo(exe))
            else:
                icon = self.style().standardIcon(QStyle.SP_MediaPlay)
            a = QAction(icon, label, self)
            a.setToolTip(f"Launch {label} with the loaded bin")
            a.triggered.connect(lambda _c=False, v=ver: self.launch_efilive(v))
            tb.addAction(a)
        # Open the .cax / .cax8 in the text editor.
        tb.addSeparator()
        editor = appsettings.text_editor()
        ed_icon = (ip.icon(QFileInfo(editor)) if editor and os.path.exists(editor)
                   else self.style().standardIcon(QStyle.SP_FileIcon))
        for ver, label in (("v7", "Edit .cax"), ("v8", "Edit .cax8")):
            a = QAction(ed_icon, label, self)
            a.setToolTip(f"Open the {ver.upper()} {'cax' if ver == 'v7' else 'cax8'} "
                         "in the text editor")
            a.triggered.connect(
                lambda _c=False, v=ver: self._open_in_editor_path(self._version_path(v)))
            tb.addAction(a)
        self._toolbar = tb

    def launch_efilive(self, ver: str):
        exe = appsettings.efilive_exe(ver)
        if not exe or not os.path.exists(exe):
            QMessageBox.information(
                self, APP_NAME, f"EFILive {ver.upper()} executable not found:\n{exe}\n\n"
                "Set the path in File ▸ Preferences.")
            return
        args = [exe]
        if getattr(self, "bin_path", None) and os.path.exists(self.bin_path):
            args.append(self.bin_path)
        try:
            subprocess.Popen(args)
            self.status.showMessage(
                f"Launched {os.path.basename(exe)}"
                + (f" with {os.path.basename(self.bin_path)}" if len(args) > 1 else ""))
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not launch EFILive.\n{e}")

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        for text, slot, sc in [
            ("&New file", self.new_file, "Ctrl+N"),
            ("New &window", self.new_window, "Ctrl+Shift+N"),
            ("&Open V8 file…", self.open_v8, "Ctrl+O"),
            ("Open &Legacy (V7) file…", self.open_legacy, "Ctrl+L"),
            ("&Save", self.save_file, "Ctrl+S"),
            ("Save &Versions (V7 + V8)", self.save_versions, "Ctrl+Shift+S"),
            ("&Reload from disk", self.reload_file, "Ctrl+R"),
        ]:
            a = QAction(text, self); a.setShortcut(sc); a.triggered.connect(slot)
            m.addAction(a)
        self._recent_menu = m.addMenu("Open &Recent")
        self._rebuild_recent_menu()
        ed = QAction("Open in &text editor", self); ed.setShortcut("Ctrl+E")
        ed.triggered.connect(self.open_in_editor); m.addAction(ed)
        e7 = QAction("Open .cax in text editor", self)
        e7.triggered.connect(lambda: self._open_in_editor_path(self._version_path("v7")))
        m.addAction(e7)
        e8 = QAction("Open .cax8 in text editor", self)
        e8.setShortcut("Ctrl+Shift+E")
        e8.triggered.connect(lambda: self._open_in_editor_path(self._version_path("v8")))
        m.addAction(e8)
        ob = QAction("Open &bin (hex view)…", self)
        ob.triggered.connect(self.open_bin); m.addAction(ob)
        m.addSeparator()
        # Per-version "Save as" — defaults to that version's configured folder.
        for ver in formats.VERSIONS.values():
            a = QAction(f"Save as {ver.label}…", self)
            a.triggered.connect(lambda _c=False, v=ver.key: self.save_as(v))
            m.addAction(a)
        dup = QAction("&Duplicate to other OS numbers…", self)
        dup.triggered.connect(self.duplicate_to_os)
        m.addAction(dup)
        m.addSeparator()
        pref = QAction("&Preferences…", self); pref.triggered.connect(self.preferences)
        m.addAction(pref)
        m.addSeparator()
        q = QAction("E&xit", self); q.triggered.connect(self.close); m.addAction(q)
        t = self.menuBar().addMenu("&Tools")
        va = QAction("&Verify against EFILive V8…", self)
        va.triggered.connect(self.verify_against_v8)
        t.addAction(va)
        ca = QAction("&Compare with another file (by address)…", self)
        ca.triggered.connect(self.compare_by_address)
        t.addAction(ca)
        sp = QAction("Verify &segment placement (by address)…", self)
        sp.triggered.connect(self.verify_segment_placement)
        t.addAction(sp)
        ro = QAction("Remove &overlapping calibrations (paste EFILive warnings)…", self)
        ro.triggered.connect(self.remove_overlaps_from_warnings)
        t.addAction(ro)
        su = QAction("Sanitize &units (to valid V8 units)…", self)
        su.triggered.connect(self.sanitize_units)
        t.addAction(su)
        t.addSeparator()
        ix = QAction("&Import from XDF (TunerPro)…", self)
        ix.triggered.connect(self.import_xdf)
        t.addAction(ix)
        ia = QAction("&Import from A2L (ASAP2)…", self)
        ia.triggered.connect(self.import_a2l)
        t.addAction(ia)
        ex = QAction("&Export to XDF (TunerPro)…", self)
        ex.triggered.connect(self.export_xdf)
        t.addAction(ex)
        ri = QAction("&Re-ID calibrations from templates…", self)
        ri.triggered.connect(self.reid_from_templates)
        t.addAction(ri)
        sm = QAction("Set ID &master from current file…", self)
        sm.triggered.connect(self.set_id_master)
        t.addAction(sm)
        st = QAction("&Standardize IDs to master (by name)…", self)
        st.triggered.connect(self.standardize_ids)
        t.addAction(st)
        sf = QAction("Standardize a &folder to master…", self)
        sf.triggered.connect(self.standardize_folder)
        t.addAction(sf)
        t.addSeparator()
        bk = QAction("&Back up cax/cax8 files now…", self)
        bk.triggered.connect(self.run_backup_now)
        t.addAction(bk)
        vm = self.menuBar().addMenu("&View")
        tm = vm.addMenu("&Theme")
        from PySide6.QtGui import QActionGroup
        self._theme_group = QActionGroup(self)
        cur = appsettings.theme()
        for name in themes.theme_names():
            a = QAction(name, self, checkable=True)
            a.setChecked(name == cur)
            a.triggered.connect(lambda _c=False, n=name: self._set_theme(n))
            self._theme_group.addAction(a)
            tm.addAction(a)

        h = self.menuBar().addMenu("&Help")
        a = QAction("&About", self); a.triggered.connect(self.about); h.addAction(a)

    def _set_theme(self, name: str):
        appsettings.set_theme(name)
        themes.apply_theme(QApplication.instance(), name)
        self.status.showMessage(f"Theme: {name}")

    # ---- file ops ------------------------------------------------------------
    def new_file(self):
        if not self._confirm_discard():
            return
        self.cax = cf.CaxFile()
        self.path = None
        self.version = appsettings.working_format()
        self._dirty = False
        self.folders.clear()
        self._reload_tree()
        self._update_title()
        self.status.showMessage(
            f"New {formats.VERSIONS[self.version].label} file")

    def open_v8(self):
        """Open a V8 .cax8, defaulting to the configured V8 folder."""
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open V8 file", appsettings.save_dir("v8"), "CAX8 Files (*.cax8)")
        if path:
            self.load(path)

    def open_legacy(self):
        """Open a legacy V7 .cax, defaulting to the configured V7 folder."""
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Open Legacy (V7) file", appsettings.save_dir("v7"),
            "CAX Files (*.cax)")
        if path:
            self.load(path)

    def load(self, path: str):
        is_v8 = path.lower().endswith(".cax8")
        try:
            self.cax = convert.load_v8(path) if is_v8 else cf.parse_file(path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Cannot open file for reading.\n{e}")
            return
        appsettings.set_last_open_dir(os.path.dirname(path))
        appsettings.add_recent_file(path)
        for w in _OPEN_WINDOWS:
            w._rebuild_recent_menu()
        self.path = path
        self.version = "v8" if is_v8 else formats.detect_version(self.cax)
        self._dirty = False
        self.folders.clear()
        self._current_section = None
        self._auto_load_bin()
        self._reload_tree()
        self._update_title()
        n_rec = sum(len(s.records()) for s in self.cax.sections)
        errs = self.cax.errors
        self.status.showMessage(f"{len(self.cax.sections)} sections · {n_rec} records · {len(errs)} bad")
        if errs:
            self._warn_bad_records(errs)

    # ---- bin / hex view ------------------------------------------------------
    def _find_bin_for(self, os_id: str):
        """Locate a bin matching the OS number in the configured bin folder."""
        roots = [appsettings.bin_dir(), segmap.BIN_DIR]
        for root in roots:
            if not root or not os.path.isdir(root):
                continue
            for dp, _dn, files in os.walk(root):
                for fn in files:
                    if fn.lower().endswith(".bin") and os_id and os_id in fn:
                        return os.path.join(dp, fn)
        return None

    def _load_bin(self, path: str):
        try:
            with open(path, "rb") as f:
                self.hex.set_data(f.read())
            self.bin_path = path
            self.status.showMessage(f"Bin: {os.path.basename(path)}")
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not read bin.\n{e}")

    def _auto_load_bin(self):
        """On opening a cax, try to find & load a matching bin for the hex view."""
        self.bin_path = None
        if not self.path:
            return
        os_id = os.path.splitext(os.path.basename(self.path))[0]
        p = self._find_bin_for(os_id)
        if p:
            self._load_bin(p)
        else:
            self.hex.set_data(b"")

    def open_bin(self):
        start = appsettings.bin_dir() or appsettings.last_open_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "Open bin for hex view", start, "BIN files (*.bin *.BIN);;All files (*)")
        if path:
            self._load_bin(path)

    def _set_addr_from_hex(self, offset: int):
        """Right-click 'Set calibration address' in the hex view."""
        if self._current_section is None:
            QMessageBox.information(self, APP_NAME, "Select a calibration first.")
            return
        addr = self._current_section.get("ADDR")
        if addr is None:
            return
        f = list(addr.fields)
        if f:
            f[0] = f"{offset:X}"
            addr.set_fields(f)
            self._on_edited()
            self._show_section(self._current_section)
            self.status.showMessage(f"Set {self._current_section.name} address to {offset:X}")

    # ---- hex: reference search + create-from-address -------------------------
    def _cal_byte_ranges(self, sec: cf.Section) -> list[tuple[int, int]]:
        """Byte ranges in the bin covered by a calibration's ADDR (one per
        comma-listed address; width = bytes × col span × row span)."""
        ar = sec.get("ADDR")
        if ar is None or not ar.fields:
            return []
        raw = ar.fields[0]
        if not raw or raw == cf.PLACEHOLDER:
            return []
        bits = 8
        if len(ar.fields) > 2 and str(ar.fields[2]).strip().isdigit():
            bits = int(ar.fields[2]) or 8
        cells = max(1, _label_count(sec.get("SI_COL"))) * \
            max(1, _label_count(sec.get("SI_ROW")))
        width = max(1, bits // 8) * cells
        out = []
        for part in str(raw).split(","):
            p = part.replace("$", "").strip()
            if not p:
                continue
            try:
                start = int(p, 16)
            except ValueError:
                continue
            out.append((start, start + width))
        return out

    def _find_references(self, lo: int, hi: int):
        """List calibrations whose address range overlaps [lo, hi); if none,
        offer to create one there."""
        if self.cax is None:
            return
        hits = []
        for sec in self.cax.sections:
            if any(s < hi and lo < e for s, e in self._cal_byte_ranges(sec)):
                hits.append(sec)
        if hits:
            names = "\n".join(
                f"  • [{s.name}]  {(ct.cal_from_section(s).caption if ct.cal_from_section(s) else '')}"
                for s in hits[:25])
            more = "" if len(hits) <= 25 else f"\n  …and {len(hits) - 25} more"
            box = QMessageBox(self)
            box.setWindowTitle(APP_NAME)
            box.setIcon(QMessageBox.Information)
            box.setText(f"{len(hits)} calibration(s) reference address {lo:X}:\n\n"
                        f"{names}{more}")
            goto_label = ("Go to calibration" if len(hits) == 1
                          else f"Go to {hits[0].name}")
            goto_btn = box.addButton(goto_label, QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Ok)
            box.setDefaultButton(goto_btn)
            box.exec()
            self.status.showMessage(
                f"{lo:X}: referenced by {len(hits)} calibration(s)")
            if box.clickedButton() is goto_btn:
                self._select_section_by_name(hits[0].name)
        else:
            r = QMessageBox.question(
                self, APP_NAME,
                f"No calibration references address {lo:X}.\n\n"
                f"Create a new calibration at {lo:X}?",
                QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                self._create_cal_from_hex(lo, hi)

    def _classify_seg(self, addr_hex: str) -> str:
        """V7 segment number for an address (from cached ranges), or '' if unknown."""
        if not self.path:
            return ""
        try:
            a = int(addr_hex, 16)
        except ValueError:
            return ""
        os_id = os.path.splitext(os.path.basename(self.path))[0]
        segs = segmap.cached_segments(os_id)
        if not segs:
            return ""
        phys = segmap.classify(a, segs)
        if not phys:
            return ""
        _num2phys, phys2num = self._seg_name_maps(segs)
        return phys2num.get(phys, "")

    def _create_cal_from_hex(self, lo: int, hi: int):
        """Create a new calibration at byte offset `lo` (right-click in hex)."""
        if self.cax is None:
            QMessageBox.information(self, APP_NAME, "Open a calibration file first.")
            return
        addr_hex = f"{lo:X}"
        seg_num = self._classify_seg(addr_hex)
        cid, ok = QInputDialog.getText(
            self, "New calibration at " + addr_hex,
            f"Calibration Id (segment {seg_num or '?'}):", text=self._auto_id(seg_num))
        cid = cid.strip()
        if not ok or not cid:
            return
        if self.cax.section(cid) is not None:
            QMessageBox.warning(self, APP_NAME, f"Calibration '{cid}' already exists.")
            return
        caption, ok2 = QInputDialog.getText(
            self, "New calibration at " + addr_hex, "Caption:")
        if not ok2:
            return
        seg_num = self._suggest_segment_for_address(addr_hex, seg_num)
        sec = self._make_section(cid, seg_num, caption.strip() or cid, "", "")
        av = sec.get("ADDR")
        if av:
            f = list(av.fields)
            f[0] = addr_hex
            bits = (hi - lo) * 8
            if len(f) > 2 and 8 <= bits <= 32:
                f[2] = str(bits)           # use the selection length as bit count
            av.set_fields(f)
        self.cax.sections.append(sec)
        cf.validate(self.cax)
        self._reload_tree()
        self._on_edited()
        self._show_section(sec)
        self.status.showMessage(f"Created {cid} at {addr_hex}")

    def _hex_goto_current(self, addr_hex: str):
        """ADDR group 'Go to' button: jump+highlight the calibration's bytes."""
        try:
            addr = int(str(addr_hex).replace("$", "").split(",")[0].strip(), 16)
        except ValueError:
            return
        span = 1
        sec = self._current_section
        if sec is not None:
            ar, col, row = sec.get("ADDR"), sec.get("SI_COL"), sec.get("SI_ROW")
            bits = 8
            if ar and len(ar.fields) > 2 and ar.fields[2].isdigit():
                bits = int(ar.fields[2])
            span = max(1, bits // 8) * max(1, _label_count(col)) * max(1, _label_count(row))
        self.hex.goto(addr, span)

    def open_in_editor(self):
        """Open the current file in the configured external text editor."""
        self._open_in_editor_path(self.path)

    def _version_path(self, ver: str):
        """Path to the V7 .cax or V8 .cax8 sibling of the open file."""
        if not self.path:
            return None
        stem = os.path.splitext(os.path.basename(self.path))[0]
        if ver == "v7":
            if self.path.lower().endswith(".cax"):
                return self.path
            return os.path.join(appsettings.save_dir("v7"), stem + ".cax")
        if self.path.lower().endswith(".cax8"):
            return self.path
        ctrl = segmod.controller_for_os(stem) or ""
        return os.path.join(appsettings.save_dir("v8"), ctrl, stem + ".cax8")

    def _open_in_editor_path(self, path):
        if not path or not os.path.exists(path):
            QMessageBox.information(
                self, APP_NAME,
                f"File not found:\n{path}\n\nFor the .cax8, run Save Versions first.")
            return
        editor = appsettings.text_editor()
        try:
            subprocess.Popen([editor, path])
            self.status.showMessage(f"Opened {os.path.basename(path)} in text editor")
        except OSError as e:
            QMessageBox.critical(
                self, APP_NAME,
                f"Could not launch text editor '{editor}'.\n{e}\n\n"
                "Set the editor exe in File ▸ Preferences.")

    def _warn_bad_records(self, errs):
        # Sort by line number so the user can work top-down through the file.
        ordered = sorted(errs, key=lambda e: e[1].lineno)
        lines = [f"line {ln.lineno}: [{name}] {ln.key or ''}: {'; '.join(ln.errors)}"
                 for name, ln in ordered[:40]]
        more = "" if len(errs) <= 40 else f"\n… and {len(errs) - 40} more"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            f"This file contains {len(errs)} incorrect record(s). Fix them at the "
            "line numbers below in a text editor and reopen — otherwise they will be "
            "lost on save.\n\nBAD RECORDS:\n" + "\n".join(lines) + more)
        box.addButton(QMessageBox.Ok)
        edit_btn = box.addButton("Edit in text editor", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is edit_btn:
            self.open_in_editor()

    def save_file(self):
        if self.cax is None:
            return
        if not self.path:
            return self.save_as(self.version)
        self._write(self.path, self.version)

    def reload_file(self):
        """Re-read the currently open file from disk, discarding unsaved edits
        (after confirmation)."""
        if not self.path:
            QMessageBox.information(self, APP_NAME, "No file is open to reload.")
            return
        if not os.path.exists(self.path):
            QMessageBox.warning(self, APP_NAME,
                                f"File no longer exists on disk:\n{self.path}")
            return
        if getattr(self, "_dirty", False) and not self._confirm_discard():
            return
        path = self.path
        self.load(path)
        self.status.showMessage(f"Reloaded {os.path.basename(path)}")

    def save_versions(self):
        """Save the working V7 .cax AND an auto-converted V8 .cax8 (with the
        learned V7->V8 segment remap) in one action."""
        if self.cax is None:
            return
        if self.version == "v8":
            QMessageBox.information(
                self, APP_NAME, "This file was opened as V8. Use Save (Ctrl+S) to "
                "write it back, or Save as Legacy V7 first.")
            return
        if not self.path:
            self.save_as("v7")
            if not self.path:
                return
        # 1) V7
        self._write(self.path, "v7")
        # 2) V8 conversion
        stem = os.path.splitext(os.path.basename(self.path))[0]
        controller = segmod.controller_for_os(stem)
        if controller is None:
            controller, ok = QInputDialog.getText(
                self, APP_NAME, f"Controller for OS {stem} (for the V8 file):")
            controller = controller.strip()
            if not ok or not controller:
                self.status.showMessage("Saved V7 only (no controller for V8).")
                return
        cmap = segmod.learned_controller_map(controller)
        if cmap is None:
            QMessageBox.warning(
                self, APP_NAME, f"No learned V7→V8 segment map for {controller}; "
                "the V8 segments may be wrong. Saving V7 only.")
            return
        v8dir = os.path.join(appsettings.save_dir("v8"), controller)
        try:
            os.makedirs(v8dir, exist_ok=True)
            v8path = os.path.join(v8dir, stem + ".cax8")
            from datetime import date
            provider = {"Version": "8.3.1.0", "Date": date.today().strftime("%Y.%m.%d"),
                        "Name": appsettings.v8_uid_name() or APP_NAME,
                        "UniqueID": appsettings.v8_unique_id()}
            rpt = convert.write_v8(self.cax, v8path, provider=provider,
                                   controller=controller, cmap=cmap)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"V7 saved, but V8 failed:\n{e}")
            return
        # 3) Linked OS numbers: build their V8 copies too (same layout/controller).
        # The linked V7 copies were already written by _write above; this mirrors
        # that for V8 so Save Versions keeps every linked file in sync.
        link = appsettings.os_link(stem)
        linked_done, linked_errs = [], []
        if link:
            for t in link["targets"]:
                if t == stem or not _valid_os_name(t):
                    continue
                try:
                    convert.write_v8(self.cax, os.path.join(v8dir, t + ".cax8"),
                                     provider=provider, controller=controller,
                                     cmap=cmap)
                    linked_done.append(t)
                except OSError as e:
                    linked_errs.append(f"{t}.cax8: {e}")
        note = f" ({len(rpt.review)} need review)" if rpt and rpt.review else ""
        msg = f"Saved both versions:\n\nV7: {self.path}\nV8: {v8path}{note}"
        if linked_done:
            msg += ("\n\nLinked V8 copies (in " + v8dir + "):\n  "
                    + "  ".join(t + ".cax8" for t in linked_done))
        if linked_errs:
            msg += "\n\nLinked V8 failed:\n  " + "\n  ".join(linked_errs)
        QMessageBox.information(self, APP_NAME, msg)
        self.status.showMessage(
            f"Saved V7 + V8 ({controller})"
            + (f" + {len(linked_done)} linked V8" if linked_done else ""))

    def save_as(self, version: str | None = None):
        if self.cax is None:
            return
        version = version or self.version
        fmt = formats.VERSIONS[version]
        if not fmt.implemented:
            QMessageBox.information(
                self, APP_NAME,
                f"Saving as {fmt.label} is not available yet — the {fmt.label} "
                "format still needs to be defined (provide a sample file).")
            return
        start = appsettings.save_dir(version)
        if self.path:
            stem = os.path.splitext(os.path.basename(self.path))[0]
            start = os.path.join(start, stem + fmt.ext)
        path, _ = QFileDialog.getSaveFileName(
            self, f"Save as {fmt.label}", start, fmt.filter)
        if not path:
            return
        if not path.lower().endswith(fmt.ext):
            path += fmt.ext
        self.path = path
        self.version = version
        self._write(path, version)

    def _write(self, path: str, version: str):
        try:
            formats.write(self.cax, path, version)
        except NotImplementedError as e:
            QMessageBox.information(self, APP_NAME, str(e))
            return
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Cannot open file for writing.\n{e}")
            return
        self._dirty = False
        self._update_title()
        msg = f"Saved {os.path.basename(path)} as {formats.VERSIONS[version].label}"
        n, errs = self._propagate_os_links(path, version)
        if n:
            msg += f" (+{n} linked OS cop{'y' if n == 1 else 'ies'})"
        self.status.showMessage(msg)
        if errs:
            QMessageBox.warning(
                self, APP_NAME,
                "The master was saved, but some linked OS copies failed:\n  "
                + "\n  ".join(errs))

    def _propagate_os_links(self, master_path: str, version: str):
        """If the just-saved file's OS number has linked OS numbers, write a copy of
        the current model under each. Returns (written_count, error_messages)."""
        stem = os.path.splitext(os.path.basename(master_path))[0]
        link = appsettings.os_link(stem)
        if not link:
            return 0, []
        fmt = formats.VERSIONS[version]
        dest_dir = link.get("dir") or os.path.dirname(master_path)
        if not os.path.isdir(dest_dir):
            return 0, [f"(destination folder missing: {dest_dir})"]
        n, errs = 0, []
        for t in link["targets"]:
            if t == stem or not _valid_os_name(t):
                continue
            try:
                formats.write(self.cax, os.path.join(dest_dir, t + fmt.ext), version)
                n += 1
            except (OSError, NotImplementedError) as e:
                errs.append(f"{t}{fmt.ext}: {e}")
        return n, errs

    def duplicate_to_os(self):
        """Write the current file's calibration layout out under one or more other
        OS numbers. EFILive identifies a .cax/.cax8 purely by its filename (the OS
        number) — nothing inside the file names the OS — so a custom OS that shares
        a factory OS's memory layout just needs the same file saved under the custom
        OS number. Includes any unsaved edits (re-serialized from the open model)."""
        if self.cax is None:
            return
        if not self.path:
            QMessageBox.information(
                self, APP_NAME, "Save the file first — its OS number comes from the "
                "filename, which duplication needs as the source.")
            return
        fmt = formats.VERSIONS[self.version]
        src_dir = os.path.dirname(self.path)
        src_stem = os.path.splitext(os.path.basename(self.path))[0]
        existing_link = appsettings.os_link(src_stem)

        dlg = QDialog(self)
        dlg.setWindowTitle("Duplicate to other OS numbers")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"Source: <b>{os.path.basename(self.path)}</b> ({fmt.label})<br>"
            "Enter the target OS numbers (one per line, or comma/space separated).<br>"
            "Each becomes a copy of this layout named <i>&lt;OS&gt;"
            f"{fmt.ext}</i>."))
        edit = QPlainTextEdit()
        edit.setPlaceholderText("02598977\n01250150")
        if existing_link:
            edit.setPlainText("\n".join(existing_link["targets"]))
        v.addWidget(edit, 1)

        v.addWidget(QLabel("Destination folder:"))
        drow = QWidget(); dh = QHBoxLayout(drow); dh.setContentsMargins(0, 0, 0, 0)
        dir_edit = QLineEdit((existing_link and existing_link.get("dir")) or src_dir)
        browse = QPushButton("Browse…")
        dh.addWidget(dir_edit, 1); dh.addWidget(browse)
        v.addWidget(drow)

        def _pick():
            d = QFileDialog.getExistingDirectory(
                dlg, "Destination folder", dir_edit.text() or src_dir)
            if d:
                dir_edit.setText(d)
        browse.clicked.connect(_pick)

        chk = QCheckBox(
            f"Auto-save these copies whenever {src_stem}{fmt.ext} is saved")
        chk.setChecked(existing_link is not None)
        v.addWidget(chk)

        brow = QWidget(); bh = QHBoxLayout(brow); bh.setContentsMargins(0, 0, 0, 0)
        ok = QPushButton("Create copies"); cancel = QPushButton("Cancel")
        bh.addStretch(1); bh.addWidget(cancel); bh.addWidget(ok)
        v.addWidget(brow)
        ok.setDefault(True)
        ok.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        dlg.resize(420, 340)
        if dlg.exec() != QDialog.Accepted:
            return

        dest_dir = dir_edit.text().strip() or src_dir
        remember = chk.isChecked()
        raw = edit.toPlainText().replace(",", " ").split()
        # Preserve order, drop duplicates and the source itself.
        targets, seen = [], set()
        for t in raw:
            if t in seen:
                continue
            seen.add(t)
            targets.append(t)

        bad = [t for t in targets if not _valid_os_name(t)]
        if bad:
            QMessageBox.warning(
                self, APP_NAME, "These are not usable OS numbers / filenames:\n  "
                + "  ".join(bad))
            return
        targets = [t for t in targets if t != src_stem]
        if not targets:
            # Nothing to copy. Still honour turning the link off.
            if existing_link and not remember:
                appsettings.remove_os_link(src_stem)
                self.status.showMessage(f"Removed OS link for {src_stem}")
            else:
                QMessageBox.information(
                    self, APP_NAME,
                    "No target OS numbers (other than the source) given.")
            return
        if not os.path.isdir(dest_dir):
            QMessageBox.warning(self, APP_NAME, f"Folder does not exist:\n{dest_dir}")
            return

        # Persist / clear the link. Store dir only when it differs from the
        # master's folder, so copies otherwise follow the master if it moves.
        if remember:
            appsettings.set_os_link(
                src_stem, targets, "" if dest_dir == src_dir else dest_dir)
        elif existing_link:
            appsettings.remove_os_link(src_stem)

        paths = {t: os.path.join(dest_dir, t + fmt.ext) for t in targets}
        existing = [t for t, p in paths.items() if os.path.exists(p)]
        if existing:
            r = QMessageBox.question(
                self, APP_NAME,
                "These already exist and will be overwritten:\n  "
                + "  ".join(t + fmt.ext for t in existing) + "\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if r != QMessageBox.Yes:
                return

        done, errs = [], []
        for t in targets:
            try:
                formats.write(self.cax, paths[t], self.version)
                done.append(t)
            except (OSError, NotImplementedError) as e:
                errs.append(f"{t}: {e}")
        msg = f"Created {len(done)} cop{'y' if len(done) == 1 else 'ies'} in "\
              f"{dest_dir}:\n  " + "  ".join(t + fmt.ext for t in done)
        if remember:
            msg += (f"\n\nLinked: these will now be re-saved automatically whenever "
                    f"{src_stem}{fmt.ext} is saved.")
        if errs:
            msg += "\n\nFailed:\n  " + "\n  ".join(errs)
        (QMessageBox.warning if errs else QMessageBox.information)(self, APP_NAME, msg)
        if done:
            self.status.showMessage(
                f"Duplicated {os.path.basename(self.path)} to {len(done)} OS number(s)"
                + (" (linked)" if remember else ""))

    def _seg_order(self):
        """V7 segment numbers ordered by their V8 number when 'V8' tree order is
        selected (else None = default V7 order). Names stay V7."""
        if appsettings.tree_segment_order() != "v8" or not self.path:
            return None
        stem = os.path.splitext(os.path.basename(self.path))[0]
        cmap = segmod.load_learned_segmap().get(segmod.controller_for_os(stem) or "", {})
        if not cmap:
            return None

        def key(n):
            v = cmap.get(n)
            return (int(v) if v and v.isdigit() else 99, int(n) if n.isdigit() else 99)
        return sorted(ct.SEGMENT_NAMES.keys(), key=key)

    def _current_controller(self) -> str | None:
        """Controller for the open file, from its OS-number filename."""
        if not self.path:
            return None
        stem = os.path.splitext(os.path.basename(self.path))[0]
        return segmod.controller_for_os(stem)

    def _seg_names(self) -> dict | None:
        """V8 per-controller segment names for the tree/authoring when editing a
        .cax8 natively; None (built-in V7 names) otherwise."""
        if self.version != "v8":
            return None
        ctrl = self._current_controller()
        names = segmod.v8_segment_names(ctrl) if ctrl else {}
        return names or None

    def _seg_number(self, label: str) -> str:
        """Resolve a tree branch label to a segment number, honouring V8 names."""
        return ct.seg_number(label, self._seg_names())

    # ---- tree ----------------------------------------------------------------
    def _reload_tree(self):
        model = QStandardItemModel()
        if self.cax is not None:
            root = ct.build_tree(self.cax, sorted(self.folders), self._seg_order(),
                                 seg_names=self._seg_names())
            for child in root.children.values():
                self._add_branch(model.invisibleRootItem(), child, depth=1)
        self.tree.setModel(model)
        self.tree.selectionModel().selectionChanged.connect(self._on_select)
        self.tree.expandToDepth(0)

    def _add_branch(self, parent: QStandardItem, branch: ct.Branch, depth=0,
                    seg="", major="", minor=""):
        item = QStandardItem(f"📁 {branch.label}")
        item.setEditable(False)
        # depth 1 = segment, 2 = major, 3 = minor
        if depth == 1:
            seg = branch.label
            kind = "seg"
        elif depth == 2:
            major = branch.label
            kind = "major"
        elif depth == 3:
            minor = branch.label
            kind = "minor"
        else:
            kind = "root"
        item.setData(kind, ROLE_KIND)
        item.setData(seg, ROLE_SEG)
        item.setData(major, ROLE_MAJOR)
        item.setData(minor, ROLE_MINOR)
        # Folders are drop targets but not draggable.
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDropEnabled)
        parent.appendRow(item)
        for child in branch.children.values():
            self._add_branch(item, child, depth + 1, seg, major, minor)
        for cal in sorted(branch.cals, key=lambda c: c.caption.lower()):
            leaf = QStandardItem(f"{ICON_GLYPH.get(cal.display, '▢')} {cal.caption}  [{cal.id}]")
            leaf.setData(cal.section.name, ROLE_SECTION)
            leaf.setData("leaf", ROLE_KIND)
            leaf.setData(seg, ROLE_SEG)        # leaf's folder context (for drop targets)
            leaf.setData(major, ROLE_MAJOR)
            leaf.setData(minor, ROLE_MINOR)
            # Calibrations are draggable; dropping onto one re-homes to its folder.
            leaf.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                          | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled)
            item.appendRow(leaf)
        return item

    def _on_tree_drop(self, target_item, names):
        """Drag calibrations onto a segment/folder/leaf -> set their VIEW
        Seg/Major/Minor to that location."""
        if self.cax is None or target_item is None:
            return
        kind = target_item.data(ROLE_KIND)
        seg_label = target_item.data(ROLE_SEG) or ""
        major = target_item.data(ROLE_MAJOR) or ""
        minor = target_item.data(ROLE_MINOR) or ""
        # Dropping on a bare segment clears the folder path; on a Major clears Minor.
        if kind == "seg":
            major = minor = ""
        elif kind == "major":
            minor = ""
        seg_num = self._seg_number(seg_label)
        moved = 0
        D = cf.PLACEHOLDER
        for name in names:
            sec = self.cax.section(name)
            view = sec.get("VIEW") if sec else None
            if not view:
                continue
            f = list(view.fields)
            while len(f) < 8:
                f.append(D)
            if seg_num:
                f[0] = seg_num
            f[2] = major          # empty -> "" (EFILive shows no folder; "." = a folder)
            f[3] = minor
            view.set_fields(f)
            moved += 1
        if moved:
            self._on_edited()
            self._reload_tree()
            self.status.showMessage(
                f"Moved {moved} calibration(s) to {seg_label}"
                + (f" / {major}" if major else "") + (f" / {minor}" if minor else ""))

    # ---- tree authoring: folders define Major/Minor --------------------------
    def _tree_menu(self, pos):
        global _FOLDER_CUT
        if self.cax is None:
            return
        idx = self.tree.indexAt(pos)
        item = self.tree.model().itemFromIndex(idx) if idx.isValid() else None
        kind = item.data(ROLE_KIND) if item else None
        seg = item.data(ROLE_SEG) if item else ""
        major = item.data(ROLE_MAJOR) if item else ""
        minor = item.data(ROLE_MINOR) if item else ""

        menu = QMenu(self)
        if kind == "seg":
            menu.addAction("New Major folder…",
                           lambda: self._new_folder(seg, "", ""))
            menu.addAction("New calibration here…",
                           lambda: self._new_cal(seg, "", ""))
            menu.addAction("Move segment's calibrations to segment…",
                           lambda: self._move_folder_to_segment(seg, "", ""))
        if kind == "major":
            menu.addAction("Rename folder…",
                           lambda: self._rename_folder(seg, major, ""))
            menu.addAction("New Minor sub-folder…",
                           lambda: self._new_folder(seg, major, ""))
            menu.addAction("New calibration here…",
                           lambda: self._new_cal(seg, major, ""))
            menu.addAction("Move folder to segment…",
                           lambda: self._move_folder_to_segment(seg, major, ""))
            menu.addAction("Cut folder",
                           lambda: self._cut_folder(seg, major, ""))
        elif kind == "minor":
            menu.addAction("Rename folder…",
                           lambda: self._rename_folder(seg, major, minor))
            menu.addAction("New calibration here…",
                           lambda: self._new_cal(seg, major, minor))
            menu.addAction("Move folder to segment…",
                           lambda: self._move_folder_to_segment(seg, major, minor))
            menu.addAction("Cut folder",
                           lambda: self._cut_folder(seg, major, minor))
        elif kind == "leaf":
            menu.addAction("Rename calibration…",
                           lambda: self._rename_cal(item.data(ROLE_SECTION)))
            menu.addAction("Move to segment…",
                           lambda: self._move_cals_to_segment(
                               self._selected_section_names()
                               or [item.data(ROLE_SECTION)]))
            menu.addAction("Copy calibration(s)", self._copy_selected)
            menu.addAction("Delete calibration",
                           lambda: self._delete_cals(self._selected_section_names()
                                                     or [item.data(ROLE_SECTION)]))

        # Paste options for folders and calibrations
        if _FOLDER_CUT or _CLIPBOARD:
            menu.addSeparator()
        if _FOLDER_CUT:
            paste_label = f"Paste folder ({_FOLDER_CUT.get('major', 'Major')})"
            if _FOLDER_CUT.get('minor'):
                paste_label = f"Paste folder ({_FOLDER_CUT.get('minor')} - sub-folder)"
            if kind in ("seg", "major"):
                menu.addAction(paste_label,
                              lambda: self._paste_folder(seg, major, minor))
        if _CLIPBOARD:
            menu.addAction(f"Paste {len(_CLIPBOARD)} calibration(s)",
                           lambda: self._paste_cals())

        if not menu.isEmpty():
            menu.exec(self.tree.viewport().mapToGlobal(pos))

    # ---- multi-file: copy/paste calibrations between open windows ------------
    def new_window(self):
        w = MainWindow()
        w.show()
        return w

    def _rebuild_recent_menu(self):
        self._recent_menu.clear()
        recents = appsettings.recent_files()
        self._recent_menu.setEnabled(bool(recents))
        for p in recents:
            a = QAction(os.path.basename(p), self)
            a.setToolTip(p)
            a.triggered.connect(lambda _c=False, path=p: self._open_recent(path))
            self._recent_menu.addAction(a)

    def _open_recent(self, path: str):
        if not os.path.exists(path):
            QMessageBox.information(self, APP_NAME, f"File no longer exists:\n{path}")
            return
        if self._confirm_discard():
            self.load(path)

    def _selected_section_names(self):
        names = []
        for idx in self.tree.selectionModel().selectedIndexes():
            it = self.tree.model().itemFromIndex(idx)
            if it and it.data(ROLE_SECTION):
                names.append(it.data(ROLE_SECTION))
        return names

    def _copy_selected(self):
        if self.cax is None:
            return
        names = self._selected_section_names()
        secs = [self.cax.section(n) for n in names]
        secs = [s for s in secs if s is not None]
        if not secs:
            return
        _CLIPBOARD.clear()
        _CLIPBOARD.extend(copy.deepcopy(s) for s in secs)
        self.status.showMessage(f"Copied {len(secs)} calibration(s) to clipboard")

    def _paste_cals(self):
        """Paste clipboard calibrations into THIS file, preserving their data;
        IDs that collide are auto-renamed. Works across windows."""
        if self.cax is None or not _CLIPBOARD:
            return
        existing = {s.name for s in self.cax.sections}
        pasted = 0
        for src in _CLIPBOARD:
            if src is None:
                continue
            sec = copy.deepcopy(src)
            cal = ct.cal_from_section(sec)
            sec.name = self._resolve_import_id(sec.name, cal.seg if cal else "", existing)
            existing.add(sec.name)
            for ln in sec.lines:           # ensure edits re-render on save
                if ln.is_record:
                    ln.edited = True
            self.cax.sections.append(sec)
            pasted += 1
        cf.validate(self.cax)
        self._reload_tree()
        self._on_edited()
        self.status.showMessage(f"Pasted {pasted} calibration(s)")

    def _delete_selected(self):
        """Delete all calibrations currently selected in the tree (Delete key)."""
        names = []
        for idx in self.tree.selectionModel().selectedIndexes():
            it = self.tree.model().itemFromIndex(idx)
            if it and it.data(ROLE_SECTION):
                names.append(it.data(ROLE_SECTION))
        if names:
            self._delete_cals(names)

    def _rename_selected(self):
        names = self._selected_section_names()
        if names:
            self._rename_cal(names[0])

    def _rename_cal(self, name):
        """Edit a calibration's Id (section name) and its caption (display name)."""
        if not name or self.cax is None:
            return
        sec = self.cax.section(name)
        if sec is None:
            return
        cal = ct.cal_from_section(sec)
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Rename calibration")
        dlg.resize(420, 130)
        lay = QFormLayout(dlg)
        id_edit = QLineEdit(name)
        cap_edit = QLineEdit(cal.caption if cal else "")
        lay.addRow("Calibration Id:", id_edit)
        lay.addRow("Caption (name):", cap_edit)
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        ok = QPushButton("Rename"); cancel = QPushButton("Cancel")
        h.addStretch(1); h.addWidget(cancel); h.addWidget(ok)
        lay.addRow(row)

        def apply():
            new_id = id_edit.text().strip()
            if not new_id:
                return
            if new_id != name and self.cax.section(new_id) is not None:
                QMessageBox.warning(self, APP_NAME,
                                    f"Calibration '{new_id}' already exists.")
                return
            sec.name = new_id
            view = sec.get("VIEW")
            if view:
                f = list(view.fields)
                while len(f) < 2:
                    f.append(cf.PLACEHOLDER)
                f[1] = cap_edit.text().strip() or new_id
                view.set_fields(f)
            self._on_edited()
            self._reload_tree()
            self._show_section(sec)
            self.status.showMessage(f"Renamed to {new_id}")
            dlg.close()
        ok.clicked.connect(apply)
        cancel.clicked.connect(dlg.close)
        id_edit.returnPressed.connect(apply)
        cap_edit.returnPressed.connect(apply)
        dlg.show()
        cap_edit.setFocus()

    def _rename_folder(self, seg: str, major: str, minor: str):
        """Rename a Major or Minor folder. Updates all calibrations in that folder."""
        if not seg or not major:
            return

        folder_type = "Minor" if minor else "Major"
        old_name = minor or major
        prompt = f"New {folder_type} folder name:"

        new_name, ok = QInputDialog.getText(self, f"Rename {folder_type} folder",
                                           prompt, text=old_name)
        new_name = new_name.strip()
        if not ok or not new_name or new_name == old_name:
            return

        # Check if the new folder already exists
        if (seg, major if not minor else new_name, new_name if not minor else minor) in self.folders:
            QMessageBox.warning(self, APP_NAME,
                              f"{folder_type} folder '{new_name}' already exists.")
            return

        # Update the folder in self.folders
        self.folders.discard((seg, major, minor))
        if not minor:
            # Renaming a major folder
            self.folders.add((seg, new_name, ""))
        else:
            # Renaming a minor folder
            self.folders.add((seg, major, new_name))

        # Update all calibrations in this folder to the new name
        if self.cax:
            for sec in self.cax.sections:
                view = sec.get("VIEW")
                if not view:
                    continue
                f = list(view.fields)
                while len(f) < 4:
                    f.append(cf.PLACEHOLDER)
                # Match calibrations that are in this folder
                cal_major = f[2] if len(f) > 2 else ""
                cal_minor = f[3] if len(f) > 3 else ""
                if not minor:
                    # Renaming major folder: update if cal is in this major
                    if cal_major == major:
                        f[2] = new_name
                        view.set_fields(f)
                else:
                    # Renaming minor folder: update if cal is in this major/minor
                    if cal_major == major and cal_minor == minor:
                        f[3] = new_name
                        view.set_fields(f)

        self._on_edited()
        self._reload_tree()
        self.status.showMessage(f"Renamed {folder_type} folder to '{new_name}'")

    def _delete_cals(self, names):
        names = [n for n in names if n]
        if not names or self.cax is None:
            return
        if len(names) == 1:
            sec = self.cax.section(names[0])
            cal = ct.cal_from_section(sec) if sec else None
            what = f"{cal.caption} [{names[0]}]" if cal else names[0]
        else:
            what = f"{len(names)} calibrations"
        if QMessageBox.question(
                self, APP_NAME, f"Delete {what}?\nThis cannot be undone (until you "
                "discard without saving).",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        drop = set(names)
        before = len(self.cax.sections)
        self.cax.sections = [s for s in self.cax.sections if s.name not in drop]
        if len(self.cax.sections) != before:
            self._on_edited()
            self._reload_tree()
            self._set_detail_placeholder("Calibration(s) deleted.")

    def remove_overlaps_from_warnings(self):
        """Paste EFILive's 'overlaps ...' warnings; remove the named user
        calibrations so the file loads cleanly.

        - Overlaps with a built-in EFILive calibration: the user cal is removed
          (checkbox list, pre-checked).
        - Overlaps between two of the user's own calibrations: only one may be
          removed. The user chooses which to keep, and the kept calibration
          absorbs the removed one's description text (so nothing is lost)."""
        if self.cax is None or not self.cax.sections:
            QMessageBox.information(self, APP_NAME, "Open a file first.")
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "Remove overlapping calibrations",
            "Paste the EFILive overlap warning lines:", "")
        if not ok or not text.strip():
            return
        efilive, pairs = parse_overlap_warnings(text)
        if not efilive and not pairs:
            QMessageBox.information(
                self, APP_NAME, "No calibration IDs found in the pasted text.\n"
                "Expected lines like 'User calibration: TC0001 overlaps ...'.")
            return
        present = {s.name for s in self.cax.sections}

        def _cap(cal_id):
            cal = ct.cal_from_section(self.cax.section(cal_id))
            return cal.caption if cal else ""

        # A pair with only one side present in the file can't be a keep-choice;
        # fall back to removing the present side. Split accordingly.
        simple = list(efilive)          # ids removed outright (checkbox list)
        pair_rows = []                  # (a, b) both present -> keep-which choice
        missing = [i for i in efilive if i not in present]
        for a, b in pairs:
            if a in present and b in present:
                pair_rows.append((a, b))
            elif a in present:
                simple.append(a); missing.append(b)
            elif b in present:
                simple.append(b); missing.append(a)
            else:
                missing.extend((a, b))
        simple = [i for i in dict.fromkeys(simple) if i in present]

        dlg = QDialog(self)
        dlg.setWindowTitle("Remove overlapping calibrations")
        v = QVBoxLayout(dlg)

        # Master switch: ignore every user-vs-user overlap in one click.
        ignore_chk = None
        if pair_rows:
            ignore_chk = QCheckBox(
                "Ignore all overlaps between two of my calibrations "
                "(remove only EFILive overlaps)")
            f = ignore_chk.font(); f.setBold(True); ignore_chk.setFont(f)
            v.addWidget(ignore_chk)

        lst = None
        if simple:
            v.addWidget(QLabel(
                "<b>Overlaps a built-in EFILive calibration</b> — these user "
                "calibrations will be removed (uncheck any to keep):"))
            lst = QListWidget()
            for i in simple:
                it = QListWidgetItem(f"{i}    {_cap(i)}")
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked)
                it.setData(Qt.UserRole, i)
                lst.addItem(it)
            v.addWidget(lst, 1)

        pair_widgets = []               # (a, b, combo, merge_chk)
        pair_boxes = []                 # the QGroupBoxes, for the master switch
        if pair_rows:
            pair_lbl = QLabel(
                "<b>Two of your calibrations overlap</b> — choose which to keep. "
                "The other is removed and its description merged into the kept one:")
            v.addWidget(pair_lbl)
            for a, b in pair_rows:
                gb = QGroupBox(f"{a} ({_cap(a)})  ⟷  {b} ({_cap(b)})")
                gl = QVBoxLayout(gb)
                combo = QComboBox()
                combo.addItem(f"Keep {a} — remove {b}", (a, b))
                combo.addItem(f"Keep {b} — remove {a}", (b, a))
                combo.addItem("Skip (leave both)", None)
                merge_chk = QCheckBox("Merge removed calibration's description "
                                      "into the kept one")
                merge_chk.setChecked(True)
                combo.currentIndexChanged.connect(
                    lambda _i, c=combo, m=merge_chk:
                    m.setEnabled(c.currentData() is not None))
                gl.addWidget(combo); gl.addWidget(merge_chk)
                v.addWidget(gb)
                pair_widgets.append((a, b, combo, merge_chk))
                pair_boxes.append(gb)
        if ignore_chk is not None:
            def _toggle_ignore(on, _boxes=pair_boxes, _lbl=pair_lbl):
                _lbl.setEnabled(not on)
                for box in _boxes:
                    box.setEnabled(not on)
            ignore_chk.toggled.connect(_toggle_ignore)

        if missing:
            v.addWidget(QLabel(
                f"<i>Not in this file (ignored): {', '.join(dict.fromkeys(missing))}</i>"))

        brow = QWidget(); bh = QHBoxLayout(brow); bh.setContentsMargins(0, 0, 0, 0)
        okb = QPushButton("Apply"); cancel = QPushButton("Cancel")
        bh.addStretch(1); bh.addWidget(cancel); bh.addWidget(okb)
        v.addWidget(brow)
        okb.setDefault(True)
        okb.clicked.connect(dlg.accept); cancel.clicked.connect(dlg.reject)
        dlg.resize(560, 460)
        if lst is None and not pair_rows:
            QMessageBox.information(
                self, APP_NAME, "None of the named calibrations are in this file.")
            return
        if dlg.exec() != QDialog.Accepted:
            return

        remove, merges = set(), []       # merges: (kept_id, removed_id)
        if lst is not None:
            for i in range(lst.count()):
                if lst.item(i).checkState() == Qt.Checked:
                    remove.add(lst.item(i).data(Qt.UserRole))
        ignore_pairs = ignore_chk.isChecked() if ignore_chk is not None else False
        for a, b, combo, merge_chk in pair_widgets:
            if ignore_pairs:
                continue
            data = combo.currentData()
            if data is None:
                continue
            kept, removed = data
            remove.add(removed)
            if merge_chk.isChecked():
                merges.append((kept, removed))
        if not remove:
            return

        # Merge descriptions before removing (source sections must still exist).
        merged = 0
        for kept, removed in merges:
            if kept in remove:
                continue  # kept is itself being removed elsewhere; skip merge
            dst, src = self.cax.section(kept), self.cax.section(removed)
            if dst and src:
                merged += cf.merge_descriptions(dst, src)

        before = len(self.cax.sections)
        self.cax.sections = [s for s in self.cax.sections if s.name not in remove]
        removed_n = before - len(self.cax.sections)
        if removed_n:
            self._on_edited()
            self._reload_tree()
            self._set_detail_placeholder(
                f"Removed {removed_n} overlapping calibration(s)"
                + (f"; merged {merged} description paragraph(s)." if merged else "."))
        note = f" (+{merged} desc merged)" if merged else ""
        self.status.showMessage(
            f"Removed {removed_n} overlapping calibration(s){note}")

    def _new_folder(self, seg: str, major: str, minor: str):
        """Create an empty sub-folder. Its name becomes Major (or Minor)."""
        prompt = "Major folder name:" if not major else "Minor sub-folder name:"
        name, ok = QInputDialog.getText(self, "New folder", prompt)
        name = name.strip()
        if not ok or not name:
            return
        if not major:
            self.folders.add((seg, name, ""))
        else:
            self.folders.add((seg, major, name))
        self._reload_tree()
        self._on_edited()

    def _new_cal(self, seg: str, major: str, minor: str):
        """Create a calibration under this folder; folder path fills VIEW fields."""
        seg_num = self._seg_number(seg)
        suggested = self._auto_id(seg_num)
        cid, ok = QInputDialog.getText(
            self, "New calibration",
            f"Calibration Id (auto-generated for segment {seg_num or '?'}):",
            text=suggested)
        cid = cid.strip()
        if not ok or not cid:
            return
        if self.cax.section(cid) is not None:
            QMessageBox.warning(self, APP_NAME, f"Calibration '{cid}' already exists.")
            return
        caption, _ = QInputDialog.getText(self, "New calibration", "Caption:")
        addr, _ = QInputDialog.getText(self, "New calibration",
                                       "Address (hex, optional):")
        addr = addr.strip().lstrip("$")
        # Suggest the correct segment from the address (if enabled + ranges cached).
        seg_num = self._suggest_segment_for_address(addr, seg_num)
        sec = self._make_section(cid, seg_num, caption.strip() or cid, major, minor)
        if addr:
            av = sec.get("ADDR")
            if av:
                f = list(av.fields); f[0] = addr; av.set_fields(f)
        self.cax.sections.append(sec)
        cf.validate(self.cax)
        self._reload_tree()
        self._on_edited()
        self._show_section(sec)

    def _cut_folder(self, seg: str, major: str, minor: str):
        """Mark a folder as cut (for later paste)."""
        global _FOLDER_CUT
        _FOLDER_CUT = {"seg": seg, "major": major, "minor": minor}
        folder_type = "Minor" if minor else "Major"
        folder_name = minor or major
        self.status.showMessage(f"Cut {folder_type} folder '{folder_name}'")

    def _paste_folder(self, target_seg: str, target_major: str, target_minor: str):
        """Paste a cut folder. If major folder pasted into major folder, convert to minor."""
        global _FOLDER_CUT
        if not _FOLDER_CUT:
            return

        src_seg = _FOLDER_CUT["seg"]
        src_major = _FOLDER_CUT["major"]
        src_minor = _FOLDER_CUT["minor"]

        if src_seg != target_seg:
            QMessageBox.warning(self, APP_NAME,
                              "Cannot move folders between segments.")
            return

        # If pasting a major folder into a major folder, convert source to minor
        if src_major and target_major and not target_minor and not src_minor:
            # Moving major "A" into major "B" → becomes minor under B
            dest_minor = src_major
            dest_major = target_major
        else:
            # Otherwise, just move the folder as-is (replacing target)
            dest_major = src_major if src_major else target_major
            dest_minor = src_minor if src_minor else target_minor

        # Remove source folder
        self.folders.discard((src_seg, src_major, src_minor))

        # Update all calibrations in the source folder to the new location
        src_seg_num = self._seg_number(src_seg)
        if self.cax:
            for sec in self.cax.sections:
                view = sec.get("VIEW")
                if not view:
                    continue
                f = list(view.fields)
                while len(f) < 8:
                    f.append(cf.PLACEHOLDER)
                # If calibration was in source folder, move it (f[0] is the
                # segment NUMBER already; compare against the number directly).
                cur_seg = "" if f[0] == cf.PLACEHOLDER else f[0]
                if cur_seg == src_seg_num:
                    if f[2] == src_major and f[3] == src_minor:
                        f[2] = dest_major or ""
                        f[3] = dest_minor or ""
                        view.set_fields(f)

        # Add destination folder
        self.folders.add((target_seg, dest_major, dest_minor))

        _FOLDER_CUT = None
        self._reload_tree()
        self._on_edited()
        folder_type = "Minor" if dest_minor else "Major"
        folder_name = dest_minor or dest_major
        self.status.showMessage(f"Moved folder to {folder_type} '{folder_name}'")

    def _pick_segment(self, title: str, exclude_label: str = "") -> str | None:
        """Ask the user for a target segment. Returns the segment NUMBER, or None
        if cancelled. Offers the known segments (skipping `exclude_label`)."""
        seg_names = self._seg_names() or ct.SEGMENT_NAMES
        pairs = [(num, name) for num, name in seg_names.items()
                 if name != exclude_label]
        if not pairs:
            return None
        items = [f"{num} · {name}" for num, name in pairs]
        choice, ok = QInputDialog.getItem(self, APP_NAME, title, items, 0, False)
        if not ok or not choice:
            return None
        return choice.split(" · ", 1)[0]

    def _move_cals_to_segment(self, names):
        """Change the segment (VIEW.Seg) of the given calibrations, keeping their
        Major/Minor folder path."""
        names = [n for n in names if n]
        if not names or self.cax is None:
            return
        seg_num = self._pick_segment(
            f"Move {len(names)} calibration(s) to segment:")
        if seg_num is None:
            return
        moved = 0
        for name in names:
            sec = self.cax.section(name)
            view = sec.get("VIEW") if sec else None
            if not view:
                continue
            f = list(view.fields)
            while len(f) < 8:
                f.append(cf.PLACEHOLDER)
            f[0] = seg_num
            view.set_fields(f)
            moved += 1
        if moved:
            self._on_edited()
            self._reload_tree()
            self.status.showMessage(
                f"Moved {moved} calibration(s) to {ct.seg_label(seg_num, self._seg_names())}")

    def _move_folder_to_segment(self, seg: str, major: str, minor: str):
        """Move every calibration in a folder (or a whole segment when major=="")
        to another segment, preserving the Major/Minor path. Also re-homes any
        empty user-created folders in that branch."""
        if self.cax is None:
            return
        src_num = self._seg_number(seg)
        what = minor or major or seg
        target = self._pick_segment(f"Move '{what}' to segment:", exclude_label=seg)
        if target is None or target == src_num:
            return
        is_minor = bool(minor)
        moved = 0
        for sec in self.cax.sections:
            view = sec.get("VIEW")
            if not view:
                continue
            f = list(view.fields)
            while len(f) < 8:
                f.append(cf.PLACEHOLDER)
            cur_seg = "" if f[0] == cf.PLACEHOLDER else f[0]
            if cur_seg != src_num:
                continue
            cal_major = "" if f[2] == cf.PLACEHOLDER else f[2]
            cal_minor = "" if f[3] == cf.PLACEHOLDER else f[3]
            if not major:                         # whole-segment move
                match = True
            elif is_minor:                        # a specific Minor sub-folder
                match = cal_major == major and cal_minor == minor
            else:                                 # a Major folder (any Minor under it)
                match = cal_major == major
            if match:
                f[0] = target
                view.set_fields(f)
                moved += 1
        # Re-home empty folder registry entries in the moved branch.
        target_label = ct.seg_label(target, self._seg_names())
        for entry in list(self.folders):
            e_seg, e_major, e_minor = entry
            if e_seg != seg:
                continue
            if not major:
                keep = True
            elif is_minor:
                keep = e_major == major and e_minor == minor
            else:
                keep = e_major == major
            if keep:
                self.folders.discard(entry)
                self.folders.add((target_label, e_major, e_minor))
        self._on_edited()
        self._reload_tree()
        self.status.showMessage(
            f"Moved '{what}' ({moved} calibration(s)) to {target_label}")

    def _suggest_segment_for_address(self, addr: str, chosen_seg: str) -> str:
        """If the address falls in a different segment than chosen, offer to move
        it there. Uses cached authoritative ranges; no-op if disabled/unavailable."""
        if not addr or not appsettings.suggest_segment_enabled() or not self.path:
            return chosen_seg
        try:
            addr_int = int(addr, 16)
        except ValueError:
            return chosen_seg
        os_id = os.path.splitext(os.path.basename(self.path))[0]
        segs = segmap.cached_segments(os_id)
        if not segs:
            return chosen_seg
        phys = segmap.classify(addr_int, segs)
        if not phys:
            return chosen_seg
        num2phys, phys2num = self._seg_name_maps(segs)
        exp = phys2num.get(phys)
        if exp and exp != chosen_seg:
            r = QMessageBox.question(
                self, APP_NAME,
                f"Address {addr.upper()} is in the {phys} segment (V7 seg {exp}), but "
                f"you're adding it under segment {chosen_seg}.\n\nPlace it in {phys} "
                f"(seg {exp}) instead?",
                QMessageBox.Yes | QMessageBox.No)
            if r == QMessageBox.Yes:
                return exp
        return chosen_seg

    def _auto_id(self, seg_num: str) -> str:
        """Next free calibration ID for a segment, from its Preferences template."""
        template = appsettings.id_template(
            seg_num, idgen.DEFAULT_ID_TEMPLATES.get(seg_num, ""))
        if not template:
            return ""
        existing = {s.name for s in self.cax.sections} if self.cax else set()
        return idgen.next_id(existing, template, appsettings.id_start(seg_num)) or ""

    @staticmethod
    def _make_section(cid: str, seg: str, caption: str, major: str, minor: str) -> cf.Section:
        sec = cf.Section(cid)

        def rec(key, fields):
            ln = cf.Line(cf.LineKind.RECORD, "", key=key, fields=fields, edited=True)
            sec.lines.append(ln)
        D = cf.PLACEHOLDER
        vis = appsettings.view_default("visibility")
        disp = appsettings.view_default("display")
        f2d = appsettings.view_default("force2d")
        grp = appsettings.view_default("group")
        rec("DESC.001", [""])
        rec("VIEW", [seg or D, caption, major or "", minor or "", vis, disp, f2d, grp])
        rec("ADDR", [D, "0", "8", "UNM", "0"])
        rec("SI_SLOT", [D] * 8)
        rec("SI_ENUM", [D])
        rec("IM_ENUM", [D])
        rec("SI_COL", [D]); rec("IM_COL", [D])
        rec("SI_ROW", [D]); rec("IM_ROW", [D])
        rec("ULABELS", [D, D, D])
        return sec

    def _apply_filter(self, text: str):
        text = text.strip().lower()
        model = self.tree.model()

        def visit(item) -> bool:
            keep_self = False
            for r in range(item.rowCount()):
                child = item.child(r)
                child_keep = visit(child)
                self.tree.setRowHidden(r, item.index(), not child_keep)
                keep_self = keep_self or child_keep
            if item.data(ROLE_SECTION):  # leaf
                keep_self = (not text) or (text in item.text().lower())
            return keep_self or not text

        rootitem = model.invisibleRootItem()
        for r in range(rootitem.rowCount()):
            top = rootitem.child(r)
            keep = visit(top)
            self.tree.setRowHidden(r, rootitem.index(), not keep)
        if text:
            self.tree.expandAll()

    def _on_select(self, *_):
        idxs = self.tree.selectionModel().selectedIndexes()
        if not idxs:
            return
        name = self.tree.model().itemFromIndex(idxs[0]).data(ROLE_SECTION)
        if not name or self.cax is None:
            return
        sec = self.cax.section(name)
        if sec:
            self._show_section(sec)

    # ---- detail editor -------------------------------------------------------
    # metric (SI) -> imperial (IM) record pairs
    _SIIM_PAIRS = [("SI_SLOT", "IM_SLOT"), ("SI_ENUM", "IM_ENUM"),
                   ("SI_ROW", "IM_ROW"), ("SI_COL", "IM_COL")]

    def _is_mirrored(self, sec: cf.Section) -> bool:
        """True if every imperial record already equals its metric counterpart."""
        any_si = False
        for si, im in self._SIIM_PAIRS:
            s, m = sec.get(si), sec.get(im)
            if s is None or m is None:
                continue
            any_si = any_si or any(f != cf.PLACEHOLDER for f in s.fields)
            if s.fields != m.fields:
                return False
        return any_si

    def _mirror_si_to_im(self, sec: cf.Section):
        """Copy metric (SI) values into the imperial (IM) records."""
        for si, im in self._SIIM_PAIRS:
            s, m = sec.get(si), sec.get(im)
            if s is not None and m is not None:
                m.set_fields(list(s.fields))

    def _datatype_of(self, sec: cf.Section) -> str:
        addr = sec.get("ADDR")
        if addr and len(addr.fields) > 3:
            return (addr.fields[3] or "").strip().upper()
        return ""

    def _normalize_sed(self, sec: cf.Section) -> bool:
        """SED (enum) calibrations are unitless — force the SI/IM slot Units to
        'none'. The col/row axis Units become 'switch' when the SI_ENUM has
        multiple options (an enum selector), otherwise 'none' where an axis
        exists. Axis labels stay editable. Returns True if anything changed."""
        if self._datatype_of(sec) != "SED":
            return False
        changed = False
        for key in ("SI_SLOT", "IM_SLOT"):
            ln = sec.get(key)
            if ln is None or not ln.fields:
                continue
            if (ln.fields[0] or "") != "none":
                f = list(ln.fields)
                f[0] = "none"
                ln.set_fields(f)
                changed = True
        is_switch = convert.enum_option_count(sec.get("SI_ENUM")) >= 2
        for key in ("SI_COL", "IM_COL", "SI_ROW", "IM_ROW"):
            ln = sec.get(key)
            if ln is None or not ln.fields:
                continue
            if is_switch:
                target = "switch"          # selector — mark the axis a switch
            elif _label_count(ln) > 0:
                target = "none"            # real axis on a plain enum -> unitless
            else:
                continue                   # empty axis on a plain enum -> leave
            if (ln.fields[0] or "") != target:
                f = list(ln.fields)
                f[0] = target
                ln.set_fields(f)
                changed = True
        return changed

    def _on_datatype_changed(self, sec: cf.Section):
        if self._normalize_sed(sec):
            self._on_edited()
        # Re-render so the slot units show 'none' and lock/unlock correctly.
        # Deferred: _show_section rebuilds the detail pane, deleting the ADDR
        # combo whose currentTextChanged signal is still being emitted — doing it
        # inline crashes (deleting a widget from inside its own signal handler).
        QTimer.singleShot(0, lambda: self._show_section(sec)
                          if self._current_section is sec else None)

    def _show_section(self, sec: cf.Section, mirror_override=None):
        self._current_section = sec
        sed = self._datatype_of(sec) == "SED"
        if self._normalize_sed(sec):
            self._on_edited()
        host = QWidget()
        v = QVBoxLayout(host)
        v.addWidget(QLabel(f"<h3>[{sec.name}]</h3>"))

        mirror_on = self._is_mirrored(sec) if mirror_override is None else mirror_override
        if sed:
            # Enum: mirror is enforced (IM == SI), so units stay consistent.
            if not self._is_mirrored(sec):
                self._mirror_si_to_im(sec)
                self._on_edited()
            mirror_on = True
        has_pair = any(sec.get(si) and sec.get(im) for si, im in self._SIIM_PAIRS)
        mirror_cb = None
        if has_pair:
            mirror_cb = QCheckBox("Imperial (IM) units mirror Metric (SI) — copy the "
                                  "SI slots into IM and hide the IM slots")
            mirror_cb.setChecked(mirror_on)
            if sed:
                mirror_cb.setEnabled(False)
                mirror_cb.setToolTip("Enforced for SED (enum) calibrations.")
            else:
                mirror_cb.toggled.connect(lambda c: self._toggle_mirror(sec, c))
            v.addWidget(mirror_cb)

        # While mirror is on the IM records are hidden, so SI edits must be
        # propagated to IM live (slots, enums, axes) — otherwise the hidden IM
        # records keep stale values and the saved file isn't actually mirrored.
        if mirror_on:
            def change_cb(s=sec):
                self._mirror_si_to_im(s)
                self._on_edited()
        else:
            change_cb = self._on_edited

        v.addWidget(DescriptionEditor(sec, self._on_edited))
        im_keys = {im for _si, im in self._SIIM_PAIRS}
        for ln in sec.lines:
            if not ln.is_record or (ln.key or "").startswith("DESC"):
                continue
            base = (ln.key or "").split(".")[0]
            if base in im_keys and mirror_on:
                continue  # hidden — mirrored from metric (reduces scrolling)
            if base == "VIEW":
                v.addWidget(RecordGroup(ln, self._on_edited, collapsible=True,
                                        expanded=False,
                                        title="VIEW options (defaults — expand to customize)"))
            elif base == "ADDR":
                v.addWidget(RecordGroup(ln, self._on_edited,
                                        addr_goto=self._hex_goto_current,
                                        on_datatype=lambda s=sec: self._on_datatype_changed(s)))
            elif base == "SI_SLOT":
                v.addWidget(RecordGroup(ln, change_cb, units_locked=sed))
            elif base == "IM_SLOT":
                v.addWidget(RecordGroup(ln, change_cb,
                                        sibling_si=sec.get("SI_SLOT"),
                                        units_locked=sed))
            elif base in ("SI_COL", "IM_COL", "SI_ROW", "IM_ROW"):
                # Enum axis units are locked to 'none'; labels stay editable.
                v.addWidget(RecordGroup(ln, change_cb,
                                        units_locked=sed and _label_count(ln) > 0))
            elif base in ("SI_ENUM", "IM_ENUM"):
                v.addWidget(RecordGroup(ln, change_cb))
            else:
                v.addWidget(RecordGroup(ln, self._on_edited))
        v.addStretch(1)
        self.detail.setWidget(host)

    def _toggle_mirror(self, sec: cf.Section, checked: bool):
        if checked:
            self._mirror_si_to_im(sec)
        self._on_edited()
        self._show_section(sec, mirror_override=checked)  # rebuild to hide/show IM

    def _on_edited(self):
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _set_detail_placeholder(self, msg: str):
        w = QLabel(msg)
        w.setAlignment(Qt.AlignCenter)
        self.detail.setWidget(w)

    # ---- misc ----------------------------------------------------------------
    def verify_against_v8(self):
        if self.cax is None or not self.path:
            QMessageBox.information(self, APP_NAME, "Open a V7 .cax file first.")
            return
        start = appsettings.save_dir("v8")
        v8path, _ = QFileDialog.getOpenFileName(
            self, "Choose the EFILive V8 .cax8 to compare against",
            start, "CAX8 Files (*.cax8)")
        if not v8path:
            return
        try:
            rpt = verifymod.verify_paths(self.path, v8path)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not read files.\n{e}")
            return
        self._show_verify_report(rpt)

    def _show_verify_report(self, rpt: "verifymod.VerifyReport"):
        lines = [rpt.summary(), ""]
        if rpt.dropped:
            lines.append(f"DROPPED — in V7 but NOT in the V8 output ({len(rpt.dropped)}):")
            for did, cap, cause in rpt.dropped:
                lines.append(f"  - {did:10} {cap[:36]:36}  | {cause}")
            lines.append("")
        if rpt.added:
            lines.append(f"ADDED — in V8 but not V7 ({len(rpt.added)}): "
                         + ", ".join(rpt.added))
            lines.append("")
        if rpt.changed:
            from collections import Counter
            kinds = Counter(d.field for cd in rpt.changed for d in cd.diffs)
            lines.append(f"CHANGED fields across {len(rpt.changed)} cals: {dict(kinds)}")
            for cd in rpt.changed:
                ex = ", ".join(f"{d.field} {d.v7}->{d.v8}" for d in cd.diffs)
                lines.append(f"  {cd.cal_id}: {ex}")

        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Conversion verification")
        dlg.resize(820, 560)
        v = QVBoxLayout(dlg)
        head = QLabel(f"<b>{os.path.basename(rpt.v7_path)}</b> vs "
                      f"<b>{os.path.basename(rpt.v8_path)}</b>")
        v.addWidget(head)
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        dlg.show()

    # ---- authoritative segment verification (address vs bin segment table) ---
    def _segments_for_current(self):
        """Resolve segment ranges for the open file: cache -> bin (auto/ask) ->
        UniversalPatcher XML. Returns (segs, controller, os_id) or (None, ...)."""
        os_id = os.path.splitext(os.path.basename(self.path))[0]
        controller = segmod.controller_for_os(os_id)
        segs = segmap.cached_segments(os_id)
        if segs:
            return segs, controller, os_id
        if controller is None:
            controller, ok = QInputDialog.getText(
                self, APP_NAME, f"Could not auto-detect the controller for OS {os_id}.\n"
                "Enter the controller code (e.g. T42, E38):")
            controller = controller.strip()
            if not ok or not controller:
                return None, None, os_id
        # No exact ranges for this OS — reuse a cached sibling of the same
        # controller (boundaries are identical within many controllers, e.g. T42).
        sib_segs, sib_os = segmap.cached_segments_for_controller(controller)
        if sib_segs:
            self.status.showMessage(
                f"Using {controller} segment ranges from sibling OS {sib_os} "
                f"(no cached ranges/bin for {os_id})")
            return sib_segs, controller, os_id
        if not segmap.segment_pointers(controller):
            QMessageBox.warning(self, APP_NAME,
                                f"No segment definition for controller {controller}.")
            return None, controller, os_id
        binf = segmap.find_bin_for_os(os_id, controller)
        if not binf:
            binf, _ = QFileDialog.getOpenFileName(
                self, f"Select a {controller} bin for OS {os_id} (to read segment ranges)",
                segmap.BIN_DIR, "BIN files (*.bin *.BIN)")
            if not binf:
                return None, controller, os_id
        try:
            segs = segmap.resolve_ranges(controller, binf)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not read bin.\n{e}")
            return None, controller, os_id
        if segs:
            segmap.cache_segments(os_id, controller, segs)
        return segs, controller, os_id

    def _seg_name_maps(self, segs):
        """From the file's calibrations: V7 seg number -> majority physical name,
        and the inverse physical name -> V7 number."""
        from collections import Counter, defaultdict
        grp = defaultdict(Counter)
        for sec in self.cax.sections:
            cal = ct.cal_from_section(sec)
            if not cal:
                continue
            a = ct._addr_int(sec)
            if a is None:
                continue
            phys = segmap.classify(a, segs)
            if phys:
                grp[cal.seg][phys] += 1
        num2phys = {seg: c.most_common(1)[0][0] for seg, c in grp.items()}
        phys2num = {}
        for seg, phys in num2phys.items():
            phys2num.setdefault(phys, seg)
        # Fall back to EFILive's standard segment numbering for physical segments
        # the file has no (correctly-placed) calibration to learn from — otherwise
        # those mismatches can't be auto-assigned a target and stay un-fixable.
        canon_n2p = {"1": "OS", "2": "Engine", "3": "EngineDiag", "4": "Trans",
                     "5": "Diag", "6": "Fuel", "7": "System", "8": "Speedo"}
        num2phys = {**canon_n2p, **num2phys}                 # file majority wins
        phys2num = {**xdfimport.PHYS_TO_V7SEG, **phys2num}
        return num2phys, phys2num

    def verify_segment_placement(self):
        if self.cax is None or not self.path:
            QMessageBox.information(self, APP_NAME, "Open a file first.")
            return
        segs, controller, os_id = self._segments_for_current()
        if not segs:
            return
        # Use EFILive's standard physical-segment -> V7 number (canonical), not the
        # file's own majority — the file may itself be mis-assigned (the thing we're
        # fixing), which would otherwise propose perpetuating the error.
        phys2num = xdfimport.PHYS_TO_V7SEG
        # EFILive's own custom calibrations sit in an injected region that doesn't
        # map to the standard physical segments, so exclude them — they'd always
        # look "wrong" and just add noise.
        custom_seg = ct.seg_number("EFILive Custom Calibrations")
        mismatches, id_mismatches = [], []
        skipped_custom = 0
        reserved = {s.name for s in self.cax.sections}
        for sec in self.cax.sections:
            cal = ct.cal_from_section(sec)
            if not cal:
                continue
            if custom_seg and str(cal.seg) == custom_seg:
                skipped_custom += 1
                continue
            a = ct._addr_int(sec)
            if a is None:
                continue
            phys = segmap.classify(a, segs)
            if phys is None:
                mismatches.append((sec, cal, a, "(outside all segments)", None))
                continue
            correct = phys2num.get(phys)          # canonical V7 seg for the address
            if correct is not None and str(correct) != str(cal.seg):
                mismatches.append((sec, cal, a, phys, correct))
            # Does the ID prefix match the template prefix for its CORRECT segment?
            if correct is not None:
                tmpl = appsettings.id_template(
                    correct, idgen.DEFAULT_ID_TEMPLATES.get(correct, ""))
                parsed = idgen.parse_template(tmpl)
                if parsed and parsed[0] and not cal.id.startswith(parsed[0]):
                    prop = idgen.next_id(reserved, tmpl, appsettings.id_start(correct))
                    if prop:
                        reserved.add(prop)
                    id_mismatches.append((sec, cal, a, correct, parsed[0], prop))
        self._show_segverify_dialog(mismatches, id_mismatches, segs, controller,
                                    os_id, skipped_custom)

    def _show_segverify_dialog(self, mismatches, id_mismatches, segs, controller,
                               os_id, skipped_custom=0):
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Verify segment placement")
        dlg.resize(840, 620)
        v = QVBoxLayout(dlg)
        ranges = "  ·  ".join(f"{s.name} " +
                              ",".join(f"{lo:X}-{hi:X}" for lo, hi in s.ranges)
                              for s in segs)
        skipnote = (f"<br><small>Ignored {skipped_custom} calibration(s) in "
                    "EFILive Custom Calibrations.</small>" if skipped_custom else "")
        v.addWidget(QLabel(f"<b>{os_id}</b> · controller <b>{controller}</b><br>"
                           f"<small>{ranges}</small>{skipnote}"))
        if not mismatches and not id_mismatches:
            v.addWidget(QLabel("✓ Every calibration is in the right segment and its ID "
                               "matches the segment. Nothing to fix."))
            dlg.show()
            return

        # --- section 1: wrong segment (by address) ---
        seg_list = QListWidget()
        v.addWidget(QLabel(f"<b>Wrong segment ({len(mismatches)})</b> — address is in a "
                           "different segment than assigned:"))
        for sec, cal, a, phys, exp in mismatches:
            if exp is not None:
                label = (f"{cal.id:10} @{a:06X}  {cal.caption[:28]:28}  "
                         f"seg {cal.seg} → {exp} ({phys})")
            else:
                label = (f"{cal.id:10} @{a:06X}  {cal.caption[:28]:28}  "
                         f"in {phys} — set manually")
            it = QListWidgetItem(label)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if exp is not None else Qt.Unchecked)
            if exp is None:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            it.setData(Qt.UserRole, (sec, exp))
            seg_list.addItem(it)
        v.addWidget(seg_list)

        # --- section 2: ID prefix doesn't match segment ---
        id_list = QListWidget()
        v.addWidget(QLabel(f"<b>ID doesn't match segment ({len(id_mismatches)})</b> — "
                           "ID prefix differs from the segment's template:"))
        for sec, cal, a, correct, prefix, prop in id_mismatches:
            tgt = f"→ {prop}" if prop else f"(expects '{prefix}…', set a template)"
            it = QListWidgetItem(f"{cal.id:10} @{a:06X}  {cal.caption[:28]:28}  "
                                 f"seg {correct} expects '{prefix}'  {tgt}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if prop else Qt.Unchecked)
            if not prop:
                it.setFlags(it.flags() & ~Qt.ItemIsEnabled)
            it.setData(Qt.UserRole, (sec, prop))
            id_list.addItem(it)
        v.addWidget(id_list)

        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        apply = QPushButton("Apply checked fixes"); cancel = QPushButton("Close")
        h.addStretch(1); h.addWidget(cancel); h.addWidget(apply)
        v.addWidget(row)

        def do_apply():
            nseg = 0
            for i in range(seg_list.count()):
                it = seg_list.item(i)
                if it.checkState() != Qt.Checked:
                    continue
                sec, exp = it.data(Qt.UserRole)
                view = sec.get("VIEW")
                if exp is not None and view:
                    f = list(view.fields); f[0] = str(exp); view.set_fields(f)
                    nseg += 1
            # ID renames: two-phase to avoid transient collisions.
            renames = []
            for i in range(id_list.count()):
                it = id_list.item(i)
                if it.checkState() != Qt.Checked:
                    continue
                sec, prop = it.data(Qt.UserRole)
                if prop:
                    renames.append((sec, prop))
            for j, (sec, _p) in enumerate(renames):
                sec.name = f"~SV{j}~"
            for sec, prop in renames:
                sec.name = prop
            if nseg or renames:
                self._on_edited()
                self._reload_tree()
            QMessageBox.information(
                self, APP_NAME,
                f"Reassigned {nseg} segment(s), renamed {len(renames)} ID(s).")
            dlg.close()
        apply.clicked.connect(do_apply)
        cancel.clicked.connect(dlg.close)
        dlg.show()

    # ---- import TunerPro XDF -------------------------------------------------
    def import_xdf(self):
        """Pick which XDF definitions to import (a selectable picker, like the A2L
        importer), instead of importing every definition at once. The dialog owns
        file browse, parse, segment detection, selection, import + the report."""
        from xdf_picker_dialog import XdfPickerDialog
        dlg = XdfPickerDialog(self, cax=self.cax)
        if dlg.exec() != QDialog.Accepted:
            return
        # A fresh .cax is created inside the dialog when none was loaded.
        if self.cax is None:
            self.cax = dlg.cax
            self.version = "v7"
            self.folders.clear()
        cf.validate(self.cax)
        self._reload_tree()
        self._on_edited()

    def export_xdf(self):
        if self.cax is None or not self.cax.sections:
            QMessageBox.information(self, APP_NAME, "Open or import a file first.")
            return
        stem = os.path.splitext(os.path.basename(self.path))[0] if self.path else "export"
        start = os.path.join(appsettings.last_open_dir(), stem + ".xdf")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export to XDF", start, "XDF files (*.xdf)")
        if not path:
            return
        if not path.lower().endswith(".xdf"):
            path += ".xdf"
        try:
            n = xdfimport.export_xdf(self.cax, path, deftitle=stem)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not write XDF.\n{e}")
            return
        appsettings.set_last_open_dir(os.path.dirname(path))
        QMessageBox.information(self, APP_NAME,
                               f"Exported {n} definitions to\n{path}")

    def import_a2l(self):
        """Import selected calibrations from A2L (ASAP2) file."""
        if self.cax is None or not self.cax.sections:
            QMessageBox.information(self, APP_NAME, "Open or import a file first.")
            return

        import a2limport
        from a2l_picker_dialog import A2lPickerDialog
        import re

        # Get segment ranges for address validation
        stem = os.path.splitext(os.path.basename(self.path))[0] if self.path else ""
        parts = re.split(r"[_\-.]", stem)
        controller = parts[0].upper() if parts else ""
        m = re.search(r"\d{8}", stem)
        os_id = m.group(0) if m else ""
        segs = segmap.cached_segments(os_id) or \
               segmap.cached_segments_for_controller(controller)[0]

        # Launch picker dialog
        dialog = A2lPickerDialog(self, cax=self.cax, segments=segs)
        if dialog.exec() == QDialog.Accepted:
            cf.validate(self.cax)
            self._reload_tree()
            self._on_edited()
            QMessageBox.information(self, APP_NAME, "A2L import complete.")

    def run_backup_now(self):
        dest = appsettings.backup_dest()
        if not dest:
            QMessageBox.information(
                self, APP_NAME, "Set a Backup folder in File ▸ Preferences first.")
            return
        srcs = appsettings.backup_sources()
        src_txt = "\n".join(f"  {lbl}: {p}" for lbl, p in srcs)
        if QMessageBox.question(
                self, APP_NAME, f"Back up all .cax / .cax8 files to:\n{dest}\n\n"
                f"From:\n{src_txt}\n\nContinue?",
                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        log_lines = []
        stats = backupmod.run_backup(dest, srcs, log=log_lines.append)
        if stats.get("error"):
            QMessageBox.critical(self, APP_NAME, stats["error"])
            return
        QMessageBox.information(
            self, APP_NAME,
            f"Backup complete → {dest}\n\n{stats['copied']} copied, "
            f"{stats['skipped']} up-to-date, {stats['errors']} errors "
            f"({stats['bytes'] / 1024:.0f} KB).")
        self.status.showMessage(f"Backup: {stats['copied']} copied to {dest}")

    # ---- compare two files by address ---------------------------------------
    def compare_by_address(self):
        if self.cax is None or not self.path:
            QMessageBox.information(self, APP_NAME, "Open a file first (it becomes 'A').")
            return
        start = appsettings.last_open_dir()
        other, _ = QFileDialog.getOpenFileName(
            self, "Choose the second file to compare (B)",
            start, "Calibration files (*.cax *.cax8)")
        if not other:
            return
        try:
            rpt = addrcompare.compare(self.path, other)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not read files.\n{e}")
            return
        lines = [rpt.summary(), ""]
        if rpt.matched:
            lines.append(f"SAME ADDRESS, DIFFERENT DATA ({len(rpt.matched)}):")
            for m in rpt.matched:
                ds = ", ".join(f"{n}: {x!r}→{y!r}" for n, x, y in m.fields)
                lines.append(f"  @{m.addr:8} {m.a.cal_id}/{m.b.cal_id}: {ds}")
            lines.append("")
        if rpt.only_a:
            lines.append(f"ONLY IN A ({len(rpt.only_a)}):")
            for c in rpt.only_a:
                lines.append(f"  @{c.addr:8} {c.cal_id:10} {c.caption[:36]}")
        self._show_compare_dialog(rpt, other, lines)

    def _show_compare_dialog(self, rpt, other_path: str, lines: list[str]):
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Compare by address")
        dlg.resize(880, 640)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"<b>{os.path.basename(self.path)}</b> (A) vs "
                           f"<b>{os.path.basename(other_path)}</b> (B)"))
        text = QPlainTextEdit("\n".join(lines)); text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text, 2)

        v.addWidget(QLabel(f"<b>Only in B — import into A ({len(rpt.only_b)}):</b> "
                           "addresses not present in your open file"))
        lst = QListWidget()
        for c in rpt.only_b:
            it = QListWidgetItem(f"@{c.addr:8} {c.cal_id:12} {c.caption[:44]}")
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked)
            it.setData(Qt.UserRole, c.addr)
            lst.addItem(it)
        v.addWidget(lst, 3)

        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        selall = QPushButton("Select all"); selnone = QPushButton("Select none")
        imp = QPushButton(f"Import selected into A")
        h.addWidget(selall); h.addWidget(selnone); h.addStretch(1); h.addWidget(imp)
        v.addWidget(row)

        def set_all(state):
            for i in range(lst.count()):
                lst.item(i).setCheckState(state)
        selall.clicked.connect(lambda: set_all(Qt.Checked))
        selnone.clicked.connect(lambda: set_all(Qt.Unchecked))
        imp.clicked.connect(lambda: self._import_from_b(other_path, lst, dlg))
        if not rpt.only_b:
            lst.addItem("(nothing unique to B)")
            imp.setEnabled(False)
        dlg.show()

    def _import_from_b(self, path_b: str, lst: QListWidget, dlg):
        addrs = [lst.item(i).data(Qt.UserRole) for i in range(lst.count())
                 if lst.item(i).checkState() == Qt.Checked and lst.item(i).data(Qt.UserRole)]
        if not addrs:
            QMessageBox.information(self, APP_NAME, "No addresses selected.")
            return
        try:
            secs = addrcompare.import_sections(path_b, addrs)
        except OSError as e:
            QMessageBox.critical(self, APP_NAME, f"Could not read B.\n{e}")
            return
        existing = {s.name for s in self.cax.sections}
        imported = 0
        for sec in secs:
            cal = ct.cal_from_section(sec)
            new_id = self._resolve_import_id(sec.name, cal.seg if cal else "", existing)
            sec.name = new_id
            existing.add(new_id)
            self.cax.sections.append(sec)
            imported += 1
        cf.validate(self.cax)
        self._reload_tree()
        self._on_edited()
        QMessageBox.information(self, APP_NAME,
                               f"Imported {imported} parameter(s) into A.")
        dlg.close()

    def _resolve_import_id(self, src_id: str, seg: str, existing: set) -> str:
        """Keep B's ID if free; else auto-generate from the segment template;
        else suffix until unique."""
        if src_id not in existing:
            return src_id
        auto = self._auto_id(seg)
        if auto and auto not in existing:
            return auto
        n = 1
        while f"{src_id}_{n}" in existing:
            n += 1
        return f"{src_id}_{n}"

    # ---- standardize IDs across files by name (per controller) --------------
    def _current_controller(self):
        if not self.path:
            return None
        stem = os.path.splitext(os.path.basename(self.path))[0]
        ctrl = segmod.controller_for_os(stem)
        if ctrl is None:
            ctrl, ok = QInputDialog.getText(
                self, APP_NAME, f"Controller for OS {stem} (e.g. T42, E38):")
            ctrl = ctrl.strip() if ok else None
        return ctrl or None

    def set_id_master(self):
        if self.cax is None or not self.cax.sections:
            QMessageBox.information(self, APP_NAME, "Open a file first.")
            return
        ctrl = self._current_controller()
        if not ctrl:
            return
        if catalogmod.has_master(ctrl):
            if QMessageBox.question(
                    self, APP_NAME, f"A master already exists for {ctrl}. Replace it "
                    f"with {os.path.basename(self.path)}?",
                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
        n = catalogmod.build_from_master(self.cax, ctrl)
        QMessageBox.information(
            self, APP_NAME, f"Set {ctrl} ID master from "
            f"{os.path.basename(self.path)} — {n} calibration names catalogued.")

    def standardize_ids(self):
        if self.cax is None or not self.cax.sections:
            QMessageBox.information(self, APP_NAME, "Open a file first.")
            return
        ctrl = self._current_controller()
        if not ctrl:
            return
        if not catalogmod.has_master(ctrl):
            QMessageBox.information(
                self, APP_NAME, f"No ID master for {ctrl} yet. Open the curated "
                f"{ctrl} file and use 'Set ID master from current file' first.")
            return
        plan, new_names, flags = catalogmod.standardize(self.cax, ctrl)
        lines = [f"Controller {ctrl} — standardize "
                 f"{os.path.basename(self.path)} to the master:", "",
                 f"{len(plan)} renames, {len(new_names)} new names added, "
                 f"{len(flags)} cross-segment flags.", ""]
        if plan:
            lines.append("RENAMES (by name):")
            lines += [f"  {o:12} -> {n}" for _s, o, n in plan[:80]]
            lines.append("")
        if new_names:
            lines.append("NEW names (not in master — added to catalog):")
            lines += [f"  {nm}  -> {nid}" for nm, nid in new_names[:60]]
            lines.append("")
        if flags:
            lines.append("CROSS-SEGMENT — review (NOT renamed):")
            lines += [f"  {cid} '{nm}' is seg {cseg} here but seg {kseg} in master "
                      f"(={kid})" for cid, nm, cseg, kseg, kid in flags[:60]]

        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Standardize IDs to master")
        dlg.resize(720, 600)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"<b>{len(plan)}</b> calibrations will be renamed to match "
                           f"the {ctrl} master. Review, then Apply."))
        text = QPlainTextEdit("\n".join(lines)); text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        apply = QPushButton(f"Apply {len(plan)} renames"); cancel = QPushButton("Close")
        h.addStretch(1); h.addWidget(cancel); h.addWidget(apply)
        v.addWidget(row)

        def do_apply():
            if plan:
                catalogmod.apply_plan(plan)
                self._on_edited()
                self._reload_tree()
            dlg.close()
        apply.clicked.connect(do_apply)
        cancel.clicked.connect(dlg.close)
        dlg.show()

    def standardize_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Choose a folder of .cax files to standardize",
            appsettings.last_open_dir())
        if not folder:
            return
        rows = catalogmod.standardize_folder(folder, write=False)  # dry run
        if not rows:
            QMessageBox.information(self, APP_NAME, "No .cax files found in that folder.")
            return
        would = sum(1 for r in rows if r[3])
        lines = [f"{len(rows)} files scanned in {folder}. Controller resolved by "
                 "OS number; same name -> same ID per controller.", ""]
        lines += [f"  {fn:24} [{ctrl:6}] {status:16} "
                  f"{ren} renames, {new} new, {flg} flagged"
                  for fn, ctrl, status, ren, new, flg in rows]
        skips = [r for r in rows if r[2].startswith("skip")]
        if skips:
            lines += ["", "Skipped (need a master, or unknown controller): "
                      + ", ".join(r[0] for r in skips)]

        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Standardize folder to master")
        dlg.resize(780, 600)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"<b>{would}</b> file(s) would be changed. Review, then "
                           "Apply to rewrite the files (each backed up to .bak)."))
        text = QPlainTextEdit("\n".join(lines)); text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        apply = QPushButton(f"Apply to {would} file(s)"); cancel = QPushButton("Close")
        h.addStretch(1); h.addWidget(cancel); h.addWidget(apply)
        v.addWidget(row)

        def do_apply():
            res = catalogmod.standardize_folder(folder, write=True)
            written = sum(1 for r in res if r[2] == "written")
            QMessageBox.information(self, APP_NAME,
                                   f"Standardized and wrote {written} file(s) in {folder}.")
            dlg.close()
        apply.clicked.connect(do_apply)
        cancel.clicked.connect(dlg.close)
        dlg.show()

    # ---- bulk sanitize units ------------------------------------------------
    _UNIT_SLOT_KEYS = ("SI_SLOT", "IM_SLOT")

    def _scan_units(self):
        """Return (fixes, unknown): fixes = [(section, key, old, target)] that can
        be set to a valid V8 unit deterministically; unknown = [(section, key, old)]
        non-canonical units with no known mapping (likely user-defined).

        SED (enum) calibrations are normalized to their required units — slots to
        'none', and col/row axes to 'switch' (multi-option enum) or 'none' — so the
        same rule the editor applies per-calibration can be applied file-wide."""
        fixes, unknown = [], []
        if self.cax is None:
            return fixes, unknown
        for sec in self.cax.sections:
            if self._datatype_of(sec) == "SED":
                self._scan_sed_units(sec, fixes)
                continue
            for key in self._UNIT_SLOT_KEYS:
                ln = sec.get(key)
                if ln is None or not ln.fields:
                    continue
                old = (ln.fields[0] or "").strip()
                if not old or old == cf.PLACEHOLDER or unitsmod.is_canonical(old):
                    continue
                canon = unitsmod.sanitize(old)
                if canon and canon != old:
                    fixes.append((sec, key, old, canon))
                else:
                    unknown.append((sec, key, old))
        return fixes, unknown

    def _scan_sed_units(self, sec: cf.Section, fixes: list):
        """Append the SED unit-normalization fixes for one enum calibration."""
        for key in ("SI_SLOT", "IM_SLOT"):
            ln = sec.get(key)
            if ln is None or not ln.fields:
                continue
            old = (ln.fields[0] or "").strip()
            if old and old != cf.PLACEHOLDER and old != "none":
                fixes.append((sec, key, old, "none"))   # stray unit on an enum slot
        is_switch = convert.enum_option_count(sec.get("SI_ENUM")) >= 2
        for key in ("SI_COL", "IM_COL", "SI_ROW", "IM_ROW"):
            ln = sec.get(key)
            if ln is None or not ln.fields:
                continue
            if is_switch:
                target = "switch"
            elif _label_count(ln) > 0:
                target = "none"
            else:
                continue
            old = (ln.fields[0] or "").strip()
            if old != target:
                fixes.append((sec, key, old or cf.PLACEHOLDER, target))

    def _select_section_by_name(self, name: str) -> bool:
        """Select & scroll to the calibration leaf with this section name."""
        model = self.tree.model()
        if model is None:
            return False
        stack = [model.invisibleRootItem()]
        while stack:
            it = stack.pop()
            for r in range(it.rowCount()):
                child = it.child(r)
                if child.data(ROLE_SECTION) == name:
                    idx = child.index()
                    self.tree.setCurrentIndex(idx)
                    self.tree.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                    self.tree.setFocus()
                    return True
                stack.append(child)
        return False

    def sanitize_units(self):
        if self.cax is None:
            return
        fixes, unknown = self._scan_units()
        if not fixes and not unknown:
            QMessageBox.information(
                self, APP_NAME, "All SI/IM slot units are already valid V8 units.")
            return

        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Sanitize units — report (confirm before applying)")
        dlg.resize(760, 620)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"<b>{len(fixes)}</b> unit(s) map to a valid V8 unit; "
            f"<b>{len(unknown)}</b> have no known mapping."))

        if fixes:
            v.addWidget(QLabel("<b>Will be normalized:</b>"))
            fix_list = QListWidget()
            for sec, key, old, new in fixes:
                item = QListWidgetItem(f"[{sec.name}]  {key}   {old!r} → {new!r}")
                item.setData(Qt.UserRole, sec.name)
                fix_list.addItem(item)
            fix_list.itemActivated.connect(
                lambda it: self._jump_from_dialog(dlg, it.data(Qt.UserRole)))
            v.addWidget(fix_list)

        unk_none = QCheckBox("Set unmapped (unknown) units to “none” (unitless)")
        if unknown:
            v.addWidget(QLabel("<b>No mapping — double-click to open the "
                               "calibration:</b>"))
            unk_list = QListWidget()
            for sec, key, old in unknown:
                item = QListWidgetItem(f"[{sec.name}]  {key}   {old!r}")
                item.setData(Qt.UserRole, sec.name)
                unk_list.addItem(item)
            unk_list.itemActivated.connect(
                lambda it: self._jump_from_dialog(dlg, it.data(Qt.UserRole)))
            v.addWidget(unk_list)
            v.addWidget(unk_none)

        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        apply = QPushButton("Apply")
        cancel = QPushButton("Cancel")
        h.addStretch(1); h.addWidget(cancel); h.addWidget(apply)
        v.addWidget(row)

        def refresh_apply():
            n = len(fixes) + (len(unknown) if unk_none.isChecked() else 0)
            apply.setText(f"Apply {n} change(s)")
            apply.setEnabled(n > 0)
        unk_none.toggled.connect(lambda _c: refresh_apply())
        refresh_apply()

        def do_apply():
            changed = False
            for sec, key, _old, new in fixes:
                ln = sec.get(key)
                f = list(ln.fields); f[0] = new; ln.set_fields(f); changed = True
            if unk_none.isChecked():
                for sec, key, _old in unknown:
                    ln = sec.get(key)
                    f = list(ln.fields); f[0] = "none"; ln.set_fields(f); changed = True
            if changed:
                self._on_edited()
                if self._current_section is not None:
                    self._show_section(self._current_section)
            dlg.close()
        apply.clicked.connect(do_apply)
        cancel.clicked.connect(dlg.close)
        dlg.show()

    def _jump_from_dialog(self, dlg, name: str):
        """Navigate the main window to a calibration from a report dialog."""
        self.raise_()
        self.activateWindow()
        self._select_section_by_name(name)

    # ---- bulk re-ID from templates ------------------------------------------
    def reid_from_templates(self):
        if self.cax is None:
            return
        plan = self._build_reid_plan()
        if not plan:
            QMessageBox.information(
                self, APP_NAME, "No calibrations to re-ID. Set ID templates for the "
                "relevant segments in Preferences first.")
            return
        changes = [(s, o, n) for s, o, n in plan if o != n]
        report = self._reid_report(plan, changes)

        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Re-ID calibrations — report (confirm before applying)")
        dlg.resize(720, 600)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(f"<b>{len(changes)}</b> of {len(plan)} calibrations would be "
                           "renamed. Review below, then Apply or Cancel."))
        text = QPlainTextEdit("\n".join(report)); text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
        savebtn = QPushButton("Save report…")
        apply = QPushButton(f"Apply {len(changes)} renames")
        cancel = QPushButton("Cancel")
        h.addWidget(savebtn); h.addStretch(1); h.addWidget(cancel); h.addWidget(apply)
        v.addWidget(row)

        def do_save():
            start = appsettings.last_open_dir()
            base = os.path.splitext(os.path.basename(self.path or "reid"))[0]
            p, _ = QFileDialog.getSaveFileName(
                self, "Save re-ID report", os.path.join(start, base + "_reid_report.txt"),
                "Text files (*.txt);;CSV files (*.csv)")
            if not p:
                return
            try:
                with open(p, "w", encoding="utf-8") as fh:
                    if p.lower().endswith(".csv"):
                        fh.write("Segment,OldID,NewID,Address,Caption,Change\n")
                        fh.write("\n".join(self._reid_csv_rows(plan)))
                    else:
                        fh.write("\n".join(report))
            except OSError as e:
                QMessageBox.critical(self, APP_NAME, f"Could not save report.\n{e}")

        def do_apply():
            for sec, _o, new in changes:
                sec.name = new
            if changes:
                self._on_edited()
                self._reload_tree()
            dlg.close()
        savebtn.clicked.connect(do_save)
        apply.clicked.connect(do_apply)
        cancel.clicked.connect(dlg.close)
        dlg.show()

    def _reid_rows(self, plan):
        """Yield (seg, old, new, addr, caption, changed) for each planned cal."""
        for sec, old, new in plan:
            cal = ct.cal_from_section(sec)
            addr = ""
            ar = sec.get("ADDR")
            if ar and ar.fields and ar.fields[0] != cf.PLACEHOLDER:
                addr = ar.fields[0]
            yield (cal.seg if cal else "", old, new, addr,
                   cal.caption if cal else "", old != new)

    def _reid_report(self, plan, changes):
        lines = [f"Re-ID report for {os.path.basename(self.path or '(untitled)')}",
                 f"{len(changes)} of {len(plan)} calibrations will be renamed.", ""]
        for warn in getattr(self, "_reid_warnings", []):
            lines.append(f"⚠ {warn}")
        if getattr(self, "_reid_warnings", []):
            lines.append("")
        # group by segment for readability
        from itertools import groupby
        rows = sorted(self._reid_rows(plan), key=lambda r: (r[0], r[1]))
        for seg, grp in groupby(rows, key=lambda r: r[0]):
            lines.append(f"Segment {seg or '?'}  ({ct.seg_label(seg)}):")
            for _s, old, new, addr, cap, changed in grp:
                arrow = f"{old:12} -> {new:12}" if changed else f"{old:12}  (unchanged)"
                lines.append(f"  @{addr:8} {arrow}  {cap[:36]}")
            lines.append("")
        return lines

    def _reid_csv_rows(self, plan):
        def q(s):
            return '"' + s.replace('"', '""') + '"' if ("," in s or '"' in s) else s
        return [f"{seg},{old},{new},{addr},{q(cap)},{'rename' if ch else 'unchanged'}"
                for seg, old, new, addr, cap, ch in self._reid_rows(plan)]

    def _build_reid_plan(self):
        """List of (section, old_id, new_id) for cals whose segment has a template."""
        reidable = []
        for sec in self.cax.sections:
            cal = ct.cal_from_section(sec)
            if cal is None:
                continue
            tmpl = appsettings.id_template(
                cal.seg, idgen.DEFAULT_ID_TEMPLATES.get(cal.seg, ""))
            if tmpl:
                reidable.append((sec, cal.seg, tmpl))
        # Reserve IDs of sections we are NOT re-IDing, then assign sequentially.
        reidable_ids = {id(s) for s, _, _ in reidable}
        reserved = {s.name for s in self.cax.sections if id(s) not in reidable_ids}
        plan = []
        self._reid_warnings = []
        for sec, seg, tmpl in reidable:
            new = idgen.next_id(reserved, tmpl, appsettings.id_start(seg))
            if new is None:
                new = sec.name  # range exhausted -> leave unchanged + warn
                self._reid_warnings.append(
                    f"Segment {seg} template '{tmpl}' start {appsettings.id_start(seg)} "
                    f"exceeds its range — '{sec.name}' left unchanged.")
            reserved.add(new)
            plan.append((sec, sec.name, new))
        return plan

    def _text_dialog(self, title: str, header: str, lines: list[str]):
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle(title)
        dlg.resize(820, 560)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(header))
        text = QPlainTextEdit("\n".join(lines))
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        dlg.show()

    def preferences(self):
        dlg = QWidget(self, Qt.Window)
        dlg.setWindowTitle("Preferences")
        dlg.resize(660, 520)
        outer = QVBoxLayout(dlg)
        tabs = QTabWidget()
        outer.addWidget(tabs)

        general = QWidget(); lay = QFormLayout(general)
        tabs.addTab(general, "General")

        # --- VIEW defaults tab ---
        view_tab = QWidget(); vlay = QFormLayout(view_tab)
        tabs.addTab(view_tab, "VIEW defaults")
        vlay.addRow(QLabel("<b>Master VIEW options applied to new calibrations.</b><br>"
                           "<small>On each calibration the VIEW group is collapsed by "
                           "default; expand it to override these.</small>"))
        view_edits: dict[str, QComboBox] = {}
        for key, label in [("visibility", "Visibility"), ("group", "Group")]:
            cb = QComboBox(); cb.setEditable(True)
            for code, text in COMBO[key]:
                cb.addItem(text, code)
            RecordGroup._set_combo(cb, appsettings.view_default(key))
            vlay.addRow(label + ":", cb)
            help = QLabel(VIEW_OPTION_HELP.get(key, ""))
            help.setWordWrap(True)
            help.setStyleSheet("color:gray; font-size:11px; padding:2px 0 8px 0;")
            vlay.addRow(help)
            view_edits[key] = cb

        apply_all_btn = QPushButton("Apply these options to ALL calibrations in the "
                                    "loaded file…")
        apply_all_btn.clicked.connect(lambda: self._apply_view_to_all(view_edits))
        vlay.addRow(apply_all_btn)

        lay.addRow(QLabel("<b>Default save folder per format version</b>"))
        editors: dict[str, QLineEdit] = {}
        for ver in formats.VERSIONS.values():
            row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit(appsettings.save_dir(ver.key))
            browse = QPushButton("Browse…")
            browse.clicked.connect(lambda _c=False, e=edit: self._pick_dir(e))
            h.addWidget(edit); h.addWidget(browse)
            lay.addRow(f"{ver.label} folder:", row)
            editors[ver.key] = edit

        lay.addRow(QLabel("<b>V8 Provider Unique ID</b>  (prefixes all V8 names; must "
                          "start at U100 — EFILive auto-assigns U00–U99)"))
        uid_row = QWidget(); uh = QHBoxLayout(uid_row); uh.setContentsMargins(0, 0, 0, 0)
        uid_num = QSpinBox(); uid_num.setRange(100, 99999)
        uid_num.setValue(appsettings.v8_uid_number()); uid_num.setPrefix("U")
        uid_name = QLineEdit(appsettings.v8_uid_name())
        uid_name.setPlaceholderText("optional name to append, e.g. TorqueUp")
        uid_preview = QLabel()
        uh.addWidget(uid_num); uh.addWidget(QLabel("+")); uh.addWidget(uid_name)
        lay.addRow("Unique ID:", uid_row)

        def _upd_preview():
            uid_preview.setText(f"<i>Result:</i> <b>U{uid_num.value()}"
                                f"{uid_name.text().strip()}</b>")
        uid_num.valueChanged.connect(_upd_preview)
        uid_name.textChanged.connect(_upd_preview)
        _upd_preview()
        lay.addRow("", uid_preview)

        lay.addRow(QLabel("<b>Calibration-ID template per segment</b>  "
                          "(<code>#</code> = auto-numbered digits, e.g. <code>B####</code> → B0001)"))
        id_edits: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        for seg, segname in ct.SEGMENT_NAMES.items():
            row = QWidget(); h = QHBoxLayout(row); h.setContentsMargins(0, 0, 0, 0)
            tmpl = QLineEdit(appsettings.id_template(seg, idgen.DEFAULT_ID_TEMPLATES.get(seg, "")))
            tmpl.setPlaceholderText("e.g. B#### or TU7###")
            start = QLineEdit(str(appsettings.id_start(seg)))
            start.setMaximumWidth(70)
            start.setToolTip("Start number")
            h.addWidget(tmpl); h.addWidget(QLabel("start")); h.addWidget(start)
            lay.addRow(f"{seg} · {segname}:", row)
            id_edits[seg] = (tmpl, start)

        lay.addRow(QLabel("<b>Behavior</b>"))
        wfmt_cb = QComboBox()
        wfmt_cb.addItem("V7.5 (.cax)", "v7")
        wfmt_cb.addItem("V8 (.cax8)", "v8")
        wfmt_cb.setCurrentIndex(1 if appsettings.working_format() == "v8" else 0)
        wfmt_cb.setToolTip("Format that New file authors and saves in. V8 edits "
                           ".cax8 natively (native segment names, Save → .cax8).")
        lay.addRow("Default working format:", wfmt_cb)

        suggest_cb = QCheckBox("Suggest correct segment from address when creating "
                               "a calibration")
        suggest_cb.setChecked(appsettings.suggest_segment_enabled())
        lay.addRow(suggest_cb)

        order_cb = QComboBox()
        order_cb.addItem("V7 (default)", "v7")
        order_cb.addItem("V8", "v8")
        order_cb.setCurrentIndex(1 if appsettings.tree_segment_order() == "v8" else 0)
        lay.addRow("Segment tree order:", order_cb)

        ed_row = QWidget(); eh = QHBoxLayout(ed_row); eh.setContentsMargins(0, 0, 0, 0)
        editor_edit = QLineEdit(appsettings.text_editor())
        editor_edit.setPlaceholderText("notepad (or full path to an editor .exe)")
        ed_browse = QPushButton("Browse…")
        ed_browse.clicked.connect(lambda: self._pick_exe(editor_edit))
        eh.addWidget(editor_edit); eh.addWidget(ed_browse)
        lay.addRow("Text editor exe:", ed_row)

        efi_edits = {}
        for ver, lbl in (("v7", "EFILive V7.5 exe:"), ("v8", "EFILive V8 exe:")):
            r = QWidget(); rh = QHBoxLayout(r); rh.setContentsMargins(0, 0, 0, 0)
            e = QLineEdit(appsettings.efilive_exe(ver))
            b = QPushButton("Browse…")
            b.clicked.connect(lambda _c=False, ed=e: self._pick_exe(ed))
            rh.addWidget(e); rh.addWidget(b)
            lay.addRow(lbl, r)
            efi_edits[ver] = e

        bin_row = QWidget(); bnh = QHBoxLayout(bin_row); bnh.setContentsMargins(0, 0, 0, 0)
        bin_edit = QLineEdit(appsettings.bin_dir())
        bin_edit.setPlaceholderText("folder of ECU .bin images (matched to OS number)")
        bin_browse = QPushButton("Browse…")
        bin_browse.clicked.connect(lambda: self._pick_dir(bin_edit))
        bnh.addWidget(bin_edit); bnh.addWidget(bin_browse)
        lay.addRow("Bin folder:", bin_row)

        bk_row = QWidget(); bh = QHBoxLayout(bk_row); bh.setContentsMargins(0, 0, 0, 0)
        backup_edit = QLineEdit(appsettings.backup_dest())
        backup_edit.setPlaceholderText("another drive/folder, e.g. E:\\Backups\\Cax")
        bk_browse = QPushButton("Browse…")
        bk_browse.clicked.connect(lambda: self._pick_dir(backup_edit))
        bh.addWidget(backup_edit); bh.addWidget(bk_browse)
        lay.addRow("Backup folder:", bk_row)

        note = QLabel(f"<i>Saved to:</i> {appsettings.ini_path()}")
        note.setWordWrap(True)
        note.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addRow(note)
        save = QPushButton("Save")
        outer.addWidget(save)

        def commit():
            appsettings.set_working_format(wfmt_cb.currentData())
            appsettings.set_suggest_segment_enabled(suggest_cb.isChecked())
            appsettings.set_tree_segment_order(order_cb.currentData())
            if editor_edit.text().strip():
                appsettings.set_text_editor(editor_edit.text().strip())
            appsettings.set_backup_dest(backup_edit.text().strip())
            appsettings.set_bin_dir(bin_edit.text().strip())
            for ver, e in efi_edits.items():
                appsettings.set_efilive_exe(ver, e.text().strip())
            self._build_toolbar_refresh()
            for k, e in editors.items():
                if e.text().strip():
                    appsettings.set_save_dir(k, e.text().strip())
            for seg, (tmpl, start) in id_edits.items():
                appsettings.set_id_template(seg, tmpl.text().strip())
                try:
                    appsettings.set_id_start(seg, int(start.text().strip() or "1"))
                except ValueError:
                    pass
            for key, cb in view_edits.items():
                code = cb.currentData()
                appsettings.set_view_default(
                    key, code if code is not None and cb.findText(cb.currentText()) >= 0
                    else cb.currentText())
            appsettings.set_v8_uid_number(uid_num.value())
            appsettings.set_v8_uid_name(uid_name.text().strip())
            if self.cax is not None:
                self._reload_tree()      # segment order / other view prefs
            dlg.close()
        save.clicked.connect(commit)
        dlg.show()

    def _pick_dir(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", edit.text() or "")
        if d:
            edit.setText(d)

    def _pick_exe(self, edit: QLineEdit):
        p, _ = QFileDialog.getOpenFileName(
            self, "Choose text editor", "", "Programs (*.exe);;All files (*)")
        if p:
            edit.setText(p)

    @staticmethod
    def _combo_value(cb: QComboBox) -> str:
        code = cb.currentData()
        return (code if code is not None and cb.findText(cb.currentText()) >= 0
                else cb.currentText())

    def _apply_view_to_all(self, view_edits: dict):
        """Apply the master Visibility/Group to every calibration in the open file."""
        if self.cax is None:
            QMessageBox.information(self, APP_NAME, "Open a file first.")
            return
        vis = self._combo_value(view_edits["visibility"])
        grp = self._combo_value(view_edits["group"])
        cals = [s for s in self.cax.sections if s.get("VIEW")]
        fname = os.path.basename(self.path) if self.path else "(untitled)"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(APP_NAME)
        box.setText(
            f"This will OVERRIDE the Visibility and Group of <b>all {len(cals)} "
            f"calibrations</b> in {fname}.<br><br>Visibility → <b>{vis}</b>, "
            f"Group → <b>{grp}</b>.<br><br>Every calibration's existing setting will "
            "be replaced. This cannot be undone (until you close without saving).<br>"
            "Continue?")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        n = 0
        for sec in cals:
            view = sec.get("VIEW")
            if view and len(view.fields) >= 8:
                f = list(view.fields)
                f[4], f[7] = vis, grp        # Visibility, Group
                view.set_fields(f)
                n += 1
        self._on_edited()
        self._reload_tree()
        QMessageBox.information(self, APP_NAME,
                               f"Applied Visibility/Group to {n} calibration(s).")

    def about(self):
        QMessageBox.about(
            self, "About",
            f"<b>{APP_NAME}</b><br>Editor for EFILive V7 (.cax) and V8 (.cax8) "
            "calibration-definition files.<br><br>"
            "Created By David Christie &amp; Claude AI<br>"
            "<b>Torque Up Technical Services</b><br>"
            "<a href='https://www.torqueup.net'>www.torqueup.net</a>")

    def _confirm_discard(self) -> bool:
        if not self._dirty:
            return True
        r = QMessageBox.question(
            self, APP_NAME, "Do you want to save this file?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Cancel:
            return False
        if r == QMessageBox.Save:
            self.save_file()
        return True

    def closeEvent(self, e):
        if self._confirm_discard():
            if self in _OPEN_WINDOWS:
                _OPEN_WINDOWS.remove(self)
            e.accept()
        else:
            e.ignore()

    def _update_title(self):
        name = os.path.basename(self.path) if self.path else "(untitled)"
        ver = formats.VERSIONS[self.version].label
        self.setWindowTitle(f"{'*' if self._dirty else ''}{name} [{ver}] — {APP_NAME}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    themes.apply_theme(app, appsettings.theme())
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
        w.load(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

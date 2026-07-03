"""
hexview — a simple, virtualized hex viewer for the ECU bin, shown beside the
calibration editor.

- Offset column + 16 hex byte columns + ASCII column.
- goto(addr, span): scroll to and highlight a calibration's bytes.
- Right-click a byte -> emits addressPicked(offset) so the caller can set the
  current calibration's ADDR to that location.

Uses QTableView + a lazy model so even a 2 MB image renders only visible rows.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QHeaderView,
                               QInputDialog, QMenu, QMessageBox, QTableView,
                               QVBoxLayout, QWidget)

_HL = QColor(255, 235, 130)     # highlight background for a calibration's bytes


class HexModel(QAbstractTableModel):
    COLS = 16

    def __init__(self, data: bytes = b""):
        super().__init__()
        self._data = data
        self._hl_lo = self._hl_hi = -1

    def set_data(self, data: bytes):
        self.beginResetModel()
        self._data = data or b""
        self._hl_lo = self._hl_hi = -1
        self.endResetModel()

    def set_highlight(self, lo: int, hi: int):
        self._hl_lo, self._hl_hi = lo, hi
        if self._data:
            top = self.index(lo // self.COLS, 0)
            bot = self.index(min(hi, len(self._data) - 1) // self.COLS, self.COLS)
            self.dataChanged.emit(top, bot, [Qt.BackgroundRole])

    def rowCount(self, _p=None):
        return (len(self._data) + self.COLS - 1) // self.COLS

    def columnCount(self, _p=None):
        return self.COLS + 1   # 16 hex + ASCII

    def offset(self, index) -> int:
        return index.row() * self.COLS + index.column()

    def data(self, index, role=Qt.DisplayRole):
        r, c = index.row(), index.column()
        base = r * self.COLS
        if c < self.COLS:
            i = base + c
            if i >= len(self._data):
                return None
            if role == Qt.DisplayRole:
                return f"{self._data[i]:02X}"
            if self._hl_lo <= i < self._hl_hi:
                if role == Qt.BackgroundRole:
                    return _HL
                if role == Qt.ForegroundRole:    # dark text on the yellow highlight
                    return QColor(20, 20, 20)
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
        else:  # ASCII column
            if role == Qt.DisplayRole:
                chunk = self._data[base:base + self.COLS]
                return "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Vertical:
            return f"{section * self.COLS:06X}"
        return f"{section:X}" if section < self.COLS else "ASCII"


class HexView(QWidget):
    addressPicked = Signal(int)         # set the current cal's ADDR to this offset
    referencesRequested = Signal(int, int)   # find cals referencing [lo, hi)
    createCalRequested = Signal(int, int)    # create a new cal at [lo, hi)

    def __init__(self):
        super().__init__()
        self.model = HexModel()
        self._history: list[int] = []
        self._hist_pos = -1
        self._last_find = b""
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setFont(QFont("Consolas", 9))
        self.table.setSelectionMode(QAbstractItemView.ContiguousSelection)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(18)
        for c in range(self.model.COLS):
            self.table.setColumnWidth(c, 26)
        self.table.horizontalHeader().setSectionResizeMode(
            self.model.COLS, QHeaderView.Stretch)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.setToolTip(
            "G / Ctrl+G  go to address\nAlt+← / Alt+→  back / forward\n"
            "Ctrl+Home / Ctrl+End  top / bottom\nCtrl+C  copy bytes as hex\n"
            "Ctrl+F  find bytes   F3  find next\n"
            "Ctrl+R  find calibrations referencing the selection")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)
        self._add_shortcuts()

    def _add_shortcuts(self):
        ctx = Qt.WidgetWithChildrenShortcut

        def sc(seq, fn):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(ctx)
            s.activated.connect(fn)
            return s
        sc("G", self.goto_dialog)
        sc("Ctrl+G", self.goto_dialog)
        sc("Alt+Left", self._nav_back)
        sc("Alt+Right", self._nav_forward)
        sc("Ctrl+Home", lambda: self.goto(0))
        sc("Ctrl+End", lambda: self.goto(len(self.model._data) - 1))
        sc("Ctrl+C", self._copy_hex)
        sc("Ctrl+F", self._find_dialog)
        sc("F3", self._find_next)
        sc("Ctrl+R", self._emit_references)

    # ---- navigation history --------------------------------------------------
    def _nav_back(self):
        if self._hist_pos > 0:
            self._hist_pos -= 1
            self.goto(self._history[self._hist_pos], record=False)

    def _nav_forward(self):
        if self._hist_pos < len(self._history) - 1:
            self._hist_pos += 1
            self.goto(self._history[self._hist_pos], record=False)

    def goto_dialog(self):
        if not self.model._data:
            return
        cur = self._history[self._hist_pos] if self._history else 0
        text, ok = QInputDialog.getText(self, "Go to address",
                                        "Address (hex):", text=f"{cur:X}")
        if not ok or not text.strip():
            return
        try:
            self.goto(int(text.strip().lstrip("$").replace("0x", ""), 16))
        except ValueError:
            QMessageBox.information(self, "Go to address", "Not a valid hex address.")

    # ---- find ----------------------------------------------------------------
    def _find_dialog(self):
        if not self.model._data:
            return
        text, ok = QInputDialog.getText(
            self, "Find bytes", "Hex bytes (e.g. 00 14 48):", text=" ".join(
                f"{b:02X}" for b in self._last_find))
        if not ok or not text.strip():
            return
        try:
            self._last_find = bytes(int(x, 16) for x in text.split())
        except ValueError:
            QMessageBox.information(self, "Find bytes", "Enter space-separated hex bytes.")
            return
        self._find_from(self._cur_offset() + 1)

    def _find_next(self):
        if self._last_find:
            self._find_from(self._cur_offset() + 1)

    def _find_from(self, start: int):
        if not self._last_find:
            return
        i = self.model._data.find(self._last_find, max(0, start))
        if i < 0:                                   # wrap around
            i = self.model._data.find(self._last_find, 0)
        if i < 0:
            QMessageBox.information(self, "Find bytes", "Not found.")
        else:
            self.goto(i, len(self._last_find))

    def _cur_offset(self) -> int:
        idx = self.table.currentIndex()
        if idx.isValid() and idx.column() < self.model.COLS:
            return self.model.offset(idx)
        return self._history[self._hist_pos] if self._history else 0

    def _copy_hex(self):
        sel = self.table.selectionModel().selectedIndexes()
        offs = sorted(self.model.offset(i) for i in sel if i.column() < self.model.COLS)
        if not offs:
            return
        data = self.model._data
        QApplication.clipboard().setText(
            " ".join(f"{data[o]:02X}" for o in offs if o < len(data)))

    def set_data(self, data: bytes):
        self.model.set_data(data)

    def has_data(self) -> bool:
        return bool(self.model._data)

    def goto(self, addr: int, span: int = 1, record: bool = True):
        if not self.model._data:
            return
        addr = max(0, min(addr, len(self.model._data) - 1))
        self.model.set_highlight(addr, addr + max(span, 1))
        idx = self.model.index(addr // self.model.COLS, addr % self.model.COLS)
        self.table.scrollTo(idx, QAbstractItemView.PositionAtCenter)
        self.table.setCurrentIndex(idx)
        self.table.setFocus()
        if record:                       # push onto navigation history
            self._history = self._history[:self._hist_pos + 1]
            if not self._history or self._history[-1] != addr:
                self._history.append(addr)
            self._hist_pos = len(self._history) - 1

    def _selected_range(self) -> tuple[int, int] | None:
        """The highlighted byte selection as [lo, hi) offsets, or None."""
        offs = [self.model.offset(i)
                for i in self.table.selectionModel().selectedIndexes()
                if i.column() < self.model.COLS]
        if not offs:
            idx = self.table.currentIndex()
            if idx.isValid() and idx.column() < self.model.COLS:
                o = self.model.offset(idx)
                offs = [o]
            else:
                return None
        n = len(self.model._data)
        lo, hi = min(offs), max(offs) + 1
        if lo >= n:
            return None
        return lo, min(hi, n)

    def _emit_references(self):
        rng = self._selected_range()
        if rng:
            self.referencesRequested.emit(*rng)

    def _menu(self, pos):
        idx = self.table.indexAt(pos)
        if not idx.isValid() or idx.column() >= self.model.COLS:
            return
        off = self.model.offset(idx)
        if off >= len(self.model._data):
            return
        # Use the highlighted selection if the clicked byte is inside it,
        # otherwise act on the single clicked byte.
        rng = self._selected_range()
        if not rng or not (rng[0] <= off < rng[1]):
            rng = (off, off + 1)
        lo, hi = rng
        span = hi - lo
        menu = QMenu(self)
        menu.addAction(f"Find references to {lo:X}…  (Ctrl+R)",
                       lambda: self.referencesRequested.emit(lo, hi))
        menu.addAction(f"Create new calibration here ({lo:X}, {span} byte"
                       f"{'s' if span != 1 else ''})…",
                       lambda: self.createCalRequested.emit(lo, hi))
        menu.addSeparator()
        menu.addAction(f"Set calibration address to {off:X}",
                       lambda: self.addressPicked.emit(off))
        menu.exec(self.table.viewport().mapToGlobal(pos))

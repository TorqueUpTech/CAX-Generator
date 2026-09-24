"""
A2L Picker Dialog — Select and import A2L calibrations into .cax files.

PySide6 dialog with:
- File dropdown
- Search field (live filtering)
- Checkbox list
- Preview panel
- Import button
"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QListWidget, QListWidgetItem, QFileDialog,
    QMessageBox, QSplitter, QTextEdit, QProgressDialog, QCheckBox
)
from PySide6.QtCore import Qt, QTimer
from pathlib import Path
import caxformat as cf
import a2limport


class A2lPickerDialog(QDialog):
    """Dialog for selecting and importing A2L calibrations."""

    def __init__(self, parent=None, cax=None, segments=None):
        """
        Initialize dialog.

        Args:
            parent: Parent widget
            cax: Loaded .cax file (caxformat.Cax object)
            segments: Segment ranges for address validation
        """
        super().__init__(parent)
        self.cax = cax
        self.segments = segments
        self.a2l_items = []
        self.a2l_items_filtered = []  # Filtered to CAL area only
        self.selected_items = []
        self.cal_range = None  # (start, end) for calibration area

        self.setWindowTitle("Import A2L Calibrations")
        self.setGeometry(100, 100, 900, 600)
        self._setup_ui()
        self._detect_cal_range()

    def _setup_ui(self):
        """Build UI layout."""
        layout = QVBoxLayout()

        # File selector
        file_layout = QHBoxLayout()
        file_layout.addWidget(QLabel("A2L File:"))
        self.file_button = QPushButton("Browse...")
        self.file_button.clicked.connect(self._select_file)
        file_layout.addWidget(self.file_button)
        self.file_label = QLabel("(no file selected)")
        file_layout.addWidget(self.file_label)
        file_layout.addStretch()
        layout.addLayout(file_layout)

        # Search field and Select All button
        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.search_field = QLineEdit()
        self.search_field.setPlaceholderText("Type to filter calibrations...")
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

        # Splitter: list on left, preview on right
        splitter = QSplitter(Qt.Horizontal)

        # Calibration list
        self.cal_list = QListWidget()
        self.cal_list.itemChanged.connect(self._on_item_changed)
        self.cal_list.itemSelectionChanged.connect(self._on_selection_changed)
        splitter.addWidget(self.cal_list)

        # Preview panel
        preview_layout = QVBoxLayout()
        preview_layout.addWidget(QLabel("Preview:"))
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        preview_layout.addWidget(self.preview_text)
        preview_widget = _create_widget(preview_layout)
        splitter.addWidget(preview_widget)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter)

        # Status/warnings
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        self.status_text.setMaximumHeight(80)
        layout.addWidget(QLabel("Warnings:"))
        layout.addWidget(self.status_text)

        # Options
        options_layout = QHBoxLayout()
        self.chk_overwrite = QCheckBox("Allow Overwrite (update existing calibrations at same address)")
        options_layout.addWidget(self.chk_overwrite)
        options_layout.addStretch()
        layout.addLayout(options_layout)

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

    def _detect_cal_range(self):
        """Detect calibration area range from segments or use default."""
        if not self.segments:
            return

        # Find CAL segment range (calibrations are after OS)
        for seg_name, (start, end) in self.segments.items():
            if 'CAL' in seg_name.upper() or 'CALIBRATION' in seg_name.upper():
                self.cal_range = (start, end)
                break

        # If no explicit CAL, use default for E40 (0x1C0000+)
        if not self.cal_range:
            # E40 typical: OS ends at 0x1C0000, CAL starts there
            self.cal_range = (0x1C0000, 0x200000)  # 1024KB total, OS=1792KB, so CAL would be ~256KB

    def _filter_to_cal_area(self):
        """Filter A2L items to calibration area only (exclude RAM/I/O)."""
        # Exclude high addresses (0xFFFF**** = RAM/runtime symbols)
        # Exclude calibrations starting with "Ve" (RAM variables)
        # Keep only addresses in the actual binary ROM (0x0 to 0x100000 for 1MB E40)
        self.a2l_items_filtered = [
            item for item in self.a2l_items
            if item.address is not None
            and item.address < 0x100000
            and not item.title.startswith('Ve')
        ]

    def _select_file(self):
        """Browse and select A2L file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Select A2L File", "",
            "A2L Files (*.a2l);;All Files (*)"
        )

        if not path:
            return

        # Show progress dialog
        progress = QProgressDialog(
            "Parsing A2L file...", None, 0, 0, self
        )
        progress.setWindowModality(Qt.WindowModal)
        progress.show()
        self.repaint()

        # Parse A2L file
        try:
            self.a2l_items = a2limport.parse_a2l_minimal(path)
            self._filter_to_cal_area()
            progress.setLabelText(f"Loading {len(self.a2l_items_filtered)} calibrations...")
            self.repaint()

            self.file_label.setText(Path(path).name)
            self._populate_list()
            progress.close()
            msg = f"Loaded {len(self.a2l_items_filtered)} calibrations (CAL area)"
            if len(self.a2l_items) > len(self.a2l_items_filtered):
                msg += f" — filtered {len(self.a2l_items) - len(self.a2l_items_filtered)} OS/other"
            self.status_text.setText(msg)
        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Failed to parse A2L file:\n{str(e)}")
            self.file_label.setText("(error)")

    def _populate_list(self):
        """Populate calibration list with checkboxes in batches (CAL area only)."""
        self.cal_list.clear()
        self.cal_list.setUpdatesEnabled(False)

        # Add items in batches for better performance
        batch_size = 500
        for i, item in enumerate(self.a2l_items_filtered):
            # Create checkbox item
            list_item = QListWidgetItem()
            list_item.setText(f"0x{item.address:X} — {item.title}" if item.address else item.title)
            list_item.setFlags(list_item.flags() | Qt.ItemIsUserCheckable)
            list_item.setCheckState(Qt.Unchecked)
            list_item.setData(Qt.UserRole, item)  # Store the A2lCalibration object

            self.cal_list.addItem(list_item)

            # Process events every batch to keep UI responsive
            if (i + 1) % batch_size == 0:
                self.repaint()

        self.cal_list.setUpdatesEnabled(True)

    def _on_search_changed(self):
        """Filter list based on search field."""
        search_text = self.search_field.text().lower()

        for i in range(self.cal_list.count()):
            item = self.cal_list.item(i)
            text = item.text().lower()
            visible = search_text in text if search_text else True
            self.cal_list.setRowHidden(i, not visible)

    def _on_item_changed(self, item):
        """Handle checkbox state change."""
        self._update_import_button()

    def _on_selection_changed(self):
        """Show preview of selected calibration."""
        selected = self.cal_list.selectedItems()
        if not selected:
            self.preview_text.setText("")
            return

        item = selected[0]
        cal = item.data(Qt.UserRole)

        if not cal:
            return

        # Build preview text
        preview = f"""
Name: {cal.title}
Type: {cal.kind}
Address: 0x{cal.address:X} (if cal.address else "N/A")
Data Type: {cal.data_type}

COMPU_METHOD: {cal.compu_method}
  Type: {cal.compu_type}
  Factor: {cal.factor}
  Offset: {cal.offset}
  Min: {cal.min_val}
  Max: {cal.max_val}

Annotation:
{cal.annotation}

{"⚠ " + cal.note if cal.note else ""}
"""
        self.preview_text.setText(preview.strip())

    def _update_import_button(self):
        """Enable import button if items are checked."""
        count = sum(1 for i in range(self.cal_list.count())
                   if self.cal_list.item(i).checkState() == Qt.Checked)
        self.import_btn.setEnabled(count > 0)

    def _select_all(self):
        """Check all visible items."""
        for i in range(self.cal_list.count()):
            item = self.cal_list.item(i)
            if not self.cal_list.isRowHidden(i):
                item.setCheckState(Qt.Checked)
        self._update_import_button()

    def _clear_all(self):
        """Uncheck all items."""
        for i in range(self.cal_list.count()):
            self.cal_list.item(i).setCheckState(Qt.Unchecked)
        self._update_import_button()

    def _import(self):
        """Import selected calibrations into .cax."""
        if self.cax is None:
            QMessageBox.warning(self, "Error", "No CAX file loaded")
            return

        # Collect selected items
        selected = []
        for i in range(self.cal_list.count()):
            item = self.cal_list.item(i)
            if item.checkState() == Qt.Checked:
                cal = item.data(Qt.UserRole)
                if cal:
                    selected.append(cal)

        if not selected:
            QMessageBox.warning(self, "Error", "No calibrations selected")
            return

        # Show progress dialog
        progress = QProgressDialog(
            f"Importing {len(selected)} calibrations...",
            None, 0, len(selected), self
        )
        progress.setWindowModality(Qt.WindowModal)
        progress.show()

        try:
            imported, warnings = a2limport.import_selected_to_cax(
                self.cax, selected, self.segments,
                allow_overwrite=self.chk_overwrite.isChecked()
            )

            progress.close()

            # Show results
            msg = f"Imported {imported} calibrations"
            if warnings:
                msg += f"\n\n{len(warnings)} warnings:\n"
                for title, warn in warnings[:10]:  # Show first 10
                    msg += f"  • {title}: {warn}\n"
                if len(warnings) > 10:
                    msg += f"  ... and {len(warnings) - 10} more"

            QMessageBox.information(self, "Import Complete", msg)
            self.accept()

        except Exception as e:
            progress.close()
            QMessageBox.critical(self, "Error", f"Import failed:\n{str(e)}")


def _create_widget(layout):
    """Create a widget from a layout."""
    from PySide6.QtWidgets import QWidget
    widget = QWidget()
    widget.setLayout(layout)
    return widget

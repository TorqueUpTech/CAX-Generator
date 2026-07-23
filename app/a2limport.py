"""
a2limport — Selective A2L calibration import into .cax files.

Parses ASAP2 (A2L) definition files and imports selected calibrations
into existing .cax files, mapping addresses to segments and extracting
COMPU_METHOD (factor/offset) transformations.

Usage:
    items = parse_a2l_minimal(path)           # Get calibration list
    for item in items:
        print(f"{item.title} @ 0x{item.address:X}")

    import_selected_to_cax(cax, selected_items, bin_segments)
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Dict, Tuple


@dataclass
class A2lCalibration:
    """A single calibration from A2L file."""
    title: str                    # CHARACTERISTIC name
    kind: str                     # 'VALUE', 'MAP', 'CURVE'
    address: Optional[int]        # Hex address from CHARACTERISTIC
    annotation: str = ""          # Description from ANNOTATION_TEXT
    compu_method: str = ""        # Reference to COMPU_METHOD
    compu_type: str = ""          # 'LINEAR' (RAT_FUNC) or 'LOOKUP' (TAB_INTP) or 'UNKNOWN'
    factor: str = "1"             # Scale from COEFFS
    offset: str = "0"             # Offset from COEFFS
    min_val: float = 0.0
    max_val: float = 0.0
    data_type: str = ""           # RL_VALUE_UB, RL_VALUE_FL, etc.
    note: str = ""                # Warnings (e.g., "complex lookup table")


def parse_a2l_minimal(path: str) -> List[A2lCalibration]:
    """
    Parse A2L (ASAP2) file and extract calibrations.

    A2L is a text-based format (not XML) with /begin..../end blocks.
    This parser extracts only metadata needed for selection:
    - CHARACTERISTIC name, address, annotation
    - COMPU_METHOD reference and type (LINEAR vs LOOKUP)

    Args:
        path: Path to .a2l file

    Returns:
        List of A2lCalibration objects
    """
    raw = Path(path).read_bytes()
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        # Fallback: try Latin-1 for non-UTF-8 files
        text = raw.decode('latin-1')

    lines = text.split('\n')

    # Build COMPU_METHOD index
    compu_methods = {}
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('/begin COMPU_METHOD'):
            # Extract COMPU_METHOD name and type
            parts = line.split()
            if len(parts) >= 3:
                cm_name = parts[2]
                compu_type, factor, offset = _parse_compu_method(lines, i)
                compu_methods[cm_name] = {
                    'type': compu_type,
                    'factor': factor,
                    'offset': offset
                }
        i += 1

    # Extract calibrations (CHARACTERISTIC elements)
    items = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('/begin CHARACTERISTIC'):
            # Extract CHARACTERISTIC name and data
            cal = _parse_characteristic(lines, i, compu_methods)
            if cal:
                items.append(cal)
        i += 1

    return items


def _parse_compu_method(lines: List[str], start_idx: int) -> Tuple[str, str, str]:
    """
    Parse COMPU_METHOD block starting at start_idx.

    Returns: (type, factor, offset)
      type: 'LINEAR', 'LOOKUP', or 'UNKNOWN'
      factor, offset: strings
    """
    compu_type = 'UNKNOWN'
    factor = '1'
    offset = '0'

    i = start_idx + 1
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('/end COMPU_METHOD'):
            break

        # Look for RAT_FUNC (linear scale/offset)
        if 'RAT_FUNC' in line:
            compu_type = 'LINEAR'
            # Find COEFFS line
            while i < len(lines):
                i += 1
                line = lines[i].strip()
                if line.startswith('COEFFS'):
                    # COEFFS a0 a1 a2 a3 a4 a5
                    # For linear: offset=a0, factor=a1
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            offset = repr(float(parts[1]))
                            factor = repr(float(parts[2]))
                        except (ValueError, IndexError):
                            pass
                    break
                if line.startswith('/'):
                    break

        # Look for TAB_INTP or TAB_VERB (lookup table)
        elif 'TAB_INTP' in line or 'TAB_VERB' in line:
            compu_type = 'LOOKUP'

        i += 1

    return compu_type, factor, offset


def _parse_characteristic(lines: List[str], start_idx: int, compu_methods: Dict) -> Optional[A2lCalibration]:
    """
    Parse CHARACTERISTIC block starting at start_idx.

    Returns: A2lCalibration or None if unable to extract
    """
    title = None
    kind = 'VALUE'
    address = None
    annotation = ""
    compu_method = ""
    data_type = ""
    min_val = 0.0
    max_val = 255.0

    # Extract name from /begin CHARACTERISTIC line
    line = lines[start_idx].strip()
    parts = line.split()
    if len(parts) >= 3:
        title = parts[2]

    # Parse the block
    i = start_idx + 1
    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('/end CHARACTERISTIC'):
            break

        # Extract kind (VALUE, MAP, CURVE) — appears as a standalone line
        if line in ('VALUE', 'MAP', 'CURVE'):
            kind = line

        # Extract address (hex value, appears as 0x...)
        elif line.startswith('0x'):
            try:
                address = int(line, 16)
            except ValueError:
                pass

        # Extract data type (RL_VALUE_*)
        elif line.startswith('RL_VALUE_'):
            data_type = line

        # Extract COMPU_METHOD reference
        elif line.startswith('CM_'):
            compu_method = line

        # Extract limits
        elif line.startswith('LOWER_LIMIT'):
            try:
                min_val = float(line.split()[1]) if len(line.split()) > 1 else 0.0
            except (ValueError, IndexError):
                pass
        elif line.startswith('UPPER_LIMIT'):
            try:
                max_val = float(line.split()[1]) if len(line.split()) > 1 else 255.0
            except (ValueError, IndexError):
                pass

        # Extract annotation
        elif line.startswith('/begin ANNOTATION_TEXT'):
            i += 1
            annot_lines = []
            while i < len(lines):
                line = lines[i].strip()
                if line.startswith('/end ANNOTATION_TEXT'):
                    break
                annot_lines.append(line)
                i += 1
            annotation = ' '.join(annot_lines).strip()

        i += 1

    if not title or address is None:
        return None

    # Get factor/offset from COMPU_METHOD
    compu_type = 'UNKNOWN'
    factor = '1'
    offset = '0'
    note = ""

    if compu_method and compu_method in compu_methods:
        cm_info = compu_methods[compu_method]
        compu_type = cm_info['type']
        factor = cm_info['factor']
        offset = cm_info['offset']

        if compu_type == 'LOOKUP':
            note = "complex lookup table (verify in CAX)"

    return A2lCalibration(
        title=title,
        kind=kind,
        address=address,
        annotation=annotation,
        compu_method=compu_method,
        compu_type=compu_type,
        factor=factor,
        offset=offset,
        min_val=min_val,
        max_val=max_val,
        data_type=data_type,
        note=note
    )


def import_selected_to_cax(cax, selected_items: List[A2lCalibration], segments=None):
    """
    Import selected A2L calibrations into loaded .cax file.

    For each selected calibration:
    1. Check if address already exists in .cax (warn)
    2. Extract factor/offset from A2L COMPU_METHOD
    3. Copy annotation to DESC
    4. Update or create .cax Section

    Args:
        cax: Loaded .cax (from caxformat.read())
        selected_items: List of A2lCalibration to import
        segments: Optional segment ranges (for address validation)

    Returns:
        (imported_count, warnings_list)
    """
    import caxformat as cf
    import segmap
    import idgen

    imported = 0
    warnings = []

    # Build id_factory for new sections
    reserved = {s.name for s in cax.sections}

    def make_id(seg):
        """Generate a new unique ID for the segment."""
        # Try to use template if available
        try:
            import appsettings
            tmpl = appsettings.id_template(seg, idgen.DEFAULT_ID_TEMPLATES.get(seg, ""))
            nid = idgen.next_id(reserved, tmpl, appsettings.id_start(seg)) if tmpl else None
        except:
            nid = None

        # Fallback: simple X###N format
        if not nid:
            n = 1
            while f"X{seg or 0}{n:03d}" in reserved:
                n += 1
            nid = f"X{seg or 0}{n:03d}"
        reserved.add(nid)
        return nid

    for item in selected_items:
        if item.address is None:
            warnings.append((item.title, "No address found"))
            continue

        # Check if calibration already exists at this address
        existing_section = None
        for sec in cax.sections:
            addr_rec = sec.get('ADDR')
            if addr_rec and addr_rec.fields:
                try:
                    addr_hex = addr_rec.fields[0]
                    if addr_hex != cf.PLACEHOLDER:
                        addr = int(addr_hex, 16)
                        if addr == item.address:
                            existing_section = sec
                            warnings.append((item.title, f"Cal already exists at 0x{item.address:X}"))
                            break
                except (ValueError, IndexError):
                    pass

        # If exists, update; otherwise create new
        if existing_section:
            sec = existing_section
        else:
            # Determine segment from address
            seg = "1"  # Default to OS
            if segments and item.address:
                phys = segmap.classify(item.address, segments)
                if phys:
                    seg_map = {"OS": "1", "Engine": "2", "EngineDiag": "3", "Trans": "4",
                              "Diag": "5", "TransDiag": "5", "Fuel": "6", "System": "7",
                              "Speedo": "8", "BootBlock": "1", "Boot Block": "1"}
                    seg = seg_map.get(phys, "1")

            sec = cf.Section(make_id(seg))

        # Add/update DESC line
        desc_lines = [line for line in sec.lines if not (line.is_record and line.key and line.key.startswith('DESC'))]
        if item.annotation:
            desc_lines.append(cf.Line(cf.LineKind.RECORD, "", key="DESC.001",
                                     fields=[item.annotation], edited=True))
        sec.lines = desc_lines

        # Update/add VIEW line (only if new section)
        if not existing_section:
            # Determine segment from address
            seg = "1"  # Default to OS
            if segments and item.address:
                phys = segmap.classify(item.address, segments)
                if phys:
                    seg_map = {"OS": "1", "Engine": "2", "EngineDiag": "3", "Trans": "4",
                              "Diag": "5", "TransDiag": "5", "Fuel": "6", "System": "7",
                              "Speedo": "8", "BootBlock": "1", "Boot Block": "1"}
                    seg = seg_map.get(phys, "1")

            sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="VIEW",
                                    fields=[seg, item.title or "", "", "", "1", "1", "0", "0"],
                                    edited=True))

        # Update/add ADDR line
        addr_hex = f"{item.address:X}" if item.address is not None else cf.PLACEHOLDER
        bits = _bits_from_data_type(item.data_type)
        dtype = "UNM"  # Unsigned numeric (default)

        sec.lines = [line for line in sec.lines if not (line.is_record and line.key == 'ADDR')]
        sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="ADDR",
                                fields=[addr_hex, "0", str(bits), dtype, "0"],
                                edited=True))

        # Update/add SI_SLOT line
        sec.lines = [line for line in sec.lines if not (line.is_record and line.key == 'SI_SLOT')]
        sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="SI_SLOT",
                                fields=[cf.PLACEHOLDER, item.factor, item.offset, "0", cf.PLACEHOLDER, cf.PLACEHOLDER, cf.PLACEHOLDER, cf.PLACEHOLDER],
                                edited=True))

        # Add to sections if new
        if not existing_section:
            cax.sections.append(sec)

        imported += 1

    return imported, warnings


def _bits_from_data_type(data_type: str) -> int:
    """Extract bit size from RL_VALUE_* data type."""
    if 'UB' in data_type or 'SB' in data_type:
        return 8
    elif 'UW' in data_type or 'SW' in data_type:
        return 16
    elif 'UL' in data_type or 'SL' in data_type or 'FL' in data_type:
        return 32
    elif 'FD' in data_type:
        return 64
    else:
        return 16  # Default

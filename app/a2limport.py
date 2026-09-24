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
    compu_type: str = ""          # 'LINEAR' (RAT_FUNC), 'LOOKUP' (TAB_INTP), 'ENUM' (COMPU_PHYS), 'UNKNOWN'
    factor: str = "1"             # Scale from COEFFS (LINEAR only)
    offset: str = "0"             # Offset from COEFFS (LINEAR only)
    min_val: float = 0.0
    max_val: float = 0.0
    data_type: str = ""           # RL_VALUE_UB, RL_VALUE_FL, etc.
    enum_values: List[str] = field(default_factory=list)  # State labels for ENUM type
    note: str = ""                # Warnings (e.g., "complex lookup table")


def parse_a2l_minimal(path: str) -> List[A2lCalibration]:
    """
    Parse A2L (ASAP2) file and extract calibrations (optimized single-pass).

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

    # Single-pass indexing: collect COMPU_METHOD and COMPU_PHYS blocks
    compu_methods = {}
    compu_phys = {}
    items = []

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

        elif line.startswith('/begin COMPU_PHYS'):
            # Extract COMPU_PHYS name and enum values
            parts = line.split()
            if len(parts) >= 3:
                cp_name = parts[2]
                enum_values = _parse_compu_phys(lines, i)
                if enum_values:
                    compu_phys[cp_name] = enum_values

        elif line.startswith('/begin CHARACTERISTIC'):
            # Extract CHARACTERISTIC name and data
            cal = _parse_characteristic(lines, i, compu_methods, compu_phys)
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
    factor = '1.0'
    offset = '0.0'

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
                if i >= len(lines):
                    break
                line = lines[i].strip()
                if line.startswith('COEFFS'):
                    # COEFFS a0 a1 a2 a3 a4 a5
                    # For linear: offset=a0, factor=a1
                    parts = line.split()
                    if len(parts) >= 3:
                        try:
                            offset = str(float(parts[1]))  # a0 = offset
                            factor = str(float(parts[2]))   # a1 = factor
                        except (ValueError, IndexError):
                            pass
                    break
                elif line.startswith('/end'):
                    break

        # Look for TAB_INTP or TAB_VERB (lookup table)
        elif 'TAB_INTP' in line or 'TAB_VERB' in line:
            compu_type = 'LOOKUP'

        i += 1

    return compu_type, factor, offset


def _parse_compu_phys(lines: List[str], start_idx: int) -> List[str]:
    """
    Parse COMPU_PHYS (enumeration) block starting at start_idx.

    Returns: List of state labels (e.g., ["Off", "On", "Standby"])
    """
    enum_values = []
    i = start_idx + 1

    while i < len(lines):
        line = lines[i].strip()

        if line.startswith('/end COMPU_PHYS'):
            break

        # Look for PHYS (physical value) entries like:
        # PHYS "Off"  or  PHYS Off  or  1 "On"
        if line and (line.startswith('PHYS') or line[0].isdigit()):
            # Extract quoted string or word
            match = re.search(r'"([^"]*)"', line)
            if match:
                enum_values.append(match.group(1))
            else:
                # Fallback: try unquoted word after PHYS keyword
                parts = line.split()
                if len(parts) > 1 and parts[0] == 'PHYS':
                    enum_values.append(parts[1])

        i += 1

    return enum_values if enum_values else []


def _parse_characteristic(lines: List[str], start_idx: int, compu_methods: Dict, compu_phys: Dict = None) -> Optional[A2lCalibration]:
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

        # Extract COMPU_METHOD reference (can be standalone or after /begin COMPU_MEASUREMENT)
        elif line.startswith('CM_'):
            compu_method = line
        elif line.startswith('/begin COMPU_MEASUREMENT'):
            # COMPU_MEASUREMENT may contain the CM_ reference
            parts = line.split()
            if len(parts) >= 3:
                compu_method = parts[2]

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

    # Get factor/offset from COMPU_METHOD, or enum values from COMPU_PHYS
    compu_type = 'UNKNOWN'
    factor = '1'
    offset = '0'
    enum_values = []
    note = ""

    if compu_method:
        # Strip any leading whitespace/quotes from compu_method name
        cm_key = compu_method.strip('\'"')

        # Check COMPU_PHYS first (enumerations)
        if compu_phys and cm_key in compu_phys:
            compu_type = 'ENUM'
            enum_values = compu_phys[cm_key]
        # Then check COMPU_METHOD (linear/lookup)
        elif cm_key in compu_methods:
            cm_info = compu_methods[cm_key]
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
        enum_values=enum_values,
        note=note
    )


def import_selected_to_cax(cax, selected_items: List[A2lCalibration], segments=None, allow_overwrite=False):
    """
    Import selected A2L calibrations into loaded .cax file.

    For each selected calibration:
    1. Check if address already exists in .cax (warn or overwrite)
    2. Extract factor/offset from A2L COMPU_METHOD or enum labels
    3. Copy annotation to DESC
    4. Update or create .cax Section

    Args:
        cax: Loaded .cax (from caxformat.read())
        selected_items: List of A2lCalibration to import
        segments: Optional segment ranges (for address validation)
        allow_overwrite: If True, overwrite existing calibrations at same address

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
                            if not allow_overwrite:
                                warnings.append((item.title, f"Cal already exists at 0x{item.address:X} (skipped)"))
                                break
                            else:
                                warnings.append((item.title, f"Cal already exists at 0x{item.address:X} (overwriting)"))
                                break
                except (ValueError, IndexError):
                    pass

        # If exists and not overwriting, skip
        if existing_section and not allow_overwrite:
            continue

        # If exists and overwriting, update; otherwise create new
        if existing_section and allow_overwrite:
            sec = existing_section
        else:
            # Determine segment from address
            seg = "0"  # Default to OS (segment 0)
            if segments and item.address:
                phys = segmap.classify(item.address, segments)
                if phys:
                    seg_map = {"OS": "0", "Engine": "2", "EngineDiag": "3", "Trans": "4",
                              "Diag": "5", "TransDiag": "5", "Fuel": "6", "System": "7",
                              "Speedo": "8", "BootBlock": "0", "Boot Block": "0"}
                    seg = seg_map.get(phys, "0")

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
            seg = "0"  # Default to OS (segment 0)
            if segments and item.address:
                phys = segmap.classify(item.address, segments)
                if phys:
                    seg_map = {"OS": "0", "Engine": "2", "EngineDiag": "3", "Trans": "4",
                              "Diag": "5", "TransDiag": "5", "Fuel": "6", "System": "7",
                              "Speedo": "8", "BootBlock": "0", "Boot Block": "0"}
                    seg = seg_map.get(phys, "0")

            sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="VIEW",
                                    fields=[seg, item.title or "", "", "", "1", "1", "0", "0"],
                                    edited=True))

        # Update/add ADDR line
        addr_hex = f"{item.address:X}" if item.address is not None else cf.PLACEHOLDER
        bits = _bits_from_data_type(item.data_type)

        # Set data type based on compu_type
        if item.compu_type == 'ENUM':
            dtype = "SED"  # State-encoded (enum)
        else:
            dtype = "UNM"  # Unsigned numeric (default)

        sec.lines = [line for line in sec.lines if not (line.is_record and line.key == 'ADDR')]
        sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="ADDR",
                                fields=[addr_hex, "0", str(bits), dtype, "0"],
                                edited=True))

        # For ENUM calibrations, add enum labels; for others, add SI_SLOT with factor/offset
        sec.lines = [line for line in sec.lines if not (line.is_record and line.key.startswith('SI_'))]

        if item.compu_type == 'ENUM' and item.enum_values:
            # Add SI_ENUM with state labels
            enum_str = ','.join(item.enum_values)
            sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="SI_ENUM",
                                    fields=[enum_str],
                                    edited=True))
            # Add IM_ENUM (same as SI for now - user can customize)
            sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key="IM_ENUM",
                                    fields=[enum_str],
                                    edited=True))
        else:
            # Add SI_SLOT for numeric calibrations
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

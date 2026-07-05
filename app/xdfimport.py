"""
xdfimport — import TunerPro XDF definitions into EFILive V7 cax calibrations.

Maps XDF elements to cax records:
  XDFCONSTANT -> scalar  (DisplayType 1 Val)
  XDFFLAG     -> bit/SED (DisplayType 1 + SI_ENUM from the description's "values:")
  XDFTABLE    -> Map/Col (z-axis = data; x/y axes -> SI_COL / SI_ROW labels)
EMBEDDEDDATA gives address+size; <MATH equation> gives Factor/Offset; the data
address is classified to a segment via segmap (so VIEW.Seg is correct).

Best effort: linear scalings and `X & mask` bitfields are handled; non-linear
equations fall back to factor 1 / offset 0 and are listed in the report.
"""
from __future__ import annotations

import os
import re
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import caxformat as cf

# Physical segment name (from segmap) -> EFILive V7 seg number. Controller-
# independent EFILive standard; covers every segment name used by the bundled
# controllers (E/T series). "Boot Block" and "BootBlock" are both spellings seen.
PHYS_TO_V7SEG = {
    "OS": "1", "Engine": "2", "EngineDiag": "3", "Trans": "4",
    "Diag": "5", "TransDiag": "5", "Fuel": "6", "System": "7",
    "Speedo": "8", "BootBlock": "1", "Boot Block": "1",
}
_SAFE = set("0123456789Xx.+-*/()&|^ ")


@dataclass
class XdfItem:
    kind: str           # 'scalar' | 'flag' | 'table'
    title: str
    desc: str
    address: int | None
    bits: int
    signed: bool
    units: str = ""
    prec: str = "0"
    factor: str = "1"
    offset: str = "0"
    bitoff: int = 0
    enum: str = ""                       # "0=Disabled,1=Enabled"
    colcount: int = 1
    rowcount: int = 1
    col_units: str = ""
    row_units: str = ""
    col_labels: list = field(default_factory=list)
    row_labels: list = field(default_factory=list)
    major: str = ""
    minor: str = ""
    note: str = ""                       # review note (non-linear scaling etc.)


@dataclass
class XdfReport:
    items: list = field(default_factory=list)
    review: list = field(default_factory=list)    # (title, note)
    skipped: list = field(default_factory=list)    # (title, address) out of flash

    def summary(self):
        kinds = {}
        for it in self.items:
            kinds[it.kind] = kinds.get(it.kind, 0) + 1
        s = f"{len(self.items)} items ({kinds}), {len(self.review)} need review"
        if self.skipped:
            s += f", {len(self.skipped)} skipped (outside flash / RAM)"
        return s


# ---- equation -> factor/offset ----------------------------------------------
# The variable is X or x as a standalone token — but NOT the 'x' inside a hex
# literal like 0x03 (preceded by a word char). Match a bare x/X only.
_VAR_RE = re.compile(r"(?<![\w])[xX](?![\w])")


# ---- Enhanced Math Evaluation ------------------------------------------------

def evaluate_math(equation: str, raw_value: float,
                  axis_context: dict = None,
                  linked_vars: dict = None) -> tuple:
    """
    Evaluate XDF math equations with TunerPro support.

    Handles:
    - Ternary: if(cond ; true ; false) -> Python (true) if (cond) else (false)
    - Bitshift: Y >> 6 -> int(Y) >> 6 (auto-wrap in int)
    - Math functions: exp, log, sqrt, pow, sin, cos, tan
    - Multi-variable: X (raw), A (row), B (col), E (element count), Y/Z (axis)
    - Linked variables: VAR type="link" references (resolved via linked_vars dict)
    - Division by zero detection (inf/nan checks)

    Args:
        equation: Math equation string (e.g., "0.75 * X - 40")
        raw_value: Raw binary value
        axis_context: Optional dict with 'row_index', 'col_index', 'x_axis_value', 'y_axis_value'
        linked_vars: Optional dict of resolved linked variable values

    Returns:
        (result: float or None, error: str)
        If error, result is None and error contains error message.
    """
    if axis_context is None:
        axis_context = {}

    # Strip XML entities (&#013;&#010; etc)
    equation = re.sub(r'&#\d+;', '', equation)
    equation = equation.strip()

    # Handle null/empty equations
    if not equation or equation.lower() in ('(null)', 'null', ''):
        return float(raw_value), ""

    # Simple passthrough
    if equation.strip().upper() == 'X':
        return float(raw_value), ""

    try:
        equation_fixed = equation.strip()

        # Fix equations starting with * or / (e.g., "*2**14" -> "X*2**14")
        # BUT: don't prepend X to unary +/- (e.g., "-0.375*X" is valid)
        if equation_fixed.startswith(('*', '/')):
            equation_fixed = 'X' + equation_fixed

        # Replace X variants like X1000, X100, X10 with raw_value
        equation_fixed = re.sub(r'\bX\d+\b', str(raw_value), equation_fixed, flags=re.IGNORECASE)

        # Convert TunerPro ternary if(cond ; true ; false) to Python
        # Pattern: if ( condition ; value_if_true ; value_if_false )
        tp_if_match = re.match(
            r'^\s*if\s*\(\s*(.+?)\s*;\s*(.+?)\s*;\s*(.+?)\s*\)\s*$',
            equation_fixed, re.IGNORECASE
        )
        if tp_if_match:
            cond_part = tp_if_match.group(1).strip()
            true_part = tp_if_match.group(2).strip()
            false_part = tp_if_match.group(3).strip()
            equation_fixed = f"({true_part}) if ({cond_part}) else ({false_part})"

        # Ensure bitshift operands are integers
        # Python's >> and << require int operands
        if '>>' in equation_fixed or '<<' in equation_fixed:
            equation_fixed = re.sub(r'\bY\b', 'int(Y)', equation_fixed)
            equation_fixed = re.sub(r'\bX\b', 'int(X)', equation_fixed)
            equation_fixed = re.sub(r'\bA\b', 'int(A)', equation_fixed)
            equation_fixed = re.sub(r'\bB\b', 'int(B)', equation_fixed)
            equation_fixed = re.sub(r'\bE\b', 'int(E)', equation_fixed)
            # Clean up double-wrapping: int(int(X)) -> int(X)
            equation_fixed = re.sub(r'int\(int\(([^)]+)\)\)', r'int(\1)', equation_fixed)

        # Build evaluation namespace
        namespace = {
            # Raw data value
            'X': raw_value, 'x': raw_value,

            # Axis context (row/col indices and axis label values)
            'A': axis_context.get('row_index', 0),
            'a': axis_context.get('row_index', 0),
            'B': max(axis_context.get('col_index', 1), 1),
            'b': max(axis_context.get('col_index', 1), 1),
            'E': axis_context.get('element_count', 1),
            'e': axis_context.get('element_count', 1),
            'Y': axis_context.get('y_axis_value', 0),
            'y': axis_context.get('y_axis_value', 0),
            'Z': axis_context.get('x_axis_value', 0),
            'z': axis_context.get('x_axis_value', 0),

            # Math functions
            'exp': math.exp, 'log': math.log, 'log10': math.log10,
            'sqrt': math.sqrt, 'pow': math.pow, 'abs': abs,
            'int': int, 'float': float,
            'sin': math.sin, 'cos': math.cos, 'tan': math.tan,
            'PI': math.pi, 'pi': math.pi,

            # Restrict eval to these symbols only
            '__builtins__': {}
        }

        # Apply linked variable overrides (highest priority)
        if linked_vars:
            for var_name, var_val in linked_vars.items():
                if var_val is not None:
                    namespace[var_name] = var_val
                    namespace[var_name.lower()] = var_val

        # Evaluate the expression
        result = eval(equation_fixed, namespace)

        # Check for invalid results (inf/nan from division by zero)
        if math.isinf(result):
            return None, f"Division by zero (result=inf) in '{equation}'"
        if math.isnan(result):
            return None, f"Invalid math operation (result=NaN) in '{equation}'"

        return float(result), ""

    except ZeroDivisionError:
        return None, f"Division by zero in '{equation}' (X={raw_value})"
    except Exception as e:
        return None, f"Math evaluation failed for '{equation}' (X={raw_value}): {str(e)}"


def _build_uniqueid_index(root) -> dict:
    """
    Build index of all elements by uniqueid for linked-var resolution.

    Returns:
        uniqueid -> XML element dict
    """
    index = {}

    # Index all XDFTABLE elements
    for table in root.findall('.//XDFTABLE'):
        uid = table.get('uniqueid')
        if uid:
            index[uid] = table

    # Index all XDFCONSTANT elements (can be linked var sources too)
    for const in root.findall('.//XDFCONSTANT'):
        uid = const.get('uniqueid')
        if uid:
            index[uid] = const

    return index


def _resolve_linked_vars(math_elem, uniqueid_index: dict) -> dict:
    """
    Resolve VAR type="link" elements to linked element references.

    Returns dict mapping VAR id -> linked element.

    Args:
        math_elem: MATH XML element containing VAR children
        uniqueid_index: Dict from _build_uniqueid_index()

    Returns:
        {'VAR_ID': linked_element, ...}
    """
    linked = {}

    for var in math_elem.findall('VAR'):
        if var.get('type') == 'link':
            var_id = var.get('id')
            linkid = var.get('linkid')
            if var_id and linkid and linkid in uniqueid_index:
                linked[var_id] = uniqueid_index[linkid]

    return linked


def parse_xdf_safe(path: str):
    """
    Parse XDF file with Latin-1 fallback for non-UTF-8 (German ü, ° etc).

    Returns:
        ET.Element (root of XDF tree)
    """
    try:
        tree = ET.parse(path)
        return tree.getroot()
    except ET.ParseError as e:
        # Fallback: re-read as Latin-1 (handles German umlauts, degree symbol, etc)
        try:
            raw = Path(path).read_bytes()
            text = raw.decode('latin-1')
            # Strip any XML declaration that may specify wrong encoding
            text = re.sub(r'<\?xml[^?]*\?>', '<?xml version="1.0" encoding="utf-8"?>', text, count=1)
            return ET.fromstring(text)
        except Exception as e2:
            raise ET.ParseError(f"Failed to parse XDF (tried UTF-8 and Latin-1): {e2}")


def scale_from_equation_enhanced(eq: str, axis_context: dict = None,
                                  linked_vars: dict = None) -> tuple:
    """
    Extract (factor, offset, mask, note) from equation.

    For linear equations (factor*X + offset), extracts factor and offset directly.
    For non-linear equations, attempts evaluation at X=0, X=1, X=2 to detect pattern.

    Args:
        eq: Equation string (e.g., "X*2", "X/10-40", "if(X>50;100;0)")
        axis_context: Optional axis context for multi-variable equations
        linked_vars: Optional linked variable values

    Returns:
        (factor, offset, mask, note)
        factor, offset: strings or "1"/"0" if non-linear
        mask: int or None (for bitfield detection)
        note: error/warning message, or "" if successful
    """
    eq = (eq or "X").strip()
    mask = None

    # Detect bitfield: X & 0xNN
    m = re.search(r"&\s*(0x[0-9A-Fa-f]+|\d+)", eq)
    if m:
        mask = int(m.group(1), 16 if m.group(1).lower().startswith("0x") else 10)

    # Try to extract linear factor/offset by evaluating at X=0, X=1, X=2
    b, err_b = evaluate_math(eq, 0, axis_context, linked_vars)
    one, err_one = evaluate_math(eq, 1, axis_context, linked_vars)
    two, err_two = evaluate_math(eq, 2, axis_context, linked_vars)

    if None in (b, one, two):
        # Non-linear or evaluation failed
        return "1", "0", mask, f"non-linear equation '{eq}'"

    # Calculate factor (slope)
    factor = one - b

    # Check if linear (two should equal factor*2 + b within tolerance)
    if abs(two - (factor * 2 + b)) > 1e-9 and mask is None:
        return "1", "0", mask, f"non-linear equation '{eq}'"

    # Format as strings (int if integer value, else repr)
    def fmt_num(v):
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        return repr(v)

    return fmt_num(factor), fmt_num(b), mask, ""


def _evaleq(eq: str, x: float):
    if not set(eq) <= _SAFE:
        return None
    try:
        return eval(_VAR_RE.sub(f"({x})", eq), {"__builtins__": {}}, {})
    except Exception:
        return None


def scale_from_equation(eq: str):
    """Return (factor, offset, mask_or_None, note). Handles linear eqs and
    `X & 0xNN` bitfields."""
    eq = (eq or "X").strip()
    mask = None
    m = re.search(r"&\s*(0x[0-9A-Fa-f]+|\d+)", eq)
    if m:
        mask = int(m.group(1), 16 if m.group(1).lower().startswith("0x") else 10)
    b = _evaleq(eq, 0)
    one = _evaleq(eq, 1)
    two = _evaleq(eq, 2)
    if None in (b, one, two):
        return "1", "0", mask, f"unparsed equation '{eq}'"
    a = one - b
    if abs(two - (a * 2 + b)) > 1e-9 and mask is None:
        return "1", "0", mask, f"non-linear equation '{eq}'"
    return _num(a), _num(b), mask, ""


def _num(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return repr(v)


def _bits_from_mask(mask: int):
    """(bitcount, bitoffset) covered by a contiguous mask."""
    if not mask:
        return None, 0
    off = (mask & -mask).bit_length() - 1
    return bin(mask >> off).count("1"), off


_ENUM_RE = re.compile(r"(?:enum|values?)\s*:\s*(.+)$", re.I)
_PAIR_RE = re.compile(r"^\s*([^=:]+?)\s*[=:]\s*(.+)$")


def _enum_from_desc(desc: str) -> str:
    """Pull an enumeration from a description into SI_ENUM form. Handles both
    'values: 0=Disabled; 1=Enabled' and A2L-style 'Enum: 0:Label,1:Label'
    (colon separators / comma or semicolon delimited) -> '0=Disabled,1=Enabled'."""
    m = _ENUM_RE.search(desc or "")
    if not m:
        return ""
    body = m.group(1).strip().rstrip(".")
    out = []
    for p in re.split(r"[;,]", body):
        mm = _PAIR_RE.match(p)
        if mm:
            out.append(f"{mm.group(1).strip()}={mm.group(2).strip()}")
    return ",".join(out)


def _clean_desc(desc: str) -> str:
    # strip the bracketed "[id ...]" and trailing "values:" block for the cax DESC
    d = re.sub(r"\[id[^\]]*\]", "", desc or "")
    d = _ENUM_RE.sub("", d)
    return d.strip()


# ---- parse XDF ---------------------------------------------------------------
def _embed(el):
    e = el.find("EMBEDDEDDATA")
    if e is None:
        return None, 16, 0, 0
    addr = e.get("mmedaddress")
    addr = int(addr, 16) if addr else None
    bits = int(e.get("mmedelementsizebits", "16"))
    rows = int(e.get("mmedrowcount", "0") or 0)
    cols = int(e.get("mmedcolcount", "0") or 0)
    return addr, bits, rows, cols


def _categories(el, catnames):
    cats = []
    for cm in el.findall("CATEGORYMEM"):
        idx = cm.get("category")
        if idx is not None and idx in catnames:
            cats.append(catnames[idx])
    return cats


def _major_minor(cats):
    if not cats:
        return "", ""
    name = cats[0]
    if " / " in name:
        a, b = name.split(" / ", 1)
        return a.strip(), b.strip()
    return name.strip(), ""


def parse_xdf(path: str) -> list:
    """Parse XDF file and extract all elements."""
    root = parse_xdf_safe(path)
    hdr = root.find("XDFHEADER")
    defaults = hdr.find("DEFAULTS") if hdr is not None else None
    signed_default = (defaults is not None and defaults.get("signed") == "1")
    catnames = {}
    if hdr is not None:
        for c in hdr.findall("CATEGORY"):
            # category index is hex like 0x1; CATEGORYMEM references decimal
            catnames[str(int(c.get("index"), 16))] = c.get("name", "")

    # NEW: Build uniqueid index for linked-var resolution
    uniqueid_index = _build_uniqueid_index(root)

    items = []
    for el in root.findall("XDFCONSTANT"):
        items.append(_scalar(el, catnames, signed_default, uniqueid_index))
    for el in root.findall("XDFFLAG"):
        items.append(_flag(el, catnames, uniqueid_index))
    for el in root.findall("XDFTABLE"):
        t = _table(el, catnames, signed_default, uniqueid_index)
        if t:
            items.append(t)
    return [i for i in items if i]


def _text(el, tag, default=""):
    e = el.find(tag)
    return (e.text or default).strip() if e is not None and e.text else default


def _math_eq(el):
    m = el.find("MATH")
    return m.get("equation", "X") if m is not None else "X"


def _scalar(el, catnames, signed_default, uniqueid_index):
    addr, bits, _r, _c = _embed(el)

    # NEW: Extract linked vars and use enhanced equation handler
    math_elem = el.find("MATH")
    linked_elems = _resolve_linked_vars(math_elem, uniqueid_index) if math_elem is not None else {}
    equation = _math_eq(el)
    factor, offset, mask, note = scale_from_equation_enhanced(
        equation,
        linked_vars={k: None for k in linked_elems.keys()}
    )

    bitoff = 0
    if mask is not None:
        nb, bitoff = _bits_from_mask(mask)
        if nb:
            bits = nb
    desc = _text(el, "description")
    enum = _enum_from_desc(desc)   # 'Enum: 0:Choice,1:Choice' -> selectable choices
    major, minor = _major_minor(_categories(el, catnames))
    return XdfItem("scalar", _text(el, "title"), _clean_desc(desc), addr, bits,
                   signed_default, units=_text(el, "units"),
                   prec=_text(el, "decimalpl", "0"), factor=factor, offset=offset,
                   bitoff=bitoff, enum=enum, major=major, minor=minor, note=note)


def _flag(el, catnames, uniqueid_index):
    addr, bits, _r, _c = _embed(el)
    mask = el.find("mask")
    maskv = int(mask.text, 16) if mask is not None and mask.text else 1
    nb, bitoff = _bits_from_mask(maskv)
    desc = _text(el, "description")
    enum = _enum_from_desc(desc) or "0=Off,1=On"
    major, minor = _major_minor(_categories(el, catnames))
    return XdfItem("flag", _text(el, "title"), _clean_desc(desc), addr, nb or 1,
                   False, prec="0", factor="1", offset="0", bitoff=bitoff,
                   enum=enum, major=major, minor=minor)


def _axis_labels(ax):
    labels = []
    for lab in ax.findall("LABEL"):
        labels.append(lab.get("value", ""))
    return labels


def _table(el, catnames, signed_default, uniqueid_index):
    axes = {ax.get("id"): ax for ax in el.findall("XDFAXIS")}
    z = axes.get("z")
    if z is None:
        return None
    addr, bits, rows, cols = _embed(z)

    # NEW: Extract linked vars and use enhanced equation handler
    math_elem = z.find("MATH")
    linked_elems = _resolve_linked_vars(math_elem, uniqueid_index) if math_elem is not None else {}
    equation = _math_eq(z)
    factor, offset, mask, note = scale_from_equation_enhanced(
        equation,
        linked_vars={k: None for k in linked_elems.keys()}
    )

    x, y = axes.get("x"), axes.get("y")
    col_labels = _axis_labels(x) if x is not None else []
    row_labels = _axis_labels(y) if y is not None else []
    cols = cols or (len(col_labels) or 1)
    rows = rows or (len(row_labels) or 1)
    display_rows = rows
    major, minor = _major_minor(_categories(el, catnames))
    return XdfItem("table", _text(el, "title"), _clean_desc(_text(el, "description")),
                   addr, bits, signed_default, units=_text(z, "units"),
                   prec=_text(z, "decimalpl", "0"), factor=factor, offset=offset,
                   colcount=cols, rowcount=display_rows,
                   col_units=_text(x, "units") if x is not None else "",
                   row_units=_text(y, "units") if y is not None else "",
                   col_labels=col_labels, row_labels=row_labels,
                   major=major, minor=minor, note=note)


# ---- build cax sections ------------------------------------------------------
def build_sections(items, segs, id_factory) -> tuple:
    """Return (sections, report). id_factory(seg) -> a fresh unique cax id.

    When segment ranges are known, items whose address falls outside all flash
    segments (RAM variables from A2L, etc.) are SKIPPED — not imported.
    """
    import segmap
    D = cf.PLACEHOLDER
    report = XdfReport()
    sections = []
    for it in items:
        phys = None
        if segs and it.address is not None:
            phys = segmap.classify(it.address, segs)
            if phys is None:        # address is outside the known flash image
                report.skipped.append((it.title, it.address))
                continue
        elif segs and it.address is None:
            report.skipped.append((it.title, None))
            continue
        seg = PHYS_TO_V7SEG.get(phys, "") if phys else ""
        cid = id_factory(seg)
        if it.kind == "table" and it.rowcount > 1 and it.colcount > 1:
            display = "3"                       # Map (2D)
        elif it.kind == "table" and (it.rowcount > 1 or it.colcount > 1):
            display = "2"                       # Col (1D)
        else:
            display = "1"                       # Val (scalar / 1x1 table)
        dtype = "SED" if it.enum else ("SNM" if it.signed else "UNM")
        sec = cf.Section(cid)

        def rec(key, fields):
            sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key=key,
                                     fields=fields, edited=True))
        rec("DESC.001", [it.desc or it.title])
        rec("VIEW", [seg or D, it.title, it.major or "", it.minor or "",
                     "1", display, "0", "0"])
        addr = f"{it.address:X}" if it.address is not None else D
        rec("ADDR", [addr, str(it.bitoff), str(it.bits), dtype, "0"])
        rec("SI_SLOT", [it.units or D, it.factor, it.offset, it.prec, D, D, D, D])
        rec("SI_ENUM", [it.enum or D]); rec("IM_ENUM", [D])
        rec("SI_COL", [it.col_units or D,
                       ",".join(it.col_labels) if it.col_labels else D])
        rec("IM_COL", [D])
        rec("SI_ROW", [it.row_units or D,
                       ",".join(it.row_labels) if it.row_labels else D])
        rec("IM_ROW", [D])
        rec("ULABELS", [D, D, D])
        sections.append(sec)
        report.items.append(it)
        if it.note:
            report.review.append((it.title, it.note))
    return sections, report


# ---- export cax -> XDF -------------------------------------------------------
def _fmtnum(s: str) -> str:
    try:
        v = float(s)
        return str(int(v)) if v.is_integer() else repr(v)
    except (TypeError, ValueError):
        return s


def _equation(factor: str, offset: str) -> str:
    f = _fmtnum(factor or "1")
    o = _fmtnum(offset or "0")
    eq = "X"
    if f not in ("1", "1.0"):
        eq = f"X * {f}"
    if o not in ("0", "0.0"):
        eq = (eq + f" + {o}") if "*" in eq else f"X + {o}"
    return eq


def _fld(sec, key, i, default=""):
    r = sec.get(key)
    if r and i < len(r.fields):
        v = r.fields[i]
        return default if v == cf.PLACEHOLDER else v
    return default


def _desc_text(sec):
    parts = [ln.fields[0] for ln in sec.lines
             if ln.is_record and (ln.key or "").startswith("DESC")
             and ln.fields and ln.fields[0] != cf.PLACEHOLDER]
    return " ".join(parts)


def export_xdf(cax, path: str, deftitle: str = "Ultimate Cax Generator export"):
    """Write the in-memory calibrations to a TunerPro XDF file."""
    import caxtree as ct
    root = ET.Element("XDFFORMAT", version="1.60")
    hdr = ET.SubElement(root, "XDFHEADER")
    ET.SubElement(hdr, "flags").text = "0x1"
    ET.SubElement(hdr, "deftitle").text = deftitle
    ET.SubElement(hdr, "description").text = "Exported by Ultimate Cax Generator"
    ET.SubElement(hdr, "BASEOFFSET", offset="0", subtract="0")
    ET.SubElement(hdr, "DEFAULTS", datasizeinbits="16", sigdigits="2",
                  outputtype="1", signed="0", lsbfirst="0", float="0")

    # categories from each cal's Major[/Minor]
    cats, cat_index = [], {}
    def cat_for(major, minor):
        name = major + (f" / {minor}" if minor else "") if major else ""
        if not name:
            return None
        if name not in cat_index:
            cat_index[name] = len(cats)
            cats.append(name)
        return cat_index[name]

    bodies = []
    uid = 1
    for sec in cax.sections:
        cal = ct.cal_from_section(sec)
        if cal is None:
            continue
        addr = _fld(sec, "ADDR", 0)
        if not addr:
            continue
        try:
            addr_hex = f"0x{int(addr, 16):X}"
        except ValueError:
            continue
        bits = _fld(sec, "ADDR", 2, "16")
        bitoff = _fld(sec, "ADDR", 1, "0")
        dtype = _fld(sec, "ADDR", 3, "UNM")
        signed = "1" if dtype == "SNM" else "0"
        units = _fld(sec, "SI_SLOT", 0)
        factor = _fld(sec, "SI_SLOT", 1, "1")
        offset = _fld(sec, "SI_SLOT", 2, "0")
        prec = _fld(sec, "SI_SLOT", 3, "2")
        display = _fld(sec, "VIEW", 5, "1")
        ci = cat_for(cal.major, cal.minor)

        col_u, col_l = _split_axis(sec, "SI_COL")
        row_u, row_l = _split_axis(sec, "SI_ROW")
        is_table = display in ("2", "3") or col_l or row_l
        enum = _fld(sec, "SI_ENUM", 0)

        if is_table:
            el = ET.Element("XDFTABLE", uniqueid=f"0x{uid:X}", flags="0x0")
            ET.SubElement(el, "title").text = cal.caption
            ET.SubElement(el, "description").text = _desc_text(sec)
            if ci is not None:
                ET.SubElement(el, "CATEGORYMEM", index="0", category=str(ci))
            _axis(el, "x", col_u, col_l)
            _axis(el, "y", row_u, row_l)
            z = ET.SubElement(el, "XDFAXIS", id="z")
            ET.SubElement(z, "EMBEDDEDDATA", mmedaddress=addr_hex,
                          mmedelementsizebits=bits,
                          mmedrowcount=str(max(len(row_l), 1)),
                          mmedcolcount=str(max(len(col_l), 1)))
            ET.SubElement(z, "units").text = units
            ET.SubElement(z, "decimalpl").text = prec
            _math(z, _equation(factor, offset))
        elif bits == "1" or (enum and dtype == "SED" and bits in ("1", "2")):
            el = ET.Element("XDFFLAG", uniqueid=f"0x{uid:X}")
            ET.SubElement(el, "title").text = cal.caption
            d = _desc_text(sec)
            if enum:
                d = (d + "  values: " + enum.replace(",", "; ")).strip()
            ET.SubElement(el, "description").text = d
            if ci is not None:
                ET.SubElement(el, "CATEGORYMEM", index="0", category=str(ci))
            nbits = int(bits) if bits.isdigit() else 1
            mask = ((1 << nbits) - 1) << int(bitoff or "0")
            ET.SubElement(el, "EMBEDDEDDATA", mmedaddress=addr_hex,
                          mmedelementsizebits="16")
            ET.SubElement(el, "mask").text = f"0x{mask:02X}"
        else:
            el = ET.Element("XDFCONSTANT", uniqueid=f"0x{uid:X}", flags="0x0")
            ET.SubElement(el, "title").text = cal.caption
            ET.SubElement(el, "description").text = _desc_text(sec)
            if ci is not None:
                ET.SubElement(el, "CATEGORYMEM", index="0", category=str(ci))
            ET.SubElement(el, "EMBEDDEDDATA", mmedaddress=addr_hex,
                          mmedelementsizebits=bits)
            ET.SubElement(el, "units").text = units
            ET.SubElement(el, "decimalpl").text = prec
            ET.SubElement(el, "datatype").text = "0"
            ET.SubElement(el, "unittype").text = "0"
            _math(el, _equation(factor, offset))
        bodies.append(el)
        uid += 1

    for i, name in enumerate(cats):
        ET.SubElement(hdr, "CATEGORY", index=f"0x{i:X}", name=name)
    for el in bodies:
        root.append(el)

    ET.indent(root, space="  ")
    tree = ET.ElementTree(root)
    with open(path, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="utf-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)
    return len(bodies)


def _split_axis(sec, key):
    u = _fld(sec, key, 0)
    labels_str = _fld(sec, key, 1)
    labels = [x.strip() for x in labels_str.split(",") if x.strip()] if labels_str else []
    return u, labels


def _axis(parent, axis_id, units, labels):
    ax = ET.SubElement(parent, "XDFAXIS", id=axis_id)
    ET.SubElement(ax, "units").text = units
    ET.SubElement(ax, "indexcount").text = str(max(len(labels), 1))
    for i, lab in enumerate(labels):
        ET.SubElement(ax, "LABEL", index=str(i), value=lab)
    _math(ax, "X")


def _math(parent, equation):
    m = ET.SubElement(parent, "MATH", equation=equation)
    ET.SubElement(m, "VAR", id="X")

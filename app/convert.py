"""
convert — V7 .cax  ->  V8 .cax8 conversion, with a verification report.

V8 is column-oriented: a few big tables ([Data], [Calibration], [Enum], [Axis],
[Description]) instead of V7's one-section-per-calibration. See template.cax8 and
docs/FORMAT_V8.md for the field layouts.

The converter's job is twofold:
  1. Produce a correct .cax8 text.
  2. Report every calibration that would be DROPPED or needs REVIEW — especially
     segment drops (the documented #1 cause of "params not coming across").
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import caxformat as cf
import caxtree as ct
import units as unitsmod
from segments import ControllerMap


# V7 ADDR/SLOT field indices (see docs/FORMAT.md)
A_ADDR, A_BITOFF, A_BITS, A_TYPE, A_INV = range(5)
S_UNITS, S_FACTOR, S_OFFSET, S_PREC, S_MINLIM, S_MAXLIM, S_MINUSR, S_MAXUSR = range(8)

# V7 DisplayType -> V8 Display keyword
DISPLAY_MAP = {"1": "Value", "2": "Curve", "3": "Map"}
DOT = "."


@dataclass
class CalReport:
    cal_id: str
    caption: str
    v7_seg: str
    v8_seg: str | None = None
    status: str = "ok"           # 'ok' | 'dropped' | 'review'
    reasons: list[str] = field(default_factory=list)


@dataclass
class ConversionReport:
    controller: str
    cals: list[CalReport] = field(default_factory=list)

    @property
    def dropped(self) -> list[CalReport]:
        return [c for c in self.cals if c.status == "dropped"]

    @property
    def review(self) -> list[CalReport]:
        return [c for c in self.cals if c.status == "review"]

    def summary(self) -> str:
        ok = sum(1 for c in self.cals if c.status == "ok")
        return (f"Controller {self.controller}: {len(self.cals)} calibrations — "
                f"{ok} ok, {len(self.review)} review, {len(self.dropped)} DROPPED")


def _f(fields: list[str], i: int, default: str = "") -> str:
    if i < len(fields):
        v = fields[i]
        return default if v == cf.PLACEHOLDER else v
    return default


def _sanitize_unit(unit: str, field_name: str, rpt: CalReport) -> str:
    """Normalize a unit to a valid V8 unit. Canonical units pass through; known
    third-party spellings are mapped (SanitizeUnits.ini); an un-mappable unit is
    left as-is but flagged for review (likely a user-defined unit)."""
    u = (unit or "").strip()
    if not u or u in (cf.PLACEHOLDER, "None"):
        return u
    if unitsmod.is_canonical(u):
        return u
    canon = unitsmod.sanitize(u)
    if canon:
        rpt.reasons.append(f"{field_name} {u!r} normalized to {canon!r}")
        return canon
    rpt.status = "review"
    rpt.reasons.append(f"{field_name} {u!r} is not a valid V8 unit "
                       "(add as a user-defined unit in EFILive, or fix it)")
    return u


def _hex_to_v8(addr: str) -> str:
    """V7 bare-hex address -> V8 '$' hex (comma lists preserved).

    Multi-column calibrations carry a comma-separated address list; some V7
    tools wrap each address in quotes (e.g. '"1e209c","1e20b0"'). Strip those so
    the V8 output is '$1e209c,$1e20b0' rather than '$"1e209c",...' (which EFILive
    rejects as an invalid unsigned integer)."""
    out = []
    for part in addr.split(","):
        p = part.strip().strip('"').strip()
        if not p:
            continue
        out.append(p if p.startswith("$") else f"${p}")
    return ",".join(out)


def _size_field(bitoff: str, bits: str, rpt: CalReport) -> str:
    """V7 BitOffset+Bits -> V8 Size 'B[:M:S]'. Flags sub-byte bitfields."""
    try:
        nbits = int(bits)
    except ValueError:
        return bits or "8"
    try:
        off = int(bitoff or "0")
    except ValueError:
        off = 0
    if off == 0 and nbits in (8, 16, 32, 64):
        return str(nbits)
    # Sub-field within a container — compute container/mask/shift and flag it.
    container = 8
    while container < off + nbits:
        container *= 2
    mask = (1 << nbits) - 1
    rpt.status = "review" if rpt.status == "ok" else rpt.status
    rpt.reasons.append(
        f"bit-packed field (offset {off}, {nbits} bits) -> Size {container}:${mask:X}:{off} — verify mask/shift")
    return f"{container}:${mask:X}:{off}"


def _quote(s: str) -> str:
    if s == "":
        return ""
    if any(c.isspace() for c in s) or "," in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


def convert(cax: cf.CaxFile, controller: str, cmap: ControllerMap | None,
            provider: dict[str, str] | None = None) -> tuple[str, ConversionReport]:
    report = ConversionReport(controller)
    provider = provider or {}

    data_rows: list[str] = []
    cal_rows: list[str] = []
    enums: dict[str, str] = {}          # name -> value
    axes: dict[str, str] = {}
    descriptions: list[str] = []
    # content -> shared name, so identical enums/axes are emitted once (EFILive style)
    enum_by_content: dict[str, str] = {}
    axis_by_content: dict[str, str] = {}

    for sec in cax.sections:
        cal = ct.cal_from_section(sec)
        if cal is None:
            continue  # [OS]/metadata, no VIEW -> not a displayable calibration
        rpt = CalReport(cal.id, cal.caption, cal.seg)
        report.cals.append(rpt)

        # --- segment remap (the drop check) ---
        if cmap is not None:
            hit = cmap.v8_for(cal.seg)
            if hit is None:
                rpt.status = "dropped"
                rpt.reasons.append(
                    f"V7 segment {cal.seg!r} has no mapping for controller {controller} "
                    "— EFILive would silently drop this calibration")
                continue
            rpt.v8_seg = hit[0]
        else:
            rpt.v8_seg = cal.seg
            rpt.status = "review"
            rpt.reasons.append("no segment map loaded — V8 segment not verified")

        addr = sec.get("ADDR")
        slot = sec.get("SI_SLOT")
        if addr is None or not _f(addr.fields, A_ADDR):
            rpt.status = "review"
            rpt.reasons.append("missing ADDR/address — cannot place in bin")
        af = addr.fields if addr else []
        sf = slot.fields if slot else []

        size = _size_field(_f(af, A_BITOFF, "0"), _f(af, A_BITS, "8"), rpt)
        dtype = _f(af, A_TYPE, "UNM")
        # Unitless params (enums/flags/raw counts) are normal — EFILive writes
        # DataUnits "None", not a review flag. Non-canonical units are normalized
        # to valid V8 units (SanitizeUnits.ini) so they don't error in V8.
        # SED (enum) calibrations are unitless by definition -> "none".
        if dtype.upper() == "SED":
            units = "none"
        else:
            units = _sanitize_unit(_f(sf, S_UNITS), "DataUnits", rpt) or "None"

        # column/row axes: SI_COL/SI_ROW = Units, "comma,separated,labels"
        col_units, col_labels = _axis(sec.get("SI_COL"))
        row_units, row_labels = _axis(sec.get("SI_ROW"))
        if dtype.upper() == "SED":
            # An enum with multiple options is a switch -> col/row units 'switch';
            # otherwise the enum axes are unitless ('none', where an axis exists).
            if enum_option_count(sec.get("SI_ENUM")) >= 2:
                col_units = row_units = "switch"
            else:
                col_units = "none" if col_labels else col_units
                row_units = "none" if row_labels else row_units
        else:
            col_units = _sanitize_unit(col_units, "ColUnits", rpt)
            row_units = _sanitize_unit(row_units, "RowUnits", rpt)
        # EFILive's "Curve" display (from V7 DisplayType 2 "Col") is a *vertical*
        # column of cells: the breakpoint axis must be the ROW axis. A V7 curve
        # authored with the breakpoints on SI_COL (more col labels than row) is
        # transposed so it renders correctly (RowCount=N, ColCount=1) instead of
        # as a wide 1-row strip.
        display = DISPLAY_MAP.get(_view(sec, 5), "Value")
        if display == "Curve" and len(col_labels) > len(row_labels):
            col_labels, row_labels = row_labels, col_labels
            col_units, row_units = row_units, col_units
        colcount = str(len(col_labels)) if col_labels else "1"
        rowcount = str(len(row_labels)) if row_labels else "1"

        col_ref = row_ref = DOT
        if col_labels:
            col_ref = _shared(axes, axis_by_content, ",".join(col_labels),
                              _axis_name(col_labels))
        if row_labels:
            row_ref = _shared(axes, axis_by_content, ",".join(row_labels),
                              _axis_name(row_labels))

        # [Enum] from SI_ENUM (already a single "tag=value,..." field in V7)
        enum_ref = DOT
        enum_str = _enum_str(sec.get("SI_ENUM"))
        if enum_str:
            enum_ref = _shared(enums, enum_by_content, enum_str,
                               _enum_name(enum_str))

        # ---- [Data] row ----
        data_rows.append(_row([
            _quote(cal.id), _hex_to_v8(_f(af, A_ADDR)), colcount, rowcount,
            _f(sf, S_FACTOR, "1"), _f(sf, S_OFFSET, "0"), _f(af, A_INV, "0"),
            size, dtype, "0",  # DataOffset
            _f(sf, S_MINLIM, DOT), _f(sf, S_MAXLIM, DOT),
            _quote(units) or DOT, _quote(col_units) or DOT, _quote(row_units) or DOT,
            DOT, DOT, DOT,  # Deposit, Endian, Align -> controller default
        ]))

        # ---- [Calibration] row ----
        tree = "\\".join(p for p in (cal.major, cal.minor) if p)
        cal_rows.append(_row([
            _quote(cal.id), rpt.v8_seg or DOT, _quote(tree) or DOT,
            DOT,                      # Flags (TODO: derive from visibility/group)
            display, _f(sf, S_MINUSR, DOT), _f(sf, S_MAXUSR, DOT),
            enum_ref, DOT,            # Enum, Options
            DOT, _f(sf, S_PREC, "0"), # DataFmt, DPrec
            DOT, DOT, col_ref,        # ColFmt, ColDesc, ColLabels
            DOT, DOT, row_ref,        # RowFmt, RowDesc, RowLabels
        ]))

        # ---- [Description] ----
        desc = _description(sec)
        if desc:
            descriptions.append(f"<!-- #{cal.id} {cal.caption} -->\n{desc}")

    text = _emit(provider, enums, axes, data_rows, cal_rows, descriptions)
    return text, report


# ---- helpers ----------------------------------------------------------------
# ---- V8 -> V7 (load a .cax8 into the editor) --------------------------------
_V8_DISPLAY = {"Value": "1", "Curve": "2", "Map": "3", "List": "4"}


def v7_section_from_v8(cid: str, d: dict, c: dict, axes: dict, enums: dict) -> "cf.Section":
    """Reconstruct a V7 Section from V8 [Data]+[Calibration] rows, resolving
    [Enum]/[Axis] references back into SI_ENUM/SI_COL/SI_ROW."""
    sec = cf.Section(cid)

    def rec(key, fields):
        sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key=key,
                                 fields=fields, edited=True))

    def axis_labels(ref: str) -> str:
        ref = (ref or "").strip().strip('"')
        if ref.startswith("'"):       # literal label list
            return ref[1:]
        ref = ref.lstrip("@")          # @xxx or plain axis name
        return axes.get(ref, "")

    size = (d.get("Size") or "8").split(":")[0]
    addr = (d.get("Address") or "").replace("$", "")
    tree = (c.get("Tree") or "").strip('"')
    parts = tree.split("\\") if tree else []
    major = parts[0] if parts else DOT
    minor = parts[1] if len(parts) > 1 else DOT
    disp = _V8_DISPLAY.get(c.get("Display", "Value"), "1")
    rec("DESC.001", [""])
    rec("VIEW", [c.get("Seg") or DOT, cid, major or "", minor or "", "1", disp, "0", "0"])
    rec("ADDR", [addr or DOT, "0", size, d.get("Type") or "UNM", d.get("Inv") or "0"])
    rec("SI_SLOT", [d.get("DataUnits") or DOT, d.get("Factor") or "1",
                    d.get("Offset") or "0", c.get("DPrec") or "0",
                    d.get("HardMin") or DOT, d.get("HardMax") or DOT,
                    c.get("SoftMin") or DOT, c.get("SoftMax") or DOT])
    enum_val = enums.get(c.get("Enum", ""), "")
    rec("SI_ENUM", [enum_val or DOT]); rec("IM_ENUM", [DOT])
    col_lbl = axis_labels(c.get("ColLabels", ""))
    row_lbl = axis_labels(c.get("RowLabels", ""))
    rec("SI_COL", [d.get("ColUnits") or DOT, col_lbl or DOT]); rec("IM_COL", [DOT])
    rec("SI_ROW", [d.get("RowUnits") or DOT, row_lbl or DOT]); rec("IM_ROW", [DOT])
    rec("ULABELS", [DOT, DOT, DOT])
    return sec


def load_v8(path: str) -> "cf.CaxFile":
    """Load a .cax8 into an editable V7-style CaxFile (best effort)."""
    import cax8format as c8
    f = c8.parse_file(path)
    cax = cf.CaxFile(path=path)
    for cid in sorted(f.cal_ids):
        sec = v7_section_from_v8(cid, f.data.get(cid, {}),
                                 f.calibration.get(cid, {}), f.axes, f.enums)
        cax.sections.append(sec)
    cf.validate(cax)
    # Preserve the original [Provider] so a round-trip save keeps it.
    cax.v8_provider = dict(f.provider)
    return cax


def _default_provider() -> dict:
    from datetime import date
    return {"Version": "8.3.1.0", "Date": date.today().strftime("%Y.%m.%d"),
            "Name": "Ultimate Cax Generator", "UniqueID": "U01"}


def write_v8(cax: "cf.CaxFile", path: str, provider: dict | None = None,
             controller: str = "", cmap=None):
    """Serialize the in-memory CaxFile to a .cax8. Writes via a .bak swap.

    With cmap=None: emits VIEW.Seg AS-IS (round-trip of an opened V8 file).
    With a learned cmap + controller: remaps V7 seg numbers -> V8 (Save Versions
    from a V7 file). Returns the ConversionReport.
    """
    import os
    prov = provider or getattr(cax, "v8_provider", None) or _default_provider()
    text, rpt = convert(cax, controller=controller, cmap=cmap, provider=prov)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="latin-1", newline="\r\n") as fh:
        fh.write(cf.to_latin1(text))
    if os.path.exists(path):
        bak = path + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(path, bak)
    os.replace(tmp, path)
    return rpt


def _shared(table: dict, by_content: dict, content: str, name: str) -> str:
    """Reuse an existing [Enum]/[Axis] entry with identical content; otherwise
    add it under a unique name. Returns the reference name."""
    if content in by_content:
        return by_content[content]
    base, n = name, 2
    while name in table and table[name] != content:
        name = f"{base}_{n}"
        n += 1
    table[name] = content
    by_content[content] = name
    return name


def _ident(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z]+", "", str(s))
    return s or "x"


def _enum_name(enum_str: str) -> str:
    """EFILive-style shared enum name, e.g. '0=Disabled,1=Enabled' ->
    'Disabled_to_Enabledx2'."""
    parts = [p for p in enum_str.split(",") if "=" in p]
    if not parts:
        return "Enum"
    first = parts[0].split("=", 1)[1]
    last = parts[-1].split("=", 1)[1]
    return f"{_ident(first)}_to_{_ident(last)}x{len(parts)}"


def _axis_name(labels: list) -> str:
    """e.g. ['0','6',...,'100'] -> '0_to_100x17'."""
    if not labels:
        return "Axis"
    return f"{_ident(labels[0])}_to_{_ident(labels[-1])}x{len(labels)}"


def _row(fields: list[str]) -> str:
    return "  ".join(f if f != "" else DOT for f in fields)


def _axis(line: cf.Line | None) -> tuple[str, list[str]]:
    """SI_ROW/COL = Units, "comma,separated,labels". Returns (units, labels)."""
    if line is None or not line.fields:
        return "", []
    units = line.fields[0]
    units = "" if units == cf.PLACEHOLDER else units
    labels_str = line.fields[1] if len(line.fields) > 1 else ""
    if labels_str in ("", cf.PLACEHOLDER):
        return units, []
    labels = [x.strip() for x in labels_str.split(",") if x.strip()]
    return units, labels


def _enum_str(line: cf.Line | None) -> str:
    """SI_ENUM = "tag=value,tag=value" (single field). Returns it raw, or ''."""
    if line is None or not line.fields:
        return ""
    v = line.fields[0]
    return "" if v in ("", cf.PLACEHOLDER) else v


def enum_option_count(line: "cf.Line | None") -> int:
    """Number of options in an SI_ENUM/IM_ENUM ('0=a,1=b' -> 2). 0 if none."""
    s = _enum_str(line)
    return len([p for p in s.split(",") if p.strip()]) if s else 0


def _view(sec: cf.Section, idx: int) -> str:
    v = sec.get("VIEW")
    if v and idx < len(v.fields):
        return v.fields[idx]
    return ""


def _description(sec: cf.Section) -> str:
    parts = [ln.fields and " ".join(ln.fields) or ln.text.split("=", 1)[-1].strip()
             for ln in sec.lines if ln.is_record and (ln.key or "").startswith("DESC")]
    parts = [p for p in parts if p and p != cf.PLACEHOLDER]
    return "<br>".join(parts)


def _pad_name(name: str) -> str:
    """Pad an [Enum]/[Axis] name to EFILive's column 53 (width 52). A name that
    already fills or overflows that width still gets at least one space so it
    never butts against the opening quote of the value."""
    return name.ljust(52) if len(name) < 52 else name + " "


def _emit(provider, enums, axes, data_rows, cal_rows, descriptions) -> str:
    out: list[str] = []
    out.append("; Generated by CAX Editor (V7 -> V8 conversion)")
    out.append("")
    out.append("[Provider]")
    for k in ("Version", "Date", "Name", "UniqueID", "Description", "Url", "Contact"):
        if provider.get(k):
            out.append(f"{k:<20}{provider[k]}")
    out.append("")
    if enums:
        out.append("[Enum]")
        for name, val in enums.items():
            # EFILive emits enum/axis label lists UNQUOTED (comma-separated). A
            # surrounding "..." makes EFILive read the whole list as ONE label
            # (columns then collapse into a single header cell).
            out.append(f'{_pad_name(name)}{val}')
        out.append("")
    if axes:
        out.append("[Axis]")
        for name, val in axes.items():
            out.append(f'{_pad_name(name)}{val}')
        out.append("")
    out.append("[Data]")
    out.extend(data_rows)
    out.append("")
    out.append("[Calibration]")
    out.extend(cal_rows)
    out.append("")
    out.append("[Description]")
    out.extend(descriptions)
    out.append("")
    return "\n".join(out)

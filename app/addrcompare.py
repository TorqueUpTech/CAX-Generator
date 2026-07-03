"""
addrcompare — compare two calibration files keyed by ADDRESS, not ID.

IDs differ between files (and between V7/V8), but the controller address
identifies the same physical parameter. Works on .cax (V7) and .cax8 (V8).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import caxformat as cf
import cax8format as c8
import caxtree as ct


@dataclass
class CalInfo:
    addr: str          # normalized first address (hex, upper, no $)
    cal_id: str
    caption: str
    dtype: str
    bits: str
    factor: str
    offset: str
    units: str


@dataclass
class AddrDiff:
    addr: str
    a: CalInfo
    b: CalInfo
    fields: list[tuple[str, str, str]] = field(default_factory=list)  # name, a, b


@dataclass
class CompareReport:
    path_a: str
    path_b: str
    only_a: list[CalInfo] = field(default_factory=list)
    only_b: list[CalInfo] = field(default_factory=list)
    matched: list[AddrDiff] = field(default_factory=list)  # only those WITH diffs
    same: int = 0                                          # matched & identical

    def summary(self) -> str:
        return (f"A={len(self.only_a) + self.same + len(self.matched)} cals, "
                f"B={len(self.only_b) + self.same + len(self.matched)} cals | "
                f"matched {self.same + len(self.matched)} by address "
                f"({self.same} identical, {len(self.matched)} differ), "
                f"{len(self.only_a)} only in A, {len(self.only_b)} only in B")


def _norm_addr(a: str) -> str:
    a = a.replace('"', "").strip()
    a = a.split(",")[0].strip().lstrip("$")
    try:
        return f"{int(a, 16):X}"
    except ValueError:
        return a.upper()


def _f(line, i):
    if line and i < len(line.fields):
        v = line.fields[i]
        return "" if v == cf.PLACEHOLDER else v
    return ""


def collect(path: str) -> dict[str, CalInfo]:
    """addr_key -> CalInfo for every calibration in a .cax or .cax8 file."""
    out: dict[str, CalInfo] = {}
    if path.lower().endswith(".cax8"):
        f = c8.parse_file(path)
        for cid in f.cal_ids:
            d = f.data.get(cid, {})
            c = f.calibration.get(cid, {})
            addr = _norm_addr(d.get("Address", ""))
            if not addr:
                continue
            out[addr] = CalInfo(addr, cid, cid, d.get("Type", ""),
                                d.get("Size", ""), d.get("Factor", ""),
                                d.get("Offset", ""), d.get("DataUnits", ""))
    else:
        cax = cf.parse_file(path)
        for sec in cax.sections:
            cal = ct.cal_from_section(sec)
            if cal is None:
                continue
            addr = _norm_addr(_f(sec.get("ADDR"), 0))
            if not addr:
                continue
            out[addr] = CalInfo(addr, sec.name, cal.caption,
                                _f(sec.get("ADDR"), 3), _f(sec.get("ADDR"), 2),
                                _f(sec.get("SI_SLOT"), 1), _f(sec.get("SI_SLOT"), 2),
                                _f(sec.get("SI_SLOT"), 0))
    return out


# V8 Display -> V7 DisplayType (inverse of convert.DISPLAY_MAP, +List)
_V8_DISPLAY = {"Value": "1", "Curve": "2", "Map": "3", "List": "4"}


def _v7_section_from_v8(cid, d, c) -> cf.Section:
    """Build a V7 Section from a V8 [Data]+[Calibration] row (best effort)."""
    D = cf.PLACEHOLDER
    sec = cf.Section(cid)

    def rec(key, fields):
        sec.lines.append(cf.Line(cf.LineKind.RECORD, "", key=key,
                                 fields=fields, edited=True))
    size = (d.get("Size") or "8").split(":")[0]
    addr = (d.get("Address") or "").replace("$", "")
    tree = (c.get("Tree") or "").strip('"')
    parts = tree.split("\\") if tree else []
    major = parts[0] if parts else D
    minor = parts[1] if len(parts) > 1 else D
    disp = _V8_DISPLAY.get(c.get("Display", "Value"), "1")
    rec("DESC.001", [""])
    rec("VIEW", [c.get("Seg") or D, cid, major or D, minor or D, "1", disp, "0", "0"])
    rec("ADDR", [addr or D, "0", size, d.get("Type") or "UNM", d.get("Inv") or "0"])
    rec("SI_SLOT", [d.get("DataUnits") or D, d.get("Factor") or "1",
                    d.get("Offset") or "0", c.get("DPrec") or "0",
                    d.get("HardMin") or D, d.get("HardMax") or D,
                    c.get("SoftMin") or D, c.get("SoftMax") or D])
    rec("SI_ENUM", [D]); rec("IM_ENUM", [D])
    rec("SI_COL", [D]); rec("IM_COL", [D])
    rec("SI_ROW", [D]); rec("IM_ROW", [D])
    rec("ULABELS", [D, D, D])
    return sec


def import_sections(path_b: str, addresses) -> list[cf.Section]:
    """Return ready-to-append V7 Sections from B for the given (normalized) addrs."""
    want = set(addresses)
    out: list[cf.Section] = []
    if path_b.lower().endswith(".cax8"):
        f = c8.parse_file(path_b)
        for cid in f.cal_ids:
            d = f.data.get(cid, {})
            if _norm_addr(d.get("Address", "")) in want:
                out.append(_v7_section_from_v8(cid, d, f.calibration.get(cid, {})))
    else:
        cax = cf.parse_file(path_b)  # fresh parse -> safe to reuse sections
        for sec in cax.sections:
            if ct.cal_from_section(sec) is None:
                continue
            if _norm_addr(_f(sec.get("ADDR"), 0)) in want:
                out.append(sec)
    return out


def compare(path_a: str, path_b: str) -> CompareReport:
    a = collect(path_a)
    b = collect(path_b)
    rpt = CompareReport(path_a, path_b)
    for addr in sorted(set(a) - set(b)):
        rpt.only_a.append(a[addr])
    for addr in sorted(set(b) - set(a)):
        rpt.only_b.append(b[addr])
    for addr in sorted(set(a) & set(b)):
        ca, cb = a[addr], b[addr]
        diffs = []
        for name, x, y in [("Id", ca.cal_id, cb.cal_id),
                           ("Caption", ca.caption, cb.caption),
                           ("Type", ca.dtype, cb.dtype),
                           ("Bits", ca.bits, cb.bits),
                           ("Factor", ca.factor, cb.factor),
                           ("Offset", ca.offset, cb.offset),
                           ("Units", ca.units, cb.units)]:
            if x != y:
                diffs.append((name, x, y))
        if diffs:
            rpt.matched.append(AddrDiff(addr, ca, cb, diffs))
        else:
            rpt.same += 1
    return rpt

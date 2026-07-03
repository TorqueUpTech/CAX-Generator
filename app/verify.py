"""
verify — diff a V7 .cax against EFILive's converted V8 .cax8 and report:
  * DROPPED  : V7 calibrations missing from the V8 output (the "didn't come across")
  * ADDED    : V8 calibrations not present in the V7 source (factory/auto-added)
  * CHANGED  : field-level differences for calibrations present in both

This is empirical — it shows what EFILive actually did, regardless of why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import caxformat as cf
import cax8format as c8
import caxtree as ct

# V7 DisplayType -> V8 Display keyword (4 = List is undocumented in the V7 header
# but appears in real data and maps to V8 "List").
V7_DISPLAY = {"1": "Value", "2": "Curve", "3": "Map", "4": "List"}


@dataclass
class FieldDiff:
    field: str
    v7: str
    v8: str


@dataclass
class CalDiff:
    cal_id: str
    caption: str
    diffs: list[FieldDiff] = field(default_factory=list)


@dataclass
class VerifyReport:
    v7_path: str
    v8_path: str
    dropped: list[tuple[str, str, str]] = field(default_factory=list)  # id, caption, cause
    added: list[str] = field(default_factory=list)
    changed: list[CalDiff] = field(default_factory=list)
    v7_count: int = 0
    v8_count: int = 0

    def summary(self) -> str:
        return (f"V7 {self.v7_count} cals -> V8 {self.v8_count} cals | "
                f"{len(self.dropped)} dropped, {len(self.added)} added, "
                f"{len(self.changed)} changed")


def _norm_addr(a: str) -> str:
    a = a.replace('"', "").strip()        # drop stray quotes from multi-addr lists
    a = a.split(",")[0].strip().lstrip("$")  # first address of a multi-col list
    try:
        return f"{int(a, 16):X}"
    except ValueError:
        return a.upper()


def _v7_cals(cax: cf.CaxFile) -> dict[str, ct.CalNode]:
    out = {}
    for sec in cax.sections:
        cal = ct.cal_from_section(sec)
        if cal is not None:
            out[cal.id] = cal
    return out


def _classify_drop(cal: ct.CalNode) -> str:
    # EFILive silently ignores cal IDs that already exist in its factory defs
    # (per the V7 .cax header rule). That's the dominant cause of silent drops.
    return "likely factory cal-ID collision (silently ignored by EFILive)"


def verify(cax: cf.CaxFile, c8f: c8.Cax8File, v7_path="", v8_path="") -> VerifyReport:
    rpt = VerifyReport(v7_path, v8_path)
    v7 = _v7_cals(cax)
    v8_ids = c8f.cal_ids
    rpt.v7_count = len(v7)
    rpt.v8_count = len(v8_ids)

    for cid, cal in v7.items():
        if cid not in v8_ids:
            rpt.dropped.append((cid, cal.caption, _classify_drop(cal)))
    for cid in sorted(v8_ids - set(v7)):
        rpt.added.append(cid)

    for cid in sorted(set(v7) & v8_ids):
        cal = v7[cid]
        sec = cal.section
        d = c8f.data.get(cid, {})
        c = c8f.calibration.get(cid, {})
        diffs: list[FieldDiff] = []

        # address
        addr7 = _addr(sec, 0)
        if addr7 and d.get("Address"):
            if _norm_addr(addr7) != _norm_addr(d["Address"]):
                diffs.append(FieldDiff("Address", addr7, d["Address"]))
        # data type
        t7 = _addr(sec, 3)
        if t7 and d.get("Type") and t7 != d["Type"]:
            diffs.append(FieldDiff("Type", t7, d["Type"]))
        # units
        u7 = _slot(sec, 0)
        u8 = d.get("DataUnits", "")
        if u7 and u8 and u8 not in (".", u7):
            diffs.append(FieldDiff("DataUnits", u7, u8))
        # segment
        if c.get("Seg") and cal.seg and c["Seg"] != cal.seg:
            diffs.append(FieldDiff("Seg", cal.seg, c["Seg"]))
        # display
        disp7 = V7_DISPLAY.get(_view(sec, 5), _view(sec, 5))
        if c.get("Display") and disp7 and c["Display"] != disp7:
            diffs.append(FieldDiff("Display", disp7, c["Display"]))

        if diffs:
            rpt.changed.append(CalDiff(cid, cal.caption, diffs))
    return rpt


def _addr(sec: cf.Section, i: int) -> str:
    r = sec.get("ADDR")
    return _f(r, i)


def _slot(sec: cf.Section, i: int) -> str:
    r = sec.get("SI_SLOT")
    return _f(r, i)


def _view(sec: cf.Section, i: int) -> str:
    r = sec.get("VIEW")
    return _f(r, i)


def _f(line, i) -> str:
    if line and i < len(line.fields):
        v = line.fields[i]
        return "" if v == cf.PLACEHOLDER else v
    return ""


def verify_paths(v7_path: str, v8_path: str) -> VerifyReport:
    cax = cf.parse_file(v7_path)
    c8f = c8.parse_file(v8_path)
    return verify(cax, c8f, v7_path, v8_path)

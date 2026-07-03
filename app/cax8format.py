"""
cax8format — minimal reader for EFILive V8 .cax8 files.

Enough to verify conversions: parses the [Data] and [Calibration] column tables
(plus [Enum]/[Axis]/[Description]) into per-Cal-ID dicts. See docs/FORMAT_V8.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from caxformat import split_fields

DATA_COLS = ["Address", "ColCount", "RowCount", "Factor", "Offset", "Inv", "Size",
             "Type", "DataOffset", "HardMin", "HardMax", "DataUnits", "ColUnits",
             "RowUnits", "Deposit", "Endian", "Align"]
CAL_COLS = ["Seg", "Tree", "Flags", "Display", "SoftMin", "SoftMax", "Enum",
            "Options", "DataFmt", "DPrec", "ColFmt", "ColDesc", "ColLabels",
            "RowFmt", "RowDesc", "RowLabels"]


@dataclass
class Cax8File:
    provider: dict[str, str] = field(default_factory=dict)
    data: dict[str, dict[str, str]] = field(default_factory=dict)        # calid -> cols
    calibration: dict[str, dict[str, str]] = field(default_factory=dict)  # calid -> cols
    enums: dict[str, str] = field(default_factory=dict)
    axes: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    path: str | None = None

    @property
    def cal_ids(self) -> set[str]:
        return set(self.data) | set(self.calibration)


def _strip_comment(line: str) -> str:
    """Remove a ';' comment outside of double quotes."""
    out, inq = [], False
    for ch in line:
        if ch == '"':
            inq = not inq
        if ch == ";" and not inq:
            break
        out.append(ch)
    return "".join(out)


def _logical_lines(text: str):
    """Yield lines, honouring '\\' continuation (outside quoted strings)."""
    buf = ""
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        # count quotes to know if we're inside a string at line end
        stripped = line.rstrip()
        if stripped.endswith("\\") and stripped.count('"') % 2 == 0:
            buf += stripped[:-1]
            continue
        buf += line
        yield buf
        buf = ""
    if buf:
        yield buf


def parse(text: str, path: str | None = None) -> Cax8File:
    f = Cax8File(path=path)
    section = None
    for logical in _logical_lines(text):
        body = _strip_comment(logical).rstrip()
        s = body.strip()
        if s == "":
            continue
        if s.startswith("[") and s.endswith("]"):
            section = s[1:-1].strip().lower()
            continue
        if section == "provider":
            parts = split_fields(body)
            if parts:
                f.provider[parts[0]] = parts[1] if len(parts) > 1 else ""
        elif section in ("data", "calibration"):
            parts = split_fields(body)
            if not parts:
                continue
            calid = parts[0]
            cols = DATA_COLS if section == "data" else CAL_COLS
            row = {cols[i]: (parts[i + 1] if i + 1 < len(parts) else "")
                   for i in range(len(cols))}
            (f.data if section == "data" else f.calibration)[calid] = row
        elif section == "enum":
            parts = split_fields(body)
            if parts:
                f.enums[parts[0]] = parts[1] if len(parts) > 1 else ""
        elif section == "axis":
            parts = split_fields(body)
            if parts:
                f.axes[parts[0]] = parts[1] if len(parts) > 1 else ""
        # [description] parsed lazily elsewhere if needed
    return f


def parse_file(path: str) -> Cax8File:
    with open(path, "r", encoding="latin-1") as fh:
        return parse(fh.read(), path)

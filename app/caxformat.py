"""
caxformat — read/write/validate EFILive .cax calibration-definition files.

Pure-Python, no Qt. This is the core that must round-trip files faithfully:
comments, blank lines, and unknown/bad records are preserved so saving never
silently loses data (matching the original CAXEdit V1.00 behaviour).

Format reference: docs/FORMAT.md
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


# ---- Known record keys and their expected field counts ----------------------
# DESC is special (DESC.NNN, free text). *_ENUM/_ROW/_COL carry comma-lists.
# Field counts are validated to reproduce the original's
# "<Rec> record contains an incorrect number of fields." diagnostics.
FIELD_COUNTS: dict[str, int] = {
    "VIEW": 8,      # Seg, Caption, Major, Minor, Visibility, DisplayType, Force2D, Group
    "ADDR": 5,      # Address(es), BitOffset, BitCount, DataType, Inverse
    "SI_SLOT": 8,   # Units, Factor, Offset, Prec, MinLimit, MaxLimit, MinUser, MaxUser
    "IM_SLOT": 8,
    "ULABELS": 3,   # Body, Col, Row
}
# List-valued keys: tokenize as Units(optional) + comma-separated label list.
LIST_KEYS = {"SI_ENUM", "IM_ENUM", "SI_ROW", "IM_ROW", "SI_COL", "IM_COL"}
ROWCOL_KEYS = {"SI_ROW", "IM_ROW", "SI_COL", "IM_COL"}  # Units + Labels
ENUM_KEYS = {"SI_ENUM", "IM_ENUM"}                       # Labels only

DATA_TYPES = {"UNM", "SNM", "SED", "FLT"}  # FLT = 4-byte IEEE float (V7 & V8)
PLACEHOLDER = "."


class LineKind(Enum):
    COMMENT = "comment"   # starts with ';'
    BLANK = "blank"
    RECORD = "record"     # KEY = values
    RAW = "raw"           # anything we couldn't classify (preserved verbatim)


@dataclass
class Line:
    """One physical line within a section (or the file preamble)."""
    kind: LineKind
    text: str                       # original verbatim text (no trailing newline)
    key: str | None = None          # for RECORD
    fields: list[str] = field(default_factory=list)  # parsed value tokens
    errors: list[str] = field(default_factory=list)  # validation messages
    edited: bool = False             # True once fields changed (re-render on save)
    lineno: int = 0                  # 1-based source line in the file (0 if synthetic)

    @property
    def is_record(self) -> bool:
        return self.kind is LineKind.RECORD

    def set_fields(self, fields: list[str]) -> None:
        """Update a record's fields and mark it for re-render on save."""
        self.fields = list(fields)
        self.edited = True


@dataclass
class Section:
    name: str                       # 'B9021', 'OS', ...
    lines: list[Line] = field(default_factory=list)
    lineno: int = 0                 # 1-based source line of the [header]

    def records(self) -> list[Line]:
        return [ln for ln in self.lines if ln.is_record]

    def get(self, key: str) -> Line | None:
        for ln in self.lines:
            if ln.is_record and ln.key == key:
                return ln
        return None


@dataclass
class CaxFile:
    preamble: list[Line] = field(default_factory=list)   # comments before first section
    sections: list[Section] = field(default_factory=list)
    path: str | None = None

    def section(self, name: str) -> Section | None:
        for s in self.sections:
            if s.name == name:
                return s
        return None

    @property
    def errors(self) -> list[tuple[str, Line]]:
        """All (section_name, line) pairs that failed validation."""
        out: list[tuple[str, Line]] = []
        for s in self.sections:
            for ln in s.lines:
                if ln.errors:
                    out.append((s.name, ln))
        return out


# ---- Tokenizer --------------------------------------------------------------
def split_fields(value: str) -> list[str]:
    """Whitespace-separated fields honouring double quotes; "" escapes a quote.

    A bare '.' is kept as the placeholder. Quoted fields keep their content
    (quotes stripped); empty quoted field -> ''.
    """
    fields: list[str] = []
    i, n = 0, len(value)
    while i < n:
        if value[i].isspace():
            i += 1
            continue
        if value[i] == '"':
            i += 1
            buf = []
            while i < n:
                if value[i] == '"':
                    if i + 1 < n and value[i + 1] == '"':  # "" -> literal "
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(value[i])
                i += 1
            fields.append("".join(buf))
        else:
            start = i
            while i < n and not value[i].isspace():
                i += 1
            fields.append(value[start:i])
    return fields


def quote_field(s: str) -> str:
    """Re-quote a field for output if it contains spaces, commas or quotes."""
    if s == "":
        return '""'
    if any(c.isspace() for c in s) or "," in s or '"' in s:
        return '"' + s.replace('"', '""') + '"'
    return s


# ---- Parser -----------------------------------------------------------------
def _classify(text: str) -> Line:
    stripped = text.strip()
    if stripped == "":
        return Line(LineKind.BLANK, text)
    if stripped.startswith(";"):
        return Line(LineKind.COMMENT, text)
    if "=" in text and not stripped.startswith("["):
        key, _, value = text.partition("=")
        key = key.strip()
        # DESC.NNN values are free text (one paragraph per record) — keep raw,
        # don't tokenize. Everything else splits into whitespace fields.
        fields = [value.strip()] if key.startswith("DESC") else split_fields(value)
        return Line(LineKind.RECORD, text, key=key, fields=fields)
    return Line(LineKind.RAW, text)


def parse(text: str, path: str | None = None) -> CaxFile:
    cax = CaxFile(path=path)
    current: Section | None = None
    for i, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = Section(name=stripped[1:-1].strip())
            current.lineno = i
            cax.sections.append(current)
            continue
        line = _classify(raw)
        line.lineno = i
        (current.lines if current is not None else cax.preamble).append(line)
    validate(cax)
    return cax


def parse_file(path: str) -> CaxFile:
    # .cax files are Latin-1 (EFILive copyright uses 0xA9); be lenient.
    with open(path, "r", encoding="latin-1") as f:
        return parse(f.read(), path=path)


# ---- Validation (reproduces original diagnostics) ---------------------------
def _expected_count(key: str) -> int | None:
    if key.startswith("DESC"):
        return None  # free text
    return FIELD_COUNTS.get(key)


def validate(cax: CaxFile) -> None:
    for sec in cax.sections:
        seen: set[str] = set()
        for ln in sec.lines:
            ln.errors.clear()
            if not ln.is_record:
                continue
            key = ln.key or ""
            base = key.split(".")[0]
            # Duplicate key within section (DESC.NNN allowed to repeat).
            if not key.startswith("DESC"):
                if key in seen:
                    ln.errors.append(f"{base} record duplicate.")
                seen.add(key)
            # Field count.
            exp = _expected_count(key)
            if exp is not None and len(ln.fields) != exp:
                ln.errors.append(
                    f"{base} record contains an incorrect number of fields.")
            # Per-field syntax (light checks; extend as needed).
            _validate_fields(ln)


def _is_placeholder(f: str) -> bool:
    return f == PLACEHOLDER


def _validate_fields(ln: Line) -> None:
    key = ln.key or ""
    f = ln.fields
    if key == "ADDR" and len(f) == 5:
        # field 2 BitOffset 0-7, field 3 BitCount int, field 4 DataType, field5 0/1
        if not _is_placeholder(f[1]) and not _is_int(f[1]):
            ln.errors.append("Bad Addr field syntax, field 2")
        if not _is_placeholder(f[2]) and not _is_int(f[2]):
            ln.errors.append("Bad Addr field syntax, field 3")
        if not _is_placeholder(f[3]) and f[3].upper() not in DATA_TYPES:
            ln.errors.append("Bad Addr field syntax, field 4")
    elif key == "VIEW" and len(f) == 8:
        # Visibility (idx 4) is a bitmapped value that may be written in $hex
        # (license level $00-$03 OR'd with $10/$20/$40/$80 flags); the others are
        # small decimal ints. Accept decimal or $hex for all of them.
        for idx in (0, 4, 5, 6, 7):
            if not _is_placeholder(f[idx]) and not _is_num(f[idx]):
                ln.errors.append(f"Bad View field syntax, field {idx + 1}")


def _is_int(s: str) -> bool:
    try:
        int(s, 10)
        return True
    except ValueError:
        return False


def _is_num(s: str) -> bool:
    """Decimal, or EFILive $-prefixed hex (e.g. '$21')."""
    if s.startswith("$"):
        try:
            int(s[1:], 16)
            return True
        except ValueError:
            return False
    return _is_int(s)


# ---- Writer -----------------------------------------------------------------
def serialize_record(ln: Line) -> str:
    """Re-emit a record line. Preserves verbatim text unless fields changed."""
    return ln.text  # default: byte-faithful. (Edited records call render_record.)


def render_record(key: str, fields: Iterable[str], *, pad: int = 10) -> str:
    """Build a record line from structured fields (used after edits)."""
    body = "  ".join(quote_field(x) for x in fields)
    return f"{key:<{pad}}= {body}"


def serialize(cax: CaxFile) -> str:
    out: list[str] = []
    for ln in cax.preamble:
        out.append(ln.text)
    for sec in cax.sections:
        out.append(f"[{sec.name}]")
        for ln in sec.lines:
            if ln.is_record and ln.edited:
                if (ln.key or "").startswith("DESC"):
                    out.append(f"{(ln.key or ''):<10}= {ln.fields[0] if ln.fields else ''}")
                else:
                    out.append(render_record(ln.key or "", ln.fields))
            elif (ln.key or "") == "VIEW" and len(ln.fields) >= 4 and \
                    (ln.fields[2] == PLACEHOLDER or ln.fields[3] == PLACEHOLDER):
                # Normalize empty Major/Minor "." -> "" (EFILive renders "." as a
                # literal folder; "" means no folder).
                f = list(ln.fields)
                if f[2] == PLACEHOLDER:
                    f[2] = ""
                if f[3] == PLACEHOLDER:
                    f[3] = ""
                out.append(render_record("VIEW", f))
            else:
                out.append(ln.text)
    return "\n".join(out) + "\n"


# Common Unicode -> Latin-1/ASCII transliterations (cax files are Latin-1).
_TRANSLIT = {
    '→': '->', '←': '<-', '↔': '<->', '⇒': '=>',
    '↑': '^', '↓': 'v', '²': '2', '³': '3',
    '½': '1/2', '¼': '1/4', '¾': '3/4',
    '–': '-', '—': '-', '‘': "'", '’': "'",
    '“': '"', '”': '"', '…': '...', '×': 'x',
    '−': '-', '·': '.', '•': '*', '₀': '0',
}


def to_latin1(s: str) -> str:
    """Make a string Latin-1 writable: transliterate common symbols, replace any
    remaining out-of-range chars with '?'."""
    for k, v in _TRANSLIT.items():
        if k in s:
            s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def write_file(cax: CaxFile, path: str) -> None:
    """Write with a .bak swap so a failed write never destroys the original."""
    import os
    data = to_latin1(serialize(cax))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="latin-1", newline="\r\n") as f:
        f.write(data)
    if os.path.exists(path):
        bak = path + ".bak"
        if os.path.exists(bak):
            os.remove(bak)
        os.replace(path, bak)
    os.replace(tmp, path)

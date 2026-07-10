"""
canmod — export EFILive .cax field definitions to the Can-Display cal-mod pipeline.

Qt-free core (matches the project's unit-testable-core convention). Pulls a field
definition (addr / datatype / scaling / units / enum) out of a parsed CaxFile for the
calibrations the user ticks and writes a `.canmod` JSON (per-OS mod *definitions*, no
values).

`.canmod` is the handoff to the standalone **GM Can Display Configurator**, which owns
the `.canmod` -> CANDISP.DB (build/browse) half plus the binary format itself.

GM calibrations are big-endian (PowerPC / Gen-IV). MVP scope: single-value (Val)
calibrations with one byte-aligned address. Multi-column tables (Col/Map) and
sub-byte bitfields are flagged, not yet emitted.
"""
from __future__ import annotations

import json
import os
import re
import struct


def osid_from_filename(name: str) -> str:
    """Best-effort OS part number pulled from a file name. GM OS/cal part numbers are
    8-digit (e.g. 24256125, 12639270), and cax/.canmod files are named for the OS — so the
    first 7-9 digit run in the stem is the OS. Returns "" when there is no such run (module
    prefixes like T43/E38 are only 2 digits and are ignored). Handles 'T43_24256125',
    '24256125.cax', 'e40_12598977_E40-2005-GTO-...'."""
    stem = os.path.splitext(os.path.basename(name or ""))[0]
    for run in re.findall(r"\d+", stem):
        if 7 <= len(run) <= 9:
            return run
    return ""

# CalModDb::Kind
MODULE_KIND = {"T43": 0, "E38": 1, "E67": 2}
KIND_NAME = {0: "T43", 1: "E38", 2: "E67"}

# Where the OSID (OS part number) lives in each module image
# (osid_format: 0=ascii,1=be-uint,2=le-uint). Used when the .cax has no [OS] PCM_OSID.
#   T43 @0x6000C: 8-char ASCII OS part number.
#   E38 @0xC060 : 4-byte BE-uint OS part number, e.g. 0x00C0DC26 = 12639270. This is
#     the OS EFILive shows (verified across bins: 12639270 @0xC060 / 12656930 @0xC060).
#     NOTE: the old 0x1000E/ASCII locator was WRONG -- 0x1000E holds a software/version
#     number that only sometimes equals the OS (e.g. this bin: "12639271" != OS 12639270),
#     so it silently failed the mod match. The E38 Cal part number is the BE-uint @0xC028.
DEFAULT_OSID_LOCATOR = {
    "T43": {"offset": 0x6000C, "len": 8, "format": "ascii"},
    "E38": {"offset": 0xC060,  "len": 4, "format": "be"},
    # E67 shares the E38 Gen-IV memory map: OS part number as a 4-byte BE-uint @0xC060
    # (verified on 12656942 -> 00 C1 21 2E). The dash recomputes checksums with the same
    # E38 six-segment @0x10000 table (FlashPrepare::prepareE67 == prepareE38).
    "E67": {"offset": 0xC060,  "len": 4, "format": "be"},
}
# E38/E67 calibration (Cal) part number location -- for display only, not the mod key.
E38_CAL_LOCATOR = {"offset": 0xC028, "len": 4, "format": "be"}
_FMT = {"ascii": 0, "be": 1, "le": 2}


# ---- .cax field extraction --------------------------------------------------
def _rec(sec, key):
    ln = sec.get(key)
    return ln.fields if ln else None


def _cax_hex(s: str) -> int:
    s = s.strip()
    return int(s[1:], 16) if s.startswith("$") else int(s, 16)  # cax addrs are hex, no 0x


def _num(x, default):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _description(sec) -> str:
    """Join a section's DESC.NNN records into one display string."""
    lines = [ln.fields[0] if ln.fields else ""
             for ln in sec.lines if ln.is_record and (ln.key or "").startswith("DESC")]
    return " ".join(t for t in lines if t)


def _enum_options(sec) -> list:
    """SI_ENUM as [{'value': int, 'label': str}]. EFILive stores explicit
    'raw=label' pairs (e.g. '0=No,1=Yes', '255=Illegal'); falls back to positional
    labels. Empty list when the field has no enum. Lets the Build DB tool show a
    labelled dropdown instead of asking for a raw 0/1."""
    er = _rec(sec, "SI_ENUM")
    if not er or not er[0] or er[0] in (".", "X"):
        return []
    out = []
    for tok in er[0].split(","):
        tok = tok.strip()
        if not tok:
            continue
        raw, sep, label = tok.partition("=")
        if sep:
            try:
                out.append({"value": int(raw.strip()), "label": label.strip()})
                continue
            except ValueError:
                pass
        out.append({"value": len(out), "label": tok})   # positional fallback
    return out


def _axis_labels(sec) -> list:
    """Row/col axis labels of a 1D table calibration (one base address, many labels
    laid at consecutive addresses -- e.g. TS073 CAN-ID list). Uses whichever axis
    carries labels; [] for a scalar or a 2D table (both axes labelled -> deferred).
    Matches convert._axis: SI_ROW/SI_COL = Units, "comma,separated,labels"."""
    def lbls(key):
        r = _rec(sec, key)
        if not r or len(r) < 2 or r[1] in (".", "", "X"):
            return []
        return [x.strip() for x in r[1].split(",") if x.strip()]
    row, col = lbls("SI_ROW"), lbls("SI_COL")
    if row and col:
        return []          # 2D table -- not expanded here
    return row or col


def extract_field(cax, section_name: str) -> dict | None:
    """A single calibration's definition, or None if it has no VIEW/ADDR."""
    sec = cax.section(section_name)
    if not sec:
        return None
    view = _rec(sec, "VIEW")
    addr = _rec(sec, "ADDR")
    si = _rec(sec, "SI_SLOT")
    if not view or not addr:
        return None

    # VIEW: Seg,Caption,Major,Minor,Visibility,DisplayType,Force2D,Group
    caption = view[1] if len(view) > 1 and view[1] not in (".", "") else section_name
    display_type = view[5] if len(view) > 5 else "1"

    # ADDR: Address(es),BitOffset,BitCount,DataType,Inverse
    addr_field = addr[0] if addr else "."
    addresses: list[int] = []
    if addr_field not in (".", ""):
        for tok in addr_field.split(","):
            try:
                addresses.append(_cax_hex(tok))
            except ValueError:
                pass
    bit_offset = int(addr[1]) if len(addr) > 1 and addr[1] not in (".", "") else 0
    bit_count = int(addr[2]) if len(addr) > 2 and addr[2] not in (".", "") else 8
    datatype = addr[3].upper() if len(addr) > 3 and addr[3] not in (".", "") else "UNM"
    inverse = len(addr) > 4 and addr[4] == "1"

    # SI_SLOT: Units,Factor,Offset,Prec,MinLimit,MaxLimit,MinUser,MaxUser
    si = si or []
    return {
        "id": section_name,
        "name": caption,
        "desc": _description(sec),
        "display_type": display_type,
        "addr": addresses,
        "bit_offset": bit_offset,
        "bit_count": bit_count,
        "datatype": datatype,
        "inverse": inverse,
        "si": {
            "units": si[0] if len(si) > 0 and si[0] not in (".", "") else "",
            "factor": _num(si[1], 1.0) if len(si) > 1 else 1.0,
            "offset": _num(si[2], 0.0) if len(si) > 2 else 0.0,
            "prec": int(_num(si[3], 0)) if len(si) > 3 else 0,
            "min": _num(si[4], None) if len(si) > 4 else None,
            "max": _num(si[5], None) if len(si) > 5 else None,
        },
        "enum": _enum_options(sec),
        "labels": _axis_labels(sec),
    }


def expand_field(field: dict) -> list:
    """Expand a multi-label calibration into one field per label. EFILive lays each
    label at the next 8/16-bit word: cell i is at base_addr + i*(bit_count/8) (1 byte
    for an 8-bit cal, 2 for 16-bit). Scalar / single-label fields pass through
    unchanged; sub-byte or multi-address fields are left as-is."""
    labels = field.get("labels") or []
    if len(labels) <= 1 or len(field.get("addr", [])) != 1:
        return [field]
    bc, bo = field.get("bit_count", 8), field.get("bit_offset", 0)
    if bo != 0 or bc % 8 != 0:
        return [field]
    stride = bc // 8
    base = field["addr"][0]
    out = []
    for i, lab in enumerate(labels):
        sub = dict(field)
        sub["addr"] = [base + i * stride]
        sub["id"] = f"{field['id']}[{lab}]"
        sub["name"] = f"{field['name']} ({lab})"
        sub["label"] = lab
        sub.pop("labels", None)
        out.append(sub)
    return out


def export_canmod(cax, section_names, module: str,
                  osid: str = "", osid_locator: dict | None = None) -> dict:
    """Build a `.canmod` definition dict for the ticked calibrations. Values are
    NOT included -- they are dialed in later (Build Can-Display DB)."""
    loc = dict(osid_locator or DEFAULT_OSID_LOCATOR.get(module, DEFAULT_OSID_LOCATOR["T43"]))
    osec = cax.section("OS")            # [OS] PCM_OSID gives the locator offset
    if osec:
        ln = osec.get("PCM_OSID")
        if ln and ln.fields and ln.fields[0] not in (".", "", "X"):
            try:
                loc["offset"] = _cax_hex(ln.fields[0])
            except ValueError:
                pass
    fields = []
    for n in section_names:
        fd = extract_field(cax, n)
        if fd:
            fields.extend(expand_field(fd))   # multi-label cals -> one field per label
    return {"module": module, "osid": osid, "osid_locator": loc, "fields": fields}


def write_canmod(canmod: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(canmod, f, indent=2)


def read_canmod(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

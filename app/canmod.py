"""
canmod — export EFILive .cax field definitions to the Can-Display cal-mod DB.

Qt-free core (matches the project's unit-testable-core convention). Two halves:

  * EXPORT: pull a field definition (addr / datatype / scaling / units / enum) out
    of a parsed CaxFile for the calibrations the user ticks -> a `.canmod` JSON
    (per-OS mod *definitions*, no values).
  * BUILD:  turn dialed-in engineering VALUES into raw bytes and write the binary
    CANDISP.DB (+ CANDISP.TXT manifest) that the dash reads. The binary layout MUST
    stay byte-compatible with the Can-Display dash `CalModDb` parser -- see
    Can-Display/docs/CAL-MOD-DB.md.

GM calibrations are big-endian (PowerPC / Gen-IV). MVP scope: single-value (Val)
calibrations with one byte-aligned address. Multi-column tables (Col/Map) and
sub-byte bitfields are flagged, not yet emitted.
"""
from __future__ import annotations

import json
import struct

# CalModDb::Kind
MODULE_KIND = {"T43": 0, "E38": 1}
KIND_NAME = {0: "T43", 1: "E38"}

# Where the OSID lives in each module image (osid_format: 0=ascii,1=be-uint,2=le-uint).
# T43 = the 8-char OS part number at 0x6000C (proven in the dash sim). E38 TBD.
DEFAULT_OSID_LOCATOR = {
    "T43": {"offset": 0x6000C, "len": 8, "format": "ascii"},
    "E38": {"offset": None,    "len": 8, "format": "ascii"},
}
_FMT = {"ascii": 0, "be": 1, "le": 2}


# ---- CRC-16/ARC (poly 0xA001, reflected) — mirrors FlashChecksum::crc16Arc -----
def crc16_arc(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFFFF


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
    }


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
    fields = [f for f in (extract_field(cax, n) for n in section_names) if f]
    return {"module": module, "osid": osid, "osid_locator": loc, "fields": fields}


def write_canmod(canmod: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(canmod, f, indent=2)


def read_canmod(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ---- value -> raw bytes -----------------------------------------------------
def value_to_bytes(field: dict, value, endian: str = "big") -> bytes:
    """Convert an engineering value to the raw bytes written at the field address.

    metric = raw*factor + offset  =>  raw = (metric - offset)/factor. SNM is signed,
    FLT is a 4-byte IEEE float, SED is a raw enum index. Raises on sub-byte fields
    (bit_offset != 0 or bit_count not a byte multiple) -- those need read-modify-
    write, not a plain patch (see docs/CAL-MOD-DB.md)."""
    bc, bo, dt = field["bit_count"], field["bit_offset"], field["datatype"]
    if bo != 0 or bc % 8 != 0:
        raise ValueError(f"{field['id']}: sub-byte field (bit_offset={bo}, bit_count={bc}) unsupported")
    nbytes = bc // 8
    order = "big" if endian == "big" else "little"

    if dt == "FLT":
        return struct.pack(">f" if order == "big" else "<f", float(value))

    if dt == "SED":
        raw = int(round(float(value)))                    # enum index
    else:
        factor = field["si"]["factor"] or 1.0
        offset = field["si"]["offset"] or 0.0
        metric = (1.0 / float(value)) if field.get("inverse") else float(value)
        raw = int(round((metric - offset) / factor))

    return raw.to_bytes(nbytes, byteorder=order, signed=(dt == "SNM"))


def field_patch(field: dict, value, endian: str = "big") -> dict:
    """One patch for a single-address field. Byte-aligned -> kind 0 OVERWRITE;
    a sub-byte field within one byte -> kind 1 RMW (mask+value bit-flag)."""
    if not field.get("addr"):
        raise ValueError(f"{field['id']}: no address")
    if len(field["addr"]) > 1:
        raise ValueError(f"{field['id']}: multi-column tables not supported yet")
    offset = field["addr"][0]
    bo, bc = field["bit_offset"], field["bit_count"]
    if bo == 0 and bc % 8 == 0:
        return {"kind": 0, "offset": offset, "bytes": value_to_bytes(field, value, endian)}
    if bo + bc > 8:
        raise ValueError(f"{field['id']}: bitfield spans a byte boundary "
                         f"(bit_offset={bo}, bit_count={bc}) unsupported")
    # sub-byte flag within one byte -> read-modify-write
    if field["datatype"] == "SED":
        raw = int(round(float(value)))                      # enum index
    else:
        factor = field["si"]["factor"] or 1.0
        off = field["si"]["offset"] or 0.0
        raw = int(round((float(value) - off) / factor))
    field_mask = (1 << bc) - 1
    mask = (field_mask << bo) & 0xFF
    val = ((raw & field_mask) << bo) & 0xFF
    return {"kind": 1, "offset": offset, "mask": bytes([mask]), "value": bytes([val])}


# ---- CANDISP.DB binary writer (byte-compatible with dash CalModDb) ----------
def _u16(x): return struct.pack("<H", x & 0xFFFF)
def _u32(x): return struct.pack("<I", x & 0xFFFFFFFF)


def build_candisp_db(records: list, locators: list) -> bytes:
    """records: [{kind:int, osid:bytes|str, label:str,
                  mods:[{name:str, patches:[{offset:int, bytes:bytes}]}]}]
       locators: [{kind:int, osid_len:int, osid_format:int|str, osid_offset:int}]"""
    payload = bytearray()
    for loc in locators:
        fmt = loc.get("osid_format", 0)
        fmt = _FMT.get(fmt, 0) if isinstance(fmt, str) else int(fmt)
        payload += bytes([loc["kind"] & 0xFF, loc["osid_len"] & 0xFF, fmt & 0xFF, 0])
        payload += _u32(loc["osid_offset"])
    for r in records:
        osid = r["osid"].encode("latin-1") if isinstance(r["osid"], str) else bytes(r["osid"])
        payload += bytes([r["kind"] & 0xFF, len(osid) & 0xFF]) + osid
        label = r["label"].encode("ascii", "replace")
        payload += _u16(len(label)) + label
        payload += _u16(len(r["mods"]))
        for m in r["mods"]:
            name = m["name"].encode("ascii", "replace")
            payload += _u16(len(name)) + name
            payload += _u16(len(m["patches"]))
            for p in m["patches"]:
                kind = p.get("kind", 0)
                payload += bytes([kind & 0xFF]) + _u32(p["offset"])
                if kind == 1:                 # RMW: len, mask[len], value[len]
                    mask, val = bytes(p["mask"]), bytes(p["value"])
                    payload += _u16(len(mask)) + mask + val
                else:                         # OVERWRITE: len, bytes[len]
                    b = bytes(p["bytes"])
                    payload += _u16(len(b)) + b

    header = bytearray(b"CDMD")
    header.append(2)                          # version (v2: per-patch kind byte)
    header.append(len(locators) & 0xFF)       # kindLocatorCount
    header += _u16(len(records))              # osRecordCount
    header += _u16(crc16_arc(bytes(payload))) # payloadCrc16 over payload
    header += b"\x00\x00"                     # reserved
    return bytes(header) + bytes(payload)


def manifest(records: list) -> str:
    """Human-readable CANDISP.TXT (the dash ignores it)."""
    out = []
    for r in records:
        osid = r["osid"].decode("latin-1") if isinstance(r["osid"], (bytes, bytearray)) else r["osid"]
        out.append(f"[{KIND_NAME.get(r['kind'], '?')} {osid}]  {r['label']}")
        for m in r["mods"]:
            parts = []
            for p in m["patches"]:
                if p.get("kind", 0) == 1:
                    parts.append(f"@0x{p['offset']:X} mask={bytes(p['mask']).hex().upper()} "
                                 f"val={bytes(p['value']).hex().upper()}")
                else:
                    parts.append(f"@0x{p['offset']:X}={bytes(p['bytes']).hex().upper()}")
            out.append(f"  - {m['name']}: {', '.join(parts)}")
        out.append("")
    return "\n".join(out)

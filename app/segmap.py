"""
segmap — authoritative address->segment classification.

Combines a UniversalPatcher SegmentConfig XML (segment names + pointer offsets)
with a controller bin (which holds the actual address table at those offsets) to
produce real, non-overlapping segment address ranges. Then any calibration
address can be classified to its CORRECT physical segment — independent of the
cax's (possibly wrong) VIEW.Seg assignment.

Pointer format proven against T43/24243170: at <Addresses>@OFFSET the bin holds
consecutive big-endian 32-bit (start,end) pairs; an optional *N reads N pairs.
"""
from __future__ import annotations

import json
import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

_APPDIR = os.path.dirname(os.path.abspath(__file__))
# Per-controller segment names + bin pointer offsets, consolidated from the
# UniversalPatcher SegmentConfig XMLs into one bundled JSON. The external XML
# folder is only a last-resort fallback for a controller not in the JSON.
SEGMENT_POINTERS_PATH = os.path.join(_APPDIR, "segment_pointers.json")
XML_DIR = r"G:\My Drive\UniversalPatcher\XML"
BIN_DIR = r"G:\My Drive\GM BIN"
CACHE_PATH = os.path.join(_APPDIR, "segment_ranges.json")

_ADDR_TOKEN = re.compile(r"@([0-9A-Fa-f]+)(?:\*(\d+))?")
_POINTERS = None


@dataclass
class Segment:
    name: str
    ranges: list[tuple[int, int]] = field(default_factory=list)


def _load_pointers() -> dict:
    global _POINTERS
    if _POINTERS is None:
        try:
            with open(SEGMENT_POINTERS_PATH, "r", encoding="utf-8") as f:
                _POINTERS = json.load(f)
        except (OSError, ValueError):
            _POINTERS = {}
    return _POINTERS


def parse_segment_pointers(xml_path: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """Parse one SegmentConfig XML -> [(name, [(offset, count), ...]), ...].
    Used by the build script (_build_segment_pointers.py) to make the JSON."""
    tree = ET.parse(xml_path)
    out = []
    for sc in tree.getroot().findall("SegmentConfig"):
        name = (sc.findtext("Name") or "").strip()
        addrs = (sc.findtext("Addresses") or "").strip()
        ptrs = [(int(off, 16), int(n or "1"))
                for off, n in _ADDR_TOKEN.findall(addrs)]
        if name and ptrs:
            out.append((name, ptrs))
    return out


def segment_pointers(controller: str) -> list[tuple[str, list[tuple[int, int]]]]:
    """Segment names + pointer offsets for a controller, from the bundled JSON
    (falls back to an external Drive XML if the controller isn't bundled)."""
    entry = _load_pointers().get((controller or "").lower())
    if entry:
        return [(name, [(off, cnt) for off, cnt in plist]) for name, plist in entry]
    want = (controller or "").lower() + ".xml"
    if os.path.isdir(XML_DIR):
        for fn in os.listdir(XML_DIR):
            if fn.lower() == want:
                return parse_segment_pointers(os.path.join(XML_DIR, fn))
    return []


def resolve_ranges(controller: str, bin_path: str) -> list[Segment]:
    """Read the real (start,end) ranges for each segment from the bin."""
    data = open(bin_path, "rb").read()
    segs = []
    for name, ptrs in segment_pointers(controller):
        seg = Segment(name)
        for off, count in ptrs:
            for k in range(count):
                base = off + k * 8
                if base + 8 <= len(data):
                    start, end = struct.unpack(">II", data[base:base + 8])
                    if end > start and end < len(data) * 4:
                        seg.ranges.append((start, end))
        if seg.ranges:
            segs.append(seg)
    return segs


def classify(addr: int, segs: list[Segment]) -> str | None:
    """Physical segment name whose range contains addr (None if outside all)."""
    for seg in segs:
        for lo, hi in seg.ranges:
            if lo <= addr <= hi:
                return seg.name
    return None


# ---- per-OS range cache (so a bin isn't needed after the first resolve) -----
def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def cached_segments(os_id: str) -> list[Segment] | None:
    entry = _load_cache().get(os_id)
    if not entry:
        return None
    return [Segment(s["name"], [tuple(r) for r in s["ranges"]])
            for s in entry["segments"]]


def cached_segments_for_controller(controller: str):
    """Ranges from any cached OS of the same controller (T42 etc. share the same
    boundaries). Returns (segs, source_os_id) or (None, None)."""
    if not controller:
        return None, None
    for osid, entry in _load_cache().items():
        if (entry.get("controller") or "").upper() == controller.upper():
            segs = [Segment(s["name"], [tuple(r) for r in s["ranges"]])
                    for s in entry["segments"]]
            return segs, osid
    return None, None


def cache_segments(os_id: str, controller: str, segs: list[Segment]) -> None:
    cache = _load_cache()
    cache[os_id] = {
        "controller": controller,
        "segments": [{"name": s.name, "ranges": [list(r) for r in s.ranges]}
                     for s in segs],
    }
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def find_bin_for_os(os_id: str, controller: str | None = None) -> str | None:
    """Best-effort: locate a bin whose name contains the OS id."""
    roots = []
    if controller:
        roots.append(os.path.join(BIN_DIR, controller))
    roots.append(BIN_DIR)
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dp, _dn, files in os.walk(root):
            for fn in files:
                if fn.lower().endswith(".bin") and os_id in fn:
                    return os.path.join(dp, fn)
    return None

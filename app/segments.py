"""
segments — parse EFILive's V7toV8_Segments.ini (the V7->V8 segment remap).

The map is per-controller and LOSSY: a V7 segment number may map to a different
V8 segment, or be absent entirely (in which case EFILive silently drops every
calibration in that segment during conversion). This module exposes the map so
the converter can warn about — and optionally override — those drops.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_INI = r"C:\Program Files (x86)\EFILive\V8\V7toV8_Segments.ini"
# V7->V8 segment map LEARNED from EFILive's own converted output (authoritative;
# the .ini above does NOT match real output). Bundled with the app.
LEARNED_SEGMAP = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "v7v8_segmap.json")


def load_learned_segmap() -> dict:
    try:
        with open(LEARNED_SEGMAP, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


@dataclass
class LearnedControllerMap:
    """V7->V8 seg map for one controller. Unknown segs pass through unchanged
    (so nothing is dropped during Save Versions)."""
    mapping: dict

    def v8_for(self, v7seg):
        s = str(v7seg).strip()
        return (self.mapping.get(s, s), "")   # never None -> converter won't drop


def learned_controller_map(controller: str) -> "LearnedControllerMap | None":
    learned = load_learned_segmap()
    m = learned.get(controller)
    if not m and controller:
        # no EFILive-converted pair to learn from (e.g. T87): borrow the map of a learned controller whose
        # UniversalPatcher segment list is identical by name and order (T87 == T87A: Boot/OS/Trans/TransDiag/System)
        try:
            import segmap
            mine = [n for n, _ in segmap.segment_pointers(controller)]
            if mine:
                for other, om in learned.items():
                    if om and [n for n, _ in segmap.segment_pointers(other)] == mine:
                        m = om
                        break
        except Exception:
            m = None
    return LearnedControllerMap(m) if m else None


@dataclass
class ControllerMap:
    name: str
    # v7 segment key (str, e.g. '7' or 'x') -> (v8 segment, description)
    mapping: dict[str, tuple[str, str]] = field(default_factory=dict)

    def v8_for(self, v7seg: str) -> tuple[str, str] | None:
        return self.mapping.get(str(v7seg).strip())


def v8_segment_names(controller: str, path: str = DEFAULT_INI) -> dict[str, str]:
    """{V8 segment number -> friendly name} for a controller, from EFILive's
    V7toV8_Segments.ini. V8 segment numbers are controller-specific, so this is
    how the editor labels the tree when authoring/editing a .cax8 natively.
    Returns {} if the controller isn't listed."""
    cm = parse_segments_ini(path).get(controller)
    if cm is None:
        return {}
    names: dict[str, str] = {}
    for _v7, (v8, desc) in cm.mapping.items():
        if v8 and desc and v8 not in names:
            names[v8] = desc
    return names


def parse_segments_ini(path: str = DEFAULT_INI) -> dict[str, ControllerMap]:
    controllers: dict[str, ControllerMap] = {}
    if not os.path.exists(path):
        return controllers
    current: ControllerMap | None = None
    with open(path, "r", encoding="latin-1") as f:
        for raw in f:
            line = raw.rstrip("\n")
            s = line.strip()
            if s.startswith(";") or s == "":
                continue
            if s.startswith("[") and s.endswith("]"):
                name = s[1:-1].strip()
                current = ControllerMap(name)
                controllers[name] = current
                continue
            if current is not None and "=" in s:
                key, _, rest = s.partition("=")
                key = key.strip()
                rest = rest.strip()
                # rest = "<v8seg>  <description...>"  (may have trailing ; comment)
                rest = rest.split(";", 1)[0].strip()
                parts = rest.split(None, 1)
                v8 = parts[0] if parts else ""
                desc = parts[1].strip() if len(parts) > 1 else ""
                current.mapping[key] = (v8, desc)
    return controllers


_OS_CONTROLLERS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "os_controllers.json")
_OS_TABLE = None


def controller_for_os(os_id: str) -> str | None:
    """Authoritative OS-number -> controller. Uses the bundled table first (works
    without EFILive installed), then the live Cam folders, then the segment-range
    cache (covers OSes EFILive ships no .cam for, e.g. T87A 24293216)."""
    global _OS_TABLE
    if _OS_TABLE is None:
        try:
            with open(_OS_CONTROLLERS, "r", encoding="utf-8") as f:
                _OS_TABLE = json.load(f)
        except (OSError, ValueError):
            _OS_TABLE = {}
    return _OS_TABLE.get(os_id) or controller_for_cam(os_id) or _controller_from_segcache(os_id)


def _controller_from_segcache(os_id: str) -> str | None:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "segment_ranges.json"),
                  "r", encoding="utf-8") as f:
            return (json.load(f).get(os_id) or {}).get("controller")
    except (OSError, ValueError):
        return None


def controller_for_cam(calib_id: str,
                       cam_root: str = r"C:\Program Files (x86)\EFILive\V8\Cam") -> str | None:
    """Find which controller folder contains <calib_id>.cam (best-effort)."""
    if not os.path.isdir(cam_root):
        return None
    target = f"{calib_id}.cam".lower()
    for ctrl in os.listdir(cam_root):
        d = os.path.join(cam_root, ctrl)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, f"{calib_id}.cam")):
            return ctrl
        # case-insensitive fallback
        if os.path.isdir(d):
            for fn in os.listdir(d):
                if fn.lower() == target:
                    return ctrl
    return None

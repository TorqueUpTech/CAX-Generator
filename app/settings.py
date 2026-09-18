"""
settings — persisted user preferences (QSettings).

Holds the default save directory for each format version so "Save as V7/V8"
dialogs open in the right place. Stored in the OS-native location
(registry on Windows under HKCU\\Software\\CAXEditor).
"""
from __future__ import annotations

import json
import os

from PySide6.QtCore import QSettings

# Persist to a readable INI file next to the app (portable), not the registry.
INI_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "caxedit_settings.ini")


_CACHED: QSettings | None = None


def _settings() -> QSettings:
    # One shared instance so writes are visible to subsequent reads immediately
    # (separate QSettings objects can read a stale, not-yet-synced INI file).
    global _CACHED
    if _CACHED is None:
        _CACHED = QSettings(INI_PATH, QSettings.IniFormat)
    return _CACHED


def ini_path() -> str:
    return INI_PATH


def _documents() -> str:
    return os.path.join(os.path.expanduser("~"), "Documents")


def save_dir(version: str) -> str:
    """Default save directory for a format version (falls back to ~/Documents)."""
    s = _settings()
    val = s.value(f"paths/{version}_dir", "", str)
    return val if val and os.path.isdir(val) else _documents()


def set_save_dir(version: str, path: str) -> None:
    _settings().setValue(f"paths/{version}_dir", path)


# ---- Linked OS numbers -------------------------------------------------------
# A master file (keyed by its OS-number filename stem) can carry a list of other
# OS numbers whose files share its exact layout. Saving the master then also
# writes a copy under each linked OS number. Stored as one JSON blob so the whole
# map lives under a single INI key.
#   { "12598977": {"targets": ["02598977", "01250150"], "dir": ""} }
# An empty "dir" means "save the copies in the same folder as the master".

def os_links() -> dict:
    raw = _settings().value("oslinks/map", "", str)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def os_link(stem: str) -> dict | None:
    """Link entry for a master OS number, or None. Shape: {targets: [...], dir}."""
    e = os_links().get(stem)
    if not isinstance(e, dict) or not e.get("targets"):
        return None
    return {"targets": list(e.get("targets", [])), "dir": e.get("dir", "")}


def set_os_link(stem: str, targets: list[str], dest_dir: str = "") -> None:
    """Create/replace the link entry for a master. Empty targets removes it."""
    m = os_links()
    if targets:
        m[stem] = {"targets": list(dict.fromkeys(targets)), "dir": dest_dir or ""}
    else:
        m.pop(stem, None)
    _settings().setValue("oslinks/map", json.dumps(m))


def remove_os_link(stem: str) -> None:
    set_os_link(stem, [])


def id_template(seg: str, default: str = "") -> str:
    """Calibration-ID template for a V7 segment number (e.g. 'B####')."""
    return _settings().value(f"idgen/{seg}_template", default, str)


def set_id_template(seg: str, template: str) -> None:
    _settings().setValue(f"idgen/{seg}_template", template)


def id_start(seg: str, default: int = 1) -> int:
    try:
        return int(_settings().value(f"idgen/{seg}_start", default))
    except (TypeError, ValueError):
        return default


def set_id_start(seg: str, start: int) -> None:
    _settings().setValue(f"idgen/{seg}_start", int(start))


# Master VIEW-option defaults applied to new calibrations (key -> default value).
VIEW_DEFAULTS = {"visibility": "1", "display": "1", "force2d": "0", "group": "0"}


def view_default(key: str) -> str:
    return _settings().value(f"viewdefaults/{key}", VIEW_DEFAULTS.get(key, ""), str)


def set_view_default(key: str, value: str) -> None:
    _settings().setValue(f"viewdefaults/{key}", value)


def text_editor() -> str:
    """External text-editor exe (falls back to notepad)."""
    val = _settings().value("ui/text_editor", "", str)
    return val if val else "notepad"


def set_text_editor(path: str) -> None:
    _settings().setValue("ui/text_editor", path)


def suggest_segment_enabled() -> bool:
    return _settings().value("ui/suggest_segment", True, bool)


def set_suggest_segment_enabled(on: bool) -> None:
    _settings().setValue("ui/suggest_segment", bool(on))


_DEFAULT_EFILIVE = {
    "v7": r"C:\Program Files (x86)\EFILive\V7.5\EFILive_Tune.exe",
    "v8": r"C:\Program Files (x86)\EFILive\V8\EFILive_ScanAndTune.exe",
}


def efilive_exe(ver: str) -> str:
    return _settings().value(f"efilive/{ver}_exe", _DEFAULT_EFILIVE.get(ver, ""), str)


def set_efilive_exe(ver: str, path: str) -> None:
    _settings().setValue(f"efilive/{ver}_exe", path)


def v8_uid_number() -> int:
    """V8 Provider UniqueID number — must be >= 100 (U00-U99 are auto-assigned
    by EFILive)."""
    try:
        return max(100, int(_settings().value("v8/uid_number", 100)))
    except (TypeError, ValueError):
        return 100


def set_v8_uid_number(n) -> None:
    _settings().setValue("v8/uid_number", max(100, int(n)))


def v8_uid_name() -> str:
    return _settings().value("v8/uid_name", "", str)


def set_v8_uid_name(name: str) -> None:
    _settings().setValue("v8/uid_name", name)


def v8_unique_id() -> str:
    """Full V8 UniqueID, e.g. 'U100' or 'U100TorqueUp'."""
    return f"U{v8_uid_number()}{v8_uid_name()}"


def tree_segment_order() -> str:
    """'v7' (default) or 'v8' — order the segment tree branches by V7 or V8 number."""
    return _settings().value("ui/tree_seg_order", "v7", str)


def set_tree_segment_order(value: str) -> None:
    _settings().setValue("ui/tree_seg_order", value)


def theme() -> str:
    return _settings().value("ui/theme", "Light", str)


def set_theme(name: str) -> None:
    _settings().setValue("ui/theme", name)


def bin_dir() -> str:
    """Folder where ECU .bin images are kept (matched to OS number for the hex view)."""
    return _settings().value("paths/bin_dir", "", str)


def set_bin_dir(path: str) -> None:
    _settings().setValue("paths/bin_dir", path)


def backup_dest() -> str:
    return _settings().value("backup/dest", "", str)


def set_backup_dest(path: str) -> None:
    _settings().setValue("backup/dest", path)


def backup_sources() -> list:
    """(label, path) source folders to back up — the configured V7 and V8 dirs."""
    return [("V7", save_dir("v7")), ("V8", save_dir("v8"))]


def recent_files() -> list:
    val = _settings().value("recent/files", [], list)
    return [p for p in (val or []) if isinstance(p, str)]


def add_recent_file(path: str, limit: int = 10) -> None:
    items = [p for p in recent_files() if p.lower() != path.lower()]
    items.insert(0, path)
    _settings().setValue("recent/files", items[:limit])


def last_open_dir() -> str:
    s = _settings()
    val = s.value("paths/last_open", "", str)
    return val if val and os.path.isdir(val) else _documents()


def set_last_open_dir(path: str) -> None:
    _settings().setValue("paths/last_open", path)

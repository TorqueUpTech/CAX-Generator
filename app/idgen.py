"""
idgen — auto-generate calibration IDs from a per-segment template.

A template is a prefix + a run of '#' (the zero-padded counter) + optional
suffix, e.g.:
    B####     -> B0001, B0002, ...        (seg 2 engine cal)
    TU7###    -> TU7001, TU7002, ...       (seg 7 system)
    D9###     -> D9001, ...                (custom block)

next_id() finds the lowest counter value >= start that isn't already used by an
existing calibration matching the same prefix/suffix/width.
"""
from __future__ import annotations

import re

# Per V7-segment default templates (first-letter convention from the .cax docs;
# seg 7 'System' has no single convention, so it's left blank for the user).
DEFAULT_ID_TEMPLATES: dict[str, str] = {
    "0": "A####",           # OS (seg 0); A = Operating System per the id convention
    "1": "",                # EFILive Custom Calibrations: no single id convention
    "2": "B####", "3": "C####", "4": "D####",
    "5": "E####", "6": "F####", "7": "", "8": "G####",
}


def parse_template(template: str):
    """Return (prefix, width, suffix) or None if no '#' counter present."""
    i = template.find("#")
    if i < 0:
        return None
    j = i
    while j < len(template) and template[j] == "#":
        j += 1
    return template[:i], j - i, template[j:]


def next_id(existing_ids, template: str, start: int = 1,
            end: int | None = None) -> str | None:
    """Next free ID for a template. Returns None if the template is empty or the
    range is exhausted."""
    parsed = parse_template(template)
    if parsed is None:
        return template or None
    prefix, width, suffix = parsed
    pat = re.compile("^" + re.escape(prefix) + r"(\d{%d})" % width
                     + re.escape(suffix) + "$")
    used = set()
    for cid in existing_ids:
        m = pat.match(cid)
        if m:
            used.add(int(m.group(1)))
    n = max(start, 0)
    cap = end if end is not None else (10 ** width) - 1
    while n in used:
        n += 1
    if n > cap:
        return None
    return f"{prefix}{n:0{width}d}{suffix}"

"""
caxtree — group a CaxFile's calibrations into the EFILive Navigator tree.

Tree shape (from each section's VIEW record):
    Segment -> Major -> Minor -> Caption(leaf = calibration section)
See docs/FORMAT.md and Tree-Capture.png.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from caxformat import CaxFile, Section, PLACEHOLDER


# Segment number -> friendly name (matches EFILive's navigator.txt numbering;
# editable by the user). Note segment 0 is the OS/PCM root and segment 1 is
# EFILive's own custom calibrations (NOT the OS) — see navigator.txt.
SEGMENT_NAMES: dict[str, str] = {
    "0": "Operating System",
    "1": "EFILive Custom Calibrations",
    "2": "Engine Calibration",
    "3": "Engine Diagnostics",
    "4": "Transmission Calibration",
    "5": "Transmission Diagnostics",
    "6": "Fuel System",
    "7": "System",
    "8": "Speedometer",
}

# DisplayType -> leaf kind (drives the icon).
DISPLAY_KIND = {"1": "val", "2": "col", "3": "map"}


@dataclass
class CalNode:
    """A leaf: one calibration."""
    section: Section
    caption: str
    seg: str
    major: str
    minor: str
    display: str          # 'val' | 'col' | 'map' | 'unknown'
    visibility: str = ""

    @property
    def id(self) -> str:
        return self.section.name


@dataclass
class Branch:
    """A folder node (segment / major / minor)."""
    label: str
    children: dict[str, "Branch"] = field(default_factory=dict)
    cals: list[CalNode] = field(default_factory=list)

    def child(self, label: str) -> "Branch":
        if label not in self.children:
            self.children[label] = Branch(label)
        return self.children[label]


def _field(view_fields: list[str], idx: int) -> str:
    if idx < len(view_fields):
        v = view_fields[idx]
        return "" if v == PLACEHOLDER else v
    return ""


def cal_from_section(sec: Section) -> CalNode | None:
    """Build a CalNode from a section's VIEW record. None for sections w/o VIEW."""
    view = sec.get("VIEW")
    if view is None:
        return None
    f = view.fields
    seg = _field(f, 0)
    caption = _field(f, 1) or sec.name
    major = _field(f, 2)
    minor = _field(f, 3)
    visibility = _field(f, 4)
    display = DISPLAY_KIND.get(_field(f, 5), "unknown")
    return CalNode(sec, caption, seg, major, minor, display, visibility)


# Reverse lookup: branch label -> V7 segment number (for writing VIEW.Seg).
SEG_BY_LABEL = {name: num for num, name in SEGMENT_NAMES.items()}


def seg_label(seg: str) -> str:
    return SEGMENT_NAMES.get(seg, f"Segment {seg}" if seg else "(no segment)")


def seg_number(label: str) -> str:
    """Branch label -> segment number ('' if not a known segment)."""
    if label in SEG_BY_LABEL:
        return SEG_BY_LABEL[label]
    if label.startswith("Segment "):
        return label.split(" ", 1)[1]
    return ""


def _addr_int(sec: Section) -> int | None:
    a = sec.get("ADDR")
    if not a or not a.fields or a.fields[0] == PLACEHOLDER:
        return None
    raw = a.fields[0].replace('"', "").split(",")[0].strip().lstrip("$")
    try:
        return int(raw, 16)
    except ValueError:
        return None


def segment_address_spans(cax: CaxFile, exclude: Section | None = None
                          ) -> dict[str, tuple[int, int]]:
    """Observed [min,max] address span per V7 segment, from the file's cals."""
    spans: dict[str, list[int]] = {}
    for sec in cax.sections:
        if sec is exclude:
            continue
        cal = cal_from_section(sec)
        if cal is None or not cal.seg:
            continue
        v = _addr_int(sec)
        if v is None:
            continue
        spans.setdefault(cal.seg, []).append(v)
    return {seg: (min(v), max(v)) for seg, v in spans.items()}


def suggest_segment(cax: CaxFile, addr: int, current_seg: str,
                    exclude: Section | None = None) -> str | None:
    """Suggest a segment for an address if it unambiguously falls inside exactly
    one *other* segment's observed span. Returns the seg number, or None when
    ambiguous/unknown/already correct."""
    spans = segment_address_spans(cax, exclude=exclude)
    hits = [seg for seg, (lo, hi) in spans.items() if lo <= addr <= hi]
    if len(hits) == 1 and hits[0] != current_seg:
        return hits[0]
    return None


def build_tree(cax: CaxFile, extra_folders: list[tuple[str, str, str]] | None = None,
               seg_order: list | None = None) -> Branch:
    """Return a synthetic root Branch holding the whole Navigator tree.

    All known segments are shown by default (even empty) so the user can create
    Major/Minor sub-folders under any of them. `extra_folders` is a list of
    (seg_label, major, minor) user-created empty folders to keep visible.
    `seg_order` is an optional list of V7 segment numbers giving the branch order
    (used to order by V8 instead of V7); segment names are unchanged.
    Sections without a VIEW ([OS]/malformed) collect under '(no VIEW)'.
    """
    root = Branch("Calibrations")
    # Seed every known segment (in the requested order) so they always appear.
    for num in (seg_order or list(SEGMENT_NAMES)):
        root.child(SEGMENT_NAMES.get(num, seg_label(num)))
    for sec in cax.sections:
        cal = cal_from_section(sec)
        if cal is None:
            root.child("(no VIEW)").cals.append(
                CalNode(sec, sec.name, "", "", "", "unknown"))
            continue
        node = root.child(seg_label(cal.seg))
        if cal.major:
            node = node.child(cal.major)
        if cal.minor:
            node = node.child(cal.minor)
        node.cals.append(cal)
    # Ensure user-created empty folders exist.
    for seg_lbl, major, minor in (extra_folders or []):
        node = root.child(seg_lbl)
        if major:
            node = node.child(major)
        if minor:
            node = node.child(minor)
    return root

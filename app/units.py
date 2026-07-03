"""EFILive unit knowledge (Qt-free).

Backed by the bundled `units.json` (built by `_build_units.py` from EFILive's
EFILiveSystem.ini + SanitizeUnits.ini). Provides:

  * the canonical unit whitelist (what V8 accepts), grouped for a dropdown,
  * `sanitize()` — map a third-party / legacy spelling to a canonical unit,
  * `conversion()` / `compute_im_slot()` — derive an IM_SLOT Factor/Offset from
    the SI_SLOT using EFILive's own conversion table.

A cax8's units MUST be canonical (or a user-defined unit added in EFILive
Properties); see the cax8 header. We never block — we warn and offer to fix.
"""
import json
import os

_PATH = os.path.join(os.path.dirname(__file__), "units.json")

_DATA = None
_SYMBOLS = None          # set of canonical symbols
_LOWER = None            # lower(symbol) -> symbol
_SAN_CI = None           # lower(variant) -> canonical (case-insensitive pass)


def _load():
    global _DATA, _SYMBOLS, _LOWER, _SAN_CI
    if _DATA is not None:
        return _DATA
    try:
        with open(_PATH, encoding="utf-8") as f:
            _DATA = json.load(f)
    except (OSError, ValueError):
        _DATA = {"units": [], "conversions": {}, "group_units": {},
                 "sanitize": {}}
    _SYMBOLS = {u[0] for u in _DATA["units"]}
    _LOWER = {u[0].lower(): u[0] for u in _DATA["units"]}
    _SAN_CI = {}
    for variant, canon in _DATA["sanitize"].items():
        _SAN_CI.setdefault(variant.lower(), canon)
    return _DATA


def units():
    """All canonical units as [symbol, group, system, name]."""
    return _load()["units"]


def grouped():
    """Units ordered for a dropdown: list of (group_label, [(symbol, name)]).

    Groups appear in first-seen order; '.' (ungrouped) is labelled 'General'.
    """
    _load()
    order, buckets = [], {}
    for sym, group, _system, name in _DATA["units"]:
        g = "General" if group in (".", "") else group
        if g not in buckets:
            buckets[g] = []
            order.append(g)
        buckets[g].append((sym, name if name not in (".", "") else sym))
    return [(g, buckets[g]) for g in order]


def is_canonical(sym: str) -> bool:
    _load()
    return (sym or "") in _SYMBOLS


def sanitize(sym: str):
    """Return the canonical unit for `sym`, or None if it can't be mapped.

    Order (matches SanitizeUnits.ini semantics):
      1. already canonical -> unchanged
      2. exact (case-sensitive) variant match
      3. case-insensitive variant match
      4. case-insensitive canonical match (e.g. 'KPA' -> 'kPa')
    """
    _load()
    if sym is None:
        return None
    s = sym.strip()
    if not s:
        return None
    if s in _SYMBOLS:
        return s
    san = _DATA["sanitize"]
    if s in san:
        return san[s]
    low = s.lower()
    if low in _SAN_CI:
        return _SAN_CI[low]
    if low in _LOWER:
        return _LOWER[low]
    return None


def group_of(sym: str):
    for u in units():
        if u[0] == sym:
            return u[1]
    return None


def default_imperial(si_sym: str):
    """The natural US/imperial counterpart of a metric unit, via [Group Units].

    e.g. '°C' -> '°F', 'kPa' -> 'psi', 'km/h' -> 'mph'. None if the
    unit isn't grouped or has no distinct US default.
    """
    _load()
    g = group_of(si_sym)
    if not g or g in (".", ""):
        return None
    row = _DATA["group_units"].get(g)
    if not row or len(row) < 2:
        return None
    metric, us = row[0], row[1]
    if si_sym == metric and us != metric:
        return us
    return None


def conversion(si_sym: str, im_sym: str):
    """(factor, offset, invert) to turn an SI value into the IM unit, or None.

    Looks up EFILive's [Unit Conversions] 'metric|custom' entry directly, then
    the reverse entry (inverting factor/offset) if only that exists.
    """
    _load()
    if not si_sym or not im_sym or si_sym == im_sym:
        return None
    conv = _DATA["conversions"]
    key = f"{si_sym}|{im_sym}"
    if key in conv:
        f, o, inv = conv[key]
        return (float(f), float(o), bool(inv))
    rev = f"{im_sym}|{si_sym}"
    if rev in conv:
        f, o, inv = conv[rev]
        ff = float(f)
        if inv or ff == 0:
            return None  # non-linear reverse — don't guess
        # forward: y = f*x + o  =>  x = (y - o)/f
        return (1.0 / ff, -float(o) / ff, False)
    return None


def compute_im_slot(si_factor, si_offset, conv):
    """Compose an SI_SLOT (raw->metric) with an SI->IM `conversion` tuple to get
    the IM_SLOT (raw->imperial) Factor/Offset. Returns (factor, offset) strings.

    metric  = raw * sf + so
    imperial = metric * c + co            (linear)   or  c / metric (invert)
    For the invert case the slot can't be a simple factor/offset, so we bail.
    """
    if conv is None:
        return None
    c, co, inv = conv
    try:
        sf = float(si_factor) if str(si_factor).strip() not in ("", ".") else 1.0
        so = float(si_offset) if str(si_offset).strip() not in ("", ".") else 0.0
    except ValueError:
        return None
    if inv:
        return None
    im_f = sf * c
    im_o = so * c + co
    return (_fmt(im_f), _fmt(im_o))


def _fmt(x: float) -> str:
    """Compact numeric string (trim trailing zeros, keep it readable)."""
    if x == int(x):
        return str(int(x))
    s = f"{x:.10g}"
    return s

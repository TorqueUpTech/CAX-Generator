"""Consolidate EFILive's unit definitions into one bundled JSON (`units.json`)
so the app can validate / offer / auto-convert units without the EFILive install
present at runtime (same pattern as segment_pointers.json).

Sources (read as Latin-1 — the degree sign is byte 0xB0):
  EFILiveSystem.ini  [Units]            -> canonical whitelist (symbol, group, system, name)
                     [Unit Conversions] -> {"SI|IM": [factor, offset, invert]}
                     [Group Units]      -> {group: [metric, us, user]}
  SanitizeUnits.ini  [Units]            -> {variant: canonical}  (third-party spellings)

Output:
  { "units": [[symbol, group, system, name], ...],
    "conversions": {"metric|custom": [factor, offset, invert_bool], ...},
    "group_units": {"<group>": [metric, us, user], ...},
    "sanitize": {"<variant>": "<canonical>", ...} }
"""
import json
import os
import shlex

EFI = r"C:\Program Files (x86)\EFILive\V8"
SYS = os.path.join(EFI, "EFILiveSystem.ini")
SAN = os.path.join(EFI, "SanitizeUnits.ini")
OUT = os.path.join(os.path.dirname(__file__), "units.json")


def _lines(path):
    with open(path, encoding="latin-1") as f:
        return f.read().splitlines()


def _sections(path):
    """Yield (section_name, [content_lines]) skipping comments/blanks."""
    name, buf = None, []
    for raw in _lines(path):
        s = raw.rstrip()
        if not s or s.lstrip().startswith(";"):
            continue
        t = s.strip()
        if t.startswith("[") and t.endswith("]"):
            if name is not None:
                yield name, buf
            name, buf = t[1:-1], []
        elif name is not None:
            buf.append(s)
    if name is not None:
        yield name, buf


def parse_system(path):
    units, conversions, group_units = [], {}, {}
    for name, rows in _sections(path):
        if name == "Units":
            for r in rows:
                if "=" not in r:
                    continue
                sym, rhs = r.split("=", 1)
                sym = sym.strip()
                if not sym:
                    continue
                try:
                    cols = shlex.split(rhs.strip())
                except ValueError:
                    cols = rhs.split()
                group = cols[0] if cols else "."
                pname = cols[1] if len(cols) > 1 else "."
                system = cols[-2] if len(cols) >= 2 else "."
                units.append([sym, group, system, pname])
        elif name == "Unit Conversions":
            for r in rows:
                if "=" not in r:
                    continue
                key, rhs = r.split("=", 1)
                key = key.strip()
                if "|" not in key:
                    continue
                f = rhs.split()
                if len(f) < 4:
                    continue
                # cols: Unused Factor Offset Invert
                conversions[key] = [f[1], f[2], f[3].upper() == "Y"]
        elif name == "Group Units":
            for r in rows:
                if "=" not in r:
                    continue
                key, rhs = r.split("=", 1)
                vals = rhs.split()
                if vals:
                    group_units[key.strip()] = vals[:3]
    return units, conversions, group_units


def parse_sanitize(path):
    out = {}
    for name, rows in _sections(path):
        if name != "Units":
            continue
        for r in rows:
            if "=" not in r:
                continue
            k, v = r.split("=", 1)
            k = k.strip()
            v = v.strip()
            if k and v:
                out[k] = v
    return out


def main():
    units, conversions, group_units = parse_system(SYS)
    sanitize = parse_sanitize(SAN)
    data = {"units": units, "conversions": conversions,
            "group_units": group_units, "sanitize": sanitize}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"),
                  sort_keys=True)
    print(f"Wrote {OUT} ({os.path.getsize(OUT) / 1024:.0f} KB)")
    print(f"  units={len(units)} conversions={len(conversions)} "
          f"groups={len(group_units)} sanitize={len(sanitize)}")
    print("  sample units:", units[:3])
    print("  sample conv  :", list(conversions.items())[:2])


if __name__ == "__main__":
    main()

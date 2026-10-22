"""
catalog — standardize calibration IDs across files by NAME, per controller.

After per-file re-ID the same calibration name can get a different template
number in each file. This keeps a master catalog so the same name always maps to
the same ID for a given controller:

    catalog.json = { "<controller>": { "<cal name>": [ "<id>", "<segment>" ] } }

Workflow: pick one curated file per controller as the master (build_from_master);
then standardize() renames every other file's calibrations to the master's IDs by
name. Names not in the master get a fresh template ID and are added. A name that
appears under a DIFFERENT segment than the catalog is flagged for review, not
renamed.
"""
from __future__ import annotations

import json
import os

import caxtree as ct
import idgen
import settings as appsettings

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "catalog.json")


def load() -> dict:
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(cat: dict) -> None:
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(cat, f, indent=2, sort_keys=True)


def build_from_master(cax, controller: str) -> int:
    """Make `cax` the master for `controller`: its name->ID pairs become canonical.
    Returns the number of entries."""
    cat = load()
    entry = {}
    for sec in cax.sections:
        cal = ct.cal_from_section(sec)
        if cal and cal.caption:
            entry[cal.caption] = [sec.name, cal.seg]
    cat[controller] = entry
    save(cat)
    return len(entry)


def has_master(controller: str) -> bool:
    return bool(load().get(controller))


def standardize(cax, controller: str, add_new: bool = True):
    """Plan ID changes for `cax` against the controller's master catalog.

    Returns (plan, new_names, flags):
      plan       = [(section, old_id, new_id), ...]
      new_names  = [(name, new_id), ...]  (added to catalog when add_new)
      flags      = [(cur_id, name, cal_seg, cat_seg, cat_id), ...] cross-segment
    Does NOT mutate the sections — call apply_plan() to do that.

    Collision-safe: the final ID of every section is unique within the file. If the catalog would map
    two sections to the same ID (e.g. two sections that still share a caption, or a caption whose
    canonical ID is already held by an untouched section), the later section is given a fresh unique ID
    instead - the tool never emits a duplicate [ID].
    """
    cat = load()
    cmap = dict(cat.get(controller, {}))
    # sections we never rename (no caption) keep their IDs: reserve them up front so nothing collides.
    fixed = {s.name for s in cax.sections
             if ct.cal_from_section(s) is None or not ct.cal_from_section(s).caption}
    reserved = {v[0] for v in cmap.values()} | {s.name for s in cax.sections}
    taken = set(fixed)                              # final IDs already claimed
    plan, new_names, flags = [], [], []
    for sec in cax.sections:
        cal = ct.cal_from_section(sec)
        if cal is None or not cal.caption:
            continue
        name = cal.caption
        if name in cmap:
            cat_id, cat_seg = cmap[name]
            if str(cat_seg) != str(cal.seg):
                flags.append((sec.name, name, cal.seg, cat_seg, cat_id))
                desired = sec.name                  # cross-segment: keep current ID (still guard below)
            else:
                desired = cat_id
        elif add_new:
            desired = _fresh_id(reserved | taken, cal.seg)
            cmap[name] = [desired, cal.seg]
            new_names.append((name, desired))
        else:
            desired = sec.name                      # dry run, unknown name: keep current ID
        if desired in taken:                        # collision guard: canonical ID blocked
            if sec.name not in taken:               # keep the section's current ID if free (idempotent)
                desired = sec.name
            else:                                   # last resort: a fresh unique ID
                desired = _fresh_id(reserved | taken, cal.seg)
        taken.add(desired)
        reserved.add(desired)
        if sec.name != desired:
            plan.append((sec, sec.name, desired))
    if add_new and new_names:
        cat[controller] = cmap
        save(cat)
    return plan, new_names, flags


def _fresh_id(reserved: set, seg: str) -> str:
    tmpl = appsettings.id_template(seg, idgen.DEFAULT_ID_TEMPLATES.get(seg, ""))
    if tmpl:
        nid = idgen.next_id(reserved, tmpl, appsettings.id_start(seg))
        if nid:
            return nid
    n = 1
    while f"X{seg or 0}{n:04d}" in reserved:
        n += 1
    return f"X{seg or 0}{n:04d}"


def apply_plan(plan) -> int:
    """Rename sections per plan, two-phase to avoid transient ID collisions."""
    for i, (sec, _old, _new) in enumerate(plan):
        sec.name = f"~STD{i}~"
    for (sec, _old, new) in plan:
        sec.name = new
    return len(plan)


def standardize_folder(folder: str, write: bool = False, log=lambda m: None) -> list:
    """Standardize every .cax in a folder against its controller's master.

    Controller is resolved by OS number (bundled os_controllers table). With
    write=False it's a dry run (no files changed). Returns per-file result rows:
    (filename, controller, status, renames, new, flags).
    """
    import caxformat
    import segments
    results = []
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith(".cax"):
            continue
        stem = os.path.splitext(fn)[0]
        path = os.path.join(folder, fn)
        ctrl = segments.controller_for_os(stem)
        if not ctrl:
            results.append((fn, "?", "skip: unknown controller", 0, 0, 0))
            continue
        if not has_master(ctrl):
            results.append((fn, ctrl, "skip: no master set", 0, 0, 0))
            continue
        try:
            cax = caxformat.parse_file(path)
        except OSError as e:
            results.append((fn, ctrl, f"error: {e}", 0, 0, 0))
            continue
        plan, new, flags = standardize(cax, ctrl, add_new=write)
        if write and plan:
            apply_plan(plan)
            caxformat.write_file(cax, path)
        status = "written" if (write and plan) else ("would change" if plan else "ok")
        results.append((fn, ctrl, status, len(plan), len(new), len(flags)))
        log(f"  {fn} [{ctrl}]: {len(plan)} renames, {len(new)} new, {len(flags)} flagged")
    return results


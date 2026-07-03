# Ultimate Cax Generator — Claude Code working agreement

> **Ultimate Cax Generator** — a Python/PySide6 editor for EFILive `.cax` (V7) and
> `.cax8` (V8) calibration-definition files. Began as a clean reimplementation of
> EFILive's lost-source Cax Generator (old binary in `CAXEdit/`), now extended well
> beyond it (verification, conversion, multi-file, segment tools).

## Read this first
1. **Use the skills** in `.claude/skills/` — they are the how-to for this project:
   - `caxedit-run` — launch/headless-test the app, sample files, module map.
   - `caxedit-segment-verify` — authoritative address→segment via UniversalPatcher XML + GM bin.
   - `caxedit-v7-v8` — V7/V8 formats, conversion, verification.
2. **Format facts** live in `docs/FORMAT.md` (V7) and `docs/FORMAT_V8.md` (V8).
   Look up the specific field there; don't re-derive the grammar.

## Project layout
- `app/` — the application (Python, no build step). Entry: `app/main.py`.
  - Core (Qt-free, unit-testable): `caxformat` (parse/validate/write, round-trip
    exact) · `caxtree` (Seg>Major>Minor tree + address helpers) · `convert`
    (V7→V8, V8→V7 load) · `verify`/`cax8format` (diff vs EFILive output) ·
    `addrcompare` (compare by address) · `segmap` (segment ranges from bin+XML) ·
    `units` (canonical units / sanitize / SI→IM conversion) ·
    `idgen` (ID templates) · `formats` · `settings`.
  - Core (Qt-free): `canmod` (export a `.cax` field def -> `.canmod`; value->raw
    bytes; write the Can-Display **CANDISP.DB** binary — byte-identical to the
    Riverdi dash `CalModDb`, see `Can-Display/docs/CAL-MOD-DB.md`).
  - GUI: `main.py` (tree + schema-driven record editor) · `canmoddlg` (Tools ▸
    **Export → To Can-Display** and **Build Can-Display DB** dialogs).
  - `_*.py` are throwaway dev/test scripts; `caxedit_settings.ini` and
    `segment_ranges.json` are generated state — don't hand-edit or commit as source.
- `docs/` — format references. `CAXEdit/` — the lost-source original binary (read-only reference).
- `template.cax8`, `cax_to_cax8.ini` — EFILive-supplied V8 references.

## Working style
- Requires `pip install pyside6` (6.11+). No compiler/build.
- After core changes, run a quick **headless** check (see `caxedit-run`) before
  launching the GUI. Round-trip fidelity matters: `caxformat` parse→serialize must
  stay byte-identical on real files — verify after touching it.
- Launching the GUI: pass file paths as one backtick-quoted PowerShell string
  (paths have spaces). **Don't auto-kill the app** — the user tests interactively;
  relaunch only after code changes.

## Hard rules / gotchas
- **Round-trip safety:** comments, blank lines, and unknown/bad records must be
  preserved on save (never silently dropped). DESC values are raw text (not tokenized).
- **`settings`** must use the single cached `QSettings` instance — separate
  instances read a stale, not-yet-synced INI. Persists to `app/caxedit_settings.ini`.
- **Segments:** join EFILive V7 seg numbers and UniversalPatcher names by **name**,
  not number. `V7toV8_Segments.ini` does NOT match real EFILive output — don't rely
  on it. `<Addresses>@X` in the UP XML is a bin pointer, not a literal range.
- **Don't trust the cax's own `VIEW.Seg`** to validate segment placement — that's
  circular; classify by address against the bin's real ranges (`segmap`).
- **Segment data is bundled & self-contained (JSON, no XML files):**
  `app/segment_pointers.json` (49 controllers' segment names + bin pointer
  offsets, consolidated from the UniversalPatcher SegmentConfig XMLs) and a
  prebuilt `app/segment_ranges.json` (102 OSes across E38/E40/E67/E98/T42/T43/
  T76/T87A). `segmap.segment_pointers(ctrl)` reads the JSON (external Drive XML is
  a last-resort fallback); cached ranges need neither pointers nor bin. Rebuild:
  `_build_segment_pointers.py` (from Drive XMLs) and `_build_segcache.py` (from bins).
- **Units must be valid V8 units.** A `.cax8`'s DataUnits/ColUnits/RowUnits must
  be canonical EFILive units (or a user-defined unit). The whitelist + conversions
  + third-party sanitize map are bundled in `app/units.json` (built by
  `_build_units.py` from `EFILiveSystem.ini` [Units]/[Unit Conversions]/[Group
  Units] and `SanitizeUnits.ini`; read those as **Latin-1** — degree = byte 0xB0).
  `units.sanitize()` matches case-sensitive then case-insensitive (EFILive
  semantics). The V7→V8 converter auto-sanitizes units and flags un-mappable ones
  for review. The units dropdown applies to SI/IM **slots and axes**
  (SI_ROW/IM_ROW/SI_COL/IM_COL). **SED (enum) calibrations are unitless** — the
  editor forces the SI/IM slot Units to `none` (field locked, labels still
  editable) and **enforces the SI→IM mirror**. The col/row axis Units become
  **`switch`** when the SI_ENUM has ≥2 options (an enum selector), else `none`
  (where the axis has labels). Same rule in the converter (DataUnits `none`;
  ColUnits/RowUnits `switch`/`none`) and in the bulk **Sanitize units** tool
  (`_scan_sed_units` / `convert.enum_option_count`).
- **Mirror is live:** when the SI↔IM mirror is on the IM records are hidden, so SI
  edits (slots, enums, axes) are copied to IM on every change — not just at toggle —
  or the saved file wouldn't actually be mirrored (`change_cb` in `_show_section`).
- External data (only needed to resolve a *new* OS not in the cache): GM bins in
  `G:\My Drive\GM BIN\<CTRL>\`, UniversalPatcher XML in
  `G:\My Drive\UniversalPatcher\XML\`, EFILive installs under
  `C:\Program Files (x86)\EFILive\`.

## Open threads
- Converter polish: segment remapping for genuine V7→V8 conversion (V8 round-trip done).
- TunerPro **XDF → cax** import (XDF samples in `G:\My Drive\GM BIN\*.xdf`).

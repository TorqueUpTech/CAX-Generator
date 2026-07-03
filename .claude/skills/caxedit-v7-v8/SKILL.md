---
name: caxedit-v7-v8
description: >
  How the CAX Editor handles EFILive's V7 (.cax) and V8 (.cax8) formats —
  converting V7→V8, loading V8 into the editor, and verifying a conversion
  against EFILive's own output. Use when asked about cax vs cax8 differences,
  why parameters "don't come across" in conversion, the [Data]/[Calibration]
  tables, segment remapping, or comparing files.
---

# V7 ↔ V8 formats and conversion

## Format shapes
- **V7 `.cax`** = INI, one `[section]` per calibration (id = section name), keys
  `VIEW/ADDR/SI_SLOT/IM_SLOT/SI_ENUM/SI_ROW/SI_COL/DESC.NNN/ULABELS`. Whitespace
  fields, `.` = placeholder, quote fields with spaces/commas. See `docs/FORMAT.md`.
- **V8 `.cax8`** = column tables: `[Provider] [Define] [Enum] [Axis] [Data]
  [Calibration] [Description]`. One row per cal in `[Data]` (Address, ColCount,
  RowCount, Factor, Offset, Inv, **Size** as `bits:mask:shift`, Type, units…) and
  `[Calibration]` (Seg, **Tree** as `Major\Minor`, Flags, Display, Enum, axis refs…).
  See `docs/FORMAT_V8.md` and `D:\Projects\CAX-Generator\template.cax8`.

## Conversion gotchas (learned from EFILive's real output)
- Addresses become `$` + UPPERCASE + zero-padded to 6 (`205c2` → `$0205C2`).
- **V7 DisplayType 4 = "List"** (undocumented in the V7 header, appears in real data).
- EFILive transforms: `Type SED→UNM` (enum'd params), `DataUnits Option→None`,
  universal **Seg remap** (e.g. V7 seg 7 System → V8 seg 1). It does NOT relocate
  addresses.
- `[Enum]`/`[Axis]` are shared & deduped by content across cals.
- Neither format stores the **controller** in-file — it's derived from the OS
  number (filename) via `segments.controller_for_cam`, or implied by the cax8's
  folder. The V8 [Provider] block has no controller field.
- Long `[Enum]` lines use `\` line-continuation.

## "Parameters not coming across"
Dominant real causes, found by diffing V7 source against EFILive's V8 output:
1. **Corrupt source** — e.g. tab chars in section headers (`[B\t9020]`) break
   EFILive's parser → most/all cals dropped. Fix the file.
2. **Cal-ID collisions** with EFILive factory definitions (per the V7 header rule
   "IDs already used by EFILive are silently ignored") — per-OS.
Don't trust `V7toV8_Segments.ini` as the segment mapping; it doesn't match real output.

## Tools
- `convert.convert(cax, controller, cmap, provider)` → cax8 text + drop/review report.
- `convert.load_v8(path)` → editable V7-style CaxFile (resolves enum/axis refs).
  Used by **File ▸ Open V8** (Ctrl+O); **Open Legacy V7** is Ctrl+L.
- **Verify a conversion** (`verify.py` / `cax8format.py`, GUI Tools ▸ Verify
  against EFILive V8): diff V7 source ↔ EFILive's `.cax8`, lists dropped/added/
  changed by cal ID. Batch script: `app/_verify_batch.py`.
- **Compare two files by address** (`addrcompare.py`): matches params by ADDR not
  ID, with import-missing-into-A. Works across .cax and .cax8.

# Ultimate Cax Generator

**An editor for EFILive calibration-definition files — V7 `.cax` and V8 `.cax8`.**
Created by David Christie & Claude AI · Torque Up Technical Services · www.torqueup.net

Began as a clean reimplementation of *Cax Generator* and has grown into a full
editing, verification and conversion toolkit.

---

## Run it

```
pip install pyside6
python app\main.py                 # or: python app\main.py "path\to\file.cax"
```

Python 3.11+ with PySide6 (Qt 6). No build/compile step. Settings persist to a
portable `app\caxedit_settings.ini` beside the app.

## Features

### Files
- **V7 `.cax`** — read / edit / write with **byte-identical round-trip** (comments,
  blanks, unknown/bad records preserved). Validated against EFILive's `Template.cax`.
- **V8 `.cax8`** — open into the editor (rebuilds records from the V8 tables,
  resolving enum/axis references).
- **Open V8** (Ctrl+O) and **Open Legacy V7** (Ctrl+L), each defaulting to its
  configured folder · **Open Recent** (last 10) · **Open in text editor** (Ctrl+E).
- Per-version Save / Save As with automatic `.bak` backup. **Save Versions**
  (Ctrl+Shift+S) writes the working V7 `.cax` *and* an auto-converted V8 `.cax8`
  (correct segment remap) in one action.
- Bad-record warnings show **line numbers** and an **Edit-in-text-editor** button.

### Editing
- **Navigator tree** (`Segment ▸ Major ▸ Minor ▸ Calibration`); all segments shown.
  Create folders to fill the Major/Minor fields.
- **Per-calibration editor** — multi-line Description (`DESC.001/002…`), VIEW options
  **collapsed by default** (expand to override), enum dropdowns with descriptions.
- **Metric/Imperial mirror** tickbox — edit the metric (`SI`) slots; auto-copy to
  imperial (`IM`) and hide the IM slots to cut scrolling.
- **Validated units** — every SI/IM **Units** field (slots *and* axes) is a grouped
  dropdown of EFILive's canonical units. A non-standard unit is flagged inline with
  a one-click **Sanitize** (maps known third-party spellings → valid V8 units);
  legitimate user-defined units are never blocked. On the IM slot, **Fill
  Factor/Offset from SI** computes the imperial Factor/Offset from EFILive's own
  conversion table. **SED (enum)** calibrations are unitless — the slot Units lock
  to `none` automatically.
- **New / Delete** calibrations; **auto-generated IDs** from per-segment templates.
- **Multiple windows** with **Copy/Paste calibrations between files** (Ctrl+C/V),
  collision-safe.

### Verification & tools (Tools menu)
- **Verify segment placement** — authoritative address→segment check using each
  controller's real ranges; flags calibrations in the wrong segment; report &
  confirm-each fix. Segment data is **bundled** (see below) so it needs no extra files.
- **Sanitize units** — scans every SI/IM slot, normalizes non-standard units to
  valid V8 units (report & confirm); un-mappable units are listed as likely
  user-defined. The V7→V8 conversion runs the same normalization automatically.

### Hex view
- Virtualized hex view of the controller bin beside the editor, with go-to, find
  bytes, copy, and back/forward history.
- **Find references** (Ctrl+R or right-click) — searches the whole file for any
  calibration whose address range covers the highlighted bytes; jumps to the first
  match, or — if nothing references it — offers to **create a calibration there**.
- **Create new calibration here** (right-click) — make a calibration at the
  selected address: prompts for Id (auto-suggested for the address's segment) and
  caption, sets the ADDR, and uses the selection length as the bit count.
- **Compare two files by address** (matches by ADDR, not ID) with import-missing.
- **Verify against EFILive V8** — diff a V7 source vs EFILive's converted `.cax8`.
- **Re-ID from templates** — bulk renumber with a savable report + confirm.
- **Import / Export XDF** — import TunerPro `.xdf` definitions (scalars, flags,
  tables) into cax — scaling from the MATH equations, units/labels from the axes,
  **VIEW.Seg classified from each address** — and export cax back out to `.xdf`.

### Preferences (tabbed)
- **General** — per-version save folders, per-segment ID templates, text-editor exe,
  segment-suggestion toggle.
- **VIEW defaults** — master Visibility & Group (with the descriptions from
  `Template.cax`) + **Apply to all calibrations** (with a warning prompt).

## Segment data (bundled, self-contained)

Segment verification maps an address to its physical segment using:
- `app/segment_pointers.json` — 49 controllers' segment names + bin pointer offsets.
- `app/segment_ranges.json` — prebuilt ranges for ~102 OSes across
  **E38, E40, E67, E98, T42, T43, T76, T87A**.
- `app/units.json` — EFILive's canonical unit whitelist, conversion factors and
  third-party→canonical sanitize map (built by `_build_units.py` from
  `EFILiveSystem.ini` + `SanitizeUnits.ini`). Used for the units dropdown,
  validation, sanitize, and the SI→IM Factor/Offset auto-fill.

For those OSes it needs **no bin and no external drive**. An un-cached OS reuses a
cached sibling of the same controller, or resolves from a bin if one is available.
Rebuild/extend the cache from bins with `python app\_build_segcache.py`.

## Layout

```
app/            application (main.py + Qt-free core modules)
  segment_pointers.json  controller segment names + bin pointer offsets
  segment_ranges.json  prebuilt per-OS segment ranges
docs/           FORMAT.md (V7), FORMAT_V8.md (V8)
.claude/skills/ how-to skills (run, segment-verify, v7-v8)
CAXEdit/        original lost-source binary (reference)
template.cax8, cax_to_cax8.ini   EFILive V8 references
```

Core modules (Qt-free, testable): `caxformat` (parse/validate/write) · `caxtree`
(tree + address helpers) · `convert` (V7↔V8) · `verify`/`cax8format` (diff vs V8) ·
`addrcompare` (compare by address) · `segmap` (segment ranges) · `idgen` (ID
templates) · `formats` · `settings`.

## Backup

Copy all `.cax` / `.cax8` from the configured V7/V8 folders to another drive/folder.
- **GUI:** set a *Backup folder* in Preferences, then **Tools ▸ Back up cax/cax8 files now**.
- **Command line / scheduled task** (runs without the GUI; incremental — only
  changed files are copied; appends to `<dest>\backup.log`):

```
python D:\Projects\CAX-Generator\app\backup.py
python …\backup.py --dest "E:\Backups\Cax" --source "C:\path\to\cax"
```

Point a Windows Task Scheduler job at `python …\app\backup.py` to run it on a
schedule independently of the app.

## Notes
The V7→V8 segment remap is learned from EFILive's own converted output
(`app/v7v8_segmap.json`, built by `_build_v7v8_segmap.py`) — the
`V7toV8_Segments.ini` does not match real output.

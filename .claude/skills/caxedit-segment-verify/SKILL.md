---
name: caxedit-segment-verify
description: >
  How the CAX Editor authoritatively determines which segment a calibration
  belongs to from its flash address, to find/fix mis-assigned calibrations. Use
  when asked about segment verification, why a calibration is "in the wrong
  segment", adding segment support for a new controller, the UniversalPatcher
  segment XMLs, reading segment ranges from a GM bin, or the @pointer format.
---

# Authoritative segment placement (address → correct segment)

A calibration's correct segment is decided by which physical flash-segment its
`ADDR` falls in. The cax's own `VIEW.Seg` can be **wrong**, so it cannot be used
to validate itself. The authoritative source is the controller's real segment
address table, which lives **in the bin**, located via UniversalPatcher XML.

Implemented in `app/segmap.py`; wired to GUI **Tools ▸ Verify segment placement**.

## Data sources (now bundled & self-contained)

1. **Segment pointers are bundled** in `app/segment_pointers.json` (49 controllers,
   consolidated from the UniversalPatcher SegmentConfig XMLs; `segmap.segment_pointers(ctrl)`;
   `segmap.xml_path_for` reads these first, Drive `G:\My Drive\UniversalPatcher\XML`
   is only a fallback). Each `<SegmentConfig>` has `<Name>` and `<Addresses>@OFFSET`
   — a **pointer**, not a literal range.
2. **A prebuilt range cache** `app/segment_ranges.json` ships ~102 OSes across
   E38/E40/E67/E98/T42/T43/T76/T87A — so most files need **no bin at all**.
3. **A controller bin** is needed only to resolve a *new* OS not in the cache:
   `G:\My Drive\GM BIN\<CTRL>\<CTRL>.<OSID>.bin`. The real start/end addresses are
   stored *inside the bin* at the pointer offset.

## Pointer format (proven on T42/T43)

At `<Addresses>@OFFSET` the bin holds consecutive **big-endian 32-bit (start,end)
pairs**; an optional `*N` means read N pairs. The first pair is the segment's
real range; later pairs are checksum sub-blocks (ignore). Decoded ranges come out
clean, contiguous, non-overlapping. Example T43/24243170:
BootBlock 020000-02FFFF · OS 030000-1BFFFF · Trans 1C0000-1FCB57 ·
Diag 1FCB58-1FEB57 · System 1FEB58-1FFFFF.

## Procedure

```python
import segmap
xml  = segmap.xml_path_for("T42")              # controller -> XML
binf = segmap.find_bin_for_os("24231674","T42")# OS -> bin in GM BIN\T42\
segs = segmap.resolve_ranges(xml, binf)        # [Segment(name, [(lo,hi)...])]
phys = segmap.classify(addr_int, segs)         # -> 'Trans' / 'System' / None
```

Resolution order in `MainWindow._segments_for_current`:
1. exact `cached_segments(os_id)` from the prebuilt JSON;
2. else **same-controller sibling** `cached_segments_for_controller(ctrl)` — reuse
   another cached OS of that controller (T42 etc. share identical boundaries;
   shows a status-bar note naming the source OS);
3. else resolve from `segment_pointers.json` + a bin (auto-found in `GM BIN\<CTRL>\`,
   else prompt) and cache the result.
Controller is auto-detected via `segments.controller_for_cam(os_id)` (EFILive Cam
folders), else prompted. Rebuild/extend the cache from all bins with
`app/_build_segcache.py`.

## V7 seg number ↔ physical name

UniversalPatcher uses **names** (Trans, System, Diag); the cax uses EFILive **numbers**.
Join by name, never by number (the numbering differs, and `V7toV8_Segments.ini`
does NOT match real output — don't trust it). The mapping is derived per-file from
the majority of correctly-placed cals (`MainWindow._seg_name_maps`): for T42/T43
this yields 1=OS, 4=Trans, 7=System. A physical segment with no in-file owner
(e.g. Diag) can't be auto-numbered → flagged "set manually".

## Detecting/fixing mistakes
Compare each cal's classified physical segment to `num2phys[cal.seg]`. If they
differ, it's mis-assigned. The fix dialog is **report + confirm each**: auto-proposes
the number where derivable, else manual; Apply rewrites `VIEW.Seg`. Real examples
found: `GTU0029` "Transmission Type" assigned System but address in Trans; `D9814`
assigned Trans but address in Diag.

## Adding a new controller / OS
If the controller is in `segment_pointers.json`, just have one `<CTRL>.<OSID>.bin`
available once — open the file and Verify (or run `_build_segcache.py`) to resolve
and cache it. For a brand-new controller, add its SegmentConfig XML to the Drive
`UniversalPatcher\XML` and rerun `_build_segment_pointers.py` to regenerate the
JSON. Only `@pointer` addresses are used; `seek:`-only segments are skipped.

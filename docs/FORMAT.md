# EFILive `.cax` file format

Recovered from sample files (their own header comments) and the `CAXEdit.exe` V1.00 parser.
A `.cax` ("Calibration definition eXtension") file is a **Windows-INI variant**, ASCII/Latin-1.

## Lexical rules
- Lines beginning with `;` are comments.
- Blank lines ignored.
- A section header is `[ID]` where `ID` is a calibration id (e.g. `[B9021]`) or the literal `[OS]`.
- Inside a section: `KEY = values`. Values are **whitespace-separated** (tabs or spaces, runs collapse).
- A field is `.` when undefined (placeholder).
- Fields containing spaces or commas **must** be double-quoted. `""` escapes a quote inside a quoted field.
- Enum/label lists (`*_ENUM`, `*_ROW`, `*_COL`) are **comma-separated** lists.

## Section keys

| Key       | Fields |
|-----------|--------|
| `DESC.NNN`| Description paragraph NNN (multiline desc split into numbered keys). May contain `{CALID}` refs and `See also: {-CALID1}{-CALID2}…` |
| `VIEW`    | Seg, Caption, Major, Minor, Visibility(0-3), DisplayType(1=Val,2=Col,3=Map), Force2D(0/1), Group(0=Basic,1=Std,2=Expert) |
| `ADDR`    | Address(es) [comma-list of hex for multi-col], BitOffset(0-7), BitCount(usually 8/16), DataType(UNM/SNM/SED), Inverse(0/1) |
| `SI_SLOT` | Units, Factor, Offset, Prec, MinLimit, MaxLimit, MinUser, MaxUser  (Metric) |
| `IM_SLOT` | Units, Factor, Offset, Prec, MinLimit, MaxLimit, MinUser, MaxUser  (Imperial) |
| `SI_ENUM` | comma-list of Metric enum labels (or `.`) |
| `IM_ENUM` | comma-list of Imperial enum labels (or `.`) |
| `SI_ROW`  | Units, comma-list of Metric row labels |
| `IM_ROW`  | Units, comma-list of Imperial row labels |
| `SI_COL`  | Units, comma-list of Metric col labels |
| `IM_COL`  | Units, comma-list of Imperial col labels |
| `ULABELS` | 3 unit labels (Body, Col, Row) — purpose undocumented, default to dots |

### Field detail
- **VIEW.Visibility**: bitmapped (may be written `$hex`). Low bits = license level
  `$00`=hidden(software-only), `$01`=Demo/All, `$02`=Personal+, `$03`=Workshop+.
  OR-able flags: `$10`=hidden on USA Location ID, `$20`=show in [Calibration] tab on
  open, `$40`=editable-but-never-saved (read-only), `$80`=not editable (read-only).
- **VIEW.DisplayType**: 1=Val, 2=Col (single column), 3=Map (2D matrix),
  **4=List** (column of contiguous bit-fields each BitCount long; for bit-encoded
  data e.g. trouble codes).
- **ADDR.DataType** (case-insensitive): `UNM` unsigned, `SNM` signed, `SED`
  state-encoded/enum, `FLT` 4-byte IEEE float. BitCount usually 8/16/32.
- **ADDR.Inverse**: 1 ⇒ value displayed/edited as 1/N.
- Calibration id first letter ⇒ segment: A=OS, B=Engine cal, C=Engine diag, D=Trans cal, E=Trans diag, F=Fuel, G=Speedo.

### Navigator tree (how VIEW maps to the EFILive tree — see Tree-Capture.png)
The editor's tree mirrors EFILive's Navigator, built from each calibration's `VIEW`:
```
Segment (VIEW.Seg)              top branch, e.g. "Transmission Calibration"
└─ Major (VIEW.Major)          folder, e.g. "Shift Times", "Pressure"
   └─ Minor (VIEW.Minor)       sub-folder (omitted/"." ⇒ leaf sits directly under Major)
      └─ Caption (VIEW.Caption) leaf = the calibration (section id is the payload)
```
Leaf icon from `VIEW.DisplayType`: 1=Val (single value), 2=Col (column/table), 3=Map (2D grid).
Segment number→friendly name is an EFILive convention, not stored in the .cax — the editor
keeps an editable lookup matching EFILive's `navigator.txt`: 0=Operating System (PCM/TCM
root), **1=EFILive Custom Calibrations**, 2=Engine Calibration, 3=Engine Diagnostics,
4=Transmission Calibration, 5=Transmission Diagnostics, 6=Fuel System, 7=System,
8=Speedometer; fall back to "Segment N". (V8/.cax8 segment numbers are controller-specific
and differ — see FORMAT_V8.md / `V7toV8_Segments.ini`; they are handled by the learned remap,
not this table.)

### `[OS]` section
Defines memory locations of OS info; all values HEX; unsupported = `X`. Observed keys:
`PCM_OSID, PCM_EMN, PCM_HW, PCM_VIN, PCM_SERIAL, PCM_BCC`.

## Validation (must match original error strings)
Per record type the parser checks **field count** and **duplicate** within a section, and per-field **syntax**:
- `"<Rec> record contains an incorrect number of fields."`
- `"<Rec> record duplicate."`
- `"Bad <Rec> field syntax, field <n>"`
- `"Bad <Si|Im>Enum record"` (enum-specific)
Records that fail are flagged as **BAD RECORDS** and listed; on load the app warns:
> "This file contains incorrect records which would be listed. Please fix these records manually
> with a text editor and reopen the file, otherwise these records will be lost on save."

On save, bad/unknown lines must be **preserved**, not silently dropped (original keeps round-trip fidelity).

## Sample files
`~/Documents/01250003.cax` and siblings (full ~16 KB files).
NB: the QNAP Qsync `.destcache` copies are 44-byte stubs (`FileSize=… MT_H=… MT_L=…`) — not real data.

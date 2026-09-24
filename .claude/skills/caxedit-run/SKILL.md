---
name: caxedit-run
description: >
  How to run, launch, and headless-test the CAX Editor (the PySide6 rebuild of
  EFILive's Cax Generator) in D:\Projects\CAX-Generator\app. Use when asked to
  start/launch/screenshot the app, open a .cax/.cax8 in it, reproduce a UI
  behavior, or quickly test a module change without opening the GUI. Covers the
  PowerShell launch quoting gotcha, sample files, and the offscreen test pattern.
---

# Running the CAX Editor

PySide6 (Qt6) desktop app. No build step — it's Python. Entry point:
`D:\Projects\CAX-Generator\app\main.py`. Requires `pip install pyside6` (6.11+).

## Launch the GUI

The app takes an optional file path argument. **Paths with spaces split in
PowerShell `Start-Process` unless passed as one backtick-quoted string** — this is
the #1 launch mistake:

```powershell
$f='C:\Program Files (x86)\EFILive\V7.5\Calibrations\24236195.cax'
Start-Process -FilePath python -ArgumentList @('D:\Projects\CAX-Generator\app\main.py', "`"$f`"")
```

Confirm it loaded (title shows the filename + version tag, not "(untitled)"):

```powershell
Start-Sleep 3
Get-Process python | Where-Object { $_.MainWindowTitle -like '*CAX*' } |
  Select-Object Id, MainWindowTitle
```

Do **not** kill the app after launching — the user tests interactively. Relaunch
(kill + start) only when you changed code and need it reloaded.

## Sample files
- V7 `.cax`: `C:\Program Files (x86)\EFILive\V7.5\Calibrations\*.cax`
  (canonical: `24236195.cax` T42, 41 cals; `24231674.cax` has segment mistakes).
- V8 `.cax8`: `C:\Users\david\Documents\EFILive\V8\User Defined Cax8\<CTRL>\*.cax8`.
- Corrupt example: `12617631.cax` (tab chars in section headers).

## Headless testing (no GUI window)

Most logic is Qt-free (`caxformat`, `caxtree`, `convert`, `verify`, `segmap`,
`idgen`). Test it directly. To build a `MainWindow` without showing a window, set
the offscreen platform:

```powershell
$env:QT_QPA_PLATFORM="offscreen"
python -c "import sys; sys.path.insert(0,r'D:\Projects\CAX-Generator\app'); import main; from PySide6.QtWidgets import QApplication; a=QApplication([]); w=main.MainWindow(); w.load(r'...path...'); print(w.windowTitle())"
```

**Gotcha:** a file with bad records pops a *modal* warning on `load()` that hangs
offscreen (no one clicks OK). Test with a clean file, or monkeypatch
`QMessageBox.question/warning` to auto-return before calling `load()`.

For multi-line scripts use a here-string (`@" ... "@`) and
`sys.stdout.reconfigure(encoding='utf-8')` — the console is cp1252 and chokes on
the tree/emoji glyphs.

## Module map
`caxformat` parse/validate/write (round-trip exact) · `caxtree` Seg>Major>Minor
tree + address→segment helpers · `convert` V7→V8 + V8→V7 load · `verify`/`cax8format`
diff vs EFILive output · `addrcompare` compare two files by address · `segmap`
authoritative segment ranges from bin+XML · `idgen` ID templates · `formats`
version writers · `settings` INI persistence. See `docs/FORMAT.md`,
`docs/FORMAT_V8.md`.


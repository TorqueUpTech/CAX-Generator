"""
backup — copy .cax / .cax8 files from the configured folders to a backup location.

Runs from the GUI (Tools menu) and standalone from the command line, so a Windows
Task Scheduler job can call it on a schedule without opening the app:

    python D:\\Projects\\CAX-Generator\\app\\backup.py
    python …\\backup.py --dest "E:\\Backups\\Cax" --source "C:\\path\\to\\cax"

Incremental: a file is copied only if missing or changed (size/mtime). Each
source is mirrored under <dest>\\<label>\\… and a line is appended to
<dest>\\backup.log.
"""
from __future__ import annotations

import os
import shutil
import sys

import settings as appsettings

EXTS = (".cax", ".cax8")


def _up_to_date(src: str, dst: str) -> bool:
    if not os.path.exists(dst):
        return False
    s, d = os.stat(src), os.stat(dst)
    return s.st_size == d.st_size and int(s.st_mtime) <= int(d.st_mtime)


def run_backup(dest: str | None = None, sources=None, log=print, stamp: str = "") -> dict:
    """Copy cax/cax8 from sources to dest. Returns stats dict.

    sources: list of (label, path) or plain paths. Defaults to the configured
    V7 and V8 folders.
    """
    dest = dest or appsettings.backup_dest()
    stats = {"copied": 0, "skipped": 0, "errors": 0, "bytes": 0, "dest": dest}
    if not dest:
        stats["error"] = "No backup destination configured."
        log(stats["error"])
        return stats
    if sources is None:
        sources = appsettings.backup_sources()
    norm = []
    for s in sources:
        if isinstance(s, (tuple, list)):
            norm.append((s[0], s[1]))
        else:
            norm.append((os.path.basename(os.path.normpath(s)) or "src", s))

    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        stats["error"] = f"Cannot create destination: {e}"
        log(stats["error"])
        return stats

    for label, src in norm:
        if not src or not os.path.isdir(src):
            log(f"  (skip source — not found: {src})")
            continue
        for dp, _dn, files in os.walk(src):
            for fn in files:
                if not fn.lower().endswith(EXTS):
                    continue
                sp = os.path.join(dp, fn)
                rel = os.path.relpath(sp, src)
                tp = os.path.join(dest, label, rel)
                try:
                    if _up_to_date(sp, tp):
                        stats["skipped"] += 1
                        continue
                    os.makedirs(os.path.dirname(tp), exist_ok=True)
                    shutil.copy2(sp, tp)
                    stats["copied"] += 1
                    stats["bytes"] += os.path.getsize(sp)
                except OSError as e:
                    stats["errors"] += 1
                    log(f"  ERROR {sp}: {e}")

    msg = (f"{stamp}backup -> {dest}: {stats['copied']} copied, "
           f"{stats['skipped']} up-to-date, {stats['errors']} errors, "
           f"{stats['bytes'] / 1024:.0f} KB")
    log(msg)
    try:
        with open(os.path.join(dest, "backup.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass
    return stats


def _main(argv):
    import argparse
    from datetime import datetime
    p = argparse.ArgumentParser(description="Back up cax/cax8 files.")
    p.add_argument("--dest", help="destination folder (default: configured)")
    p.add_argument("--source", action="append",
                   help="source folder (repeatable; default: configured V7+V8)")
    args = p.parse_args(argv)
    sources = args.source if args.source else None
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S  ")
    stats = run_backup(dest=args.dest, sources=sources, stamp=stamp)
    return 1 if stats.get("error") or stats.get("errors") else 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys.exit(_main(sys.argv[1:]))

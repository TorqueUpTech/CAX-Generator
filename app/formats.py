"""
formats — pluggable .cax format versions (V7, V8, …).

The reader produces a version-neutral CaxFile (sections/records). Each format
version supplies a *writer* so the same in-memory data can be saved as V7 or V8.
V8 differs significantly from V7 and is stubbed until its spec/sample is supplied.

Default save directories per version are user settings (see settings.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import caxformat as cf


@dataclass(frozen=True)
class FormatVersion:
    key: str                 # 'v7', 'v8'
    label: str               # 'V7 (legacy)', 'V8'
    ext: str                 # '.cax', '.cax8'
    filter: str              # file dialog filter
    writer: Callable[[cf.CaxFile, str], None]
    implemented: bool = True
    needs_controller: bool = False   # V8 conversion needs a target controller


def _write_v7(cax: cf.CaxFile, path: str) -> None:
    cf.write_file(cax, path)


def _write_v8(cax: cf.CaxFile, path: str) -> None:
    # Direct V8 emit (round-trip): uses each cal's VIEW.Seg as-is and the
    # preserved [Provider]. For genuine V7->V8 conversion with segment remapping,
    # use convert.convert() with a ControllerMap instead.
    import convert
    convert.write_v8(cax, path)


VERSIONS: dict[str, FormatVersion] = {
    "v7": FormatVersion("v7", "V7 (legacy)", ".cax", "CAX Files (*.cax)",
                        _write_v7, implemented=True),
    "v8": FormatVersion("v8", "V8", ".cax8", "CAX8 Files (*.cax8)",
                        _write_v8, implemented=True, needs_controller=True),
}

DEFAULT_VERSION = "v7"


def write(cax: cf.CaxFile, path: str, version: str) -> None:
    fmt = VERSIONS.get(version)
    if fmt is None:
        raise ValueError(f"Unknown format version: {version}")
    fmt.writer(cax, path)


def detect_version(cax: cf.CaxFile) -> str:
    """Best-effort version sniff from a parsed file. Defaults to V7.

    Refine once V8 markers are known (e.g. a header key or new section type).
    """
    return DEFAULT_VERSION

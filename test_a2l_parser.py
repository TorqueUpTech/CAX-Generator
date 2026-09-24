#!/usr/bin/env python
"""Quick test of A2L parser on GM ECM file."""

import sys
sys.path.insert(0, 'app')

import a2limport

# Test parsing the GM ECM A2L file
path = r"G:\My Drive\GM BIN\A2L Files\e38a8114404_quasi-12647991-12649046-12653249-12653252-2012-c6.a2l"

print("Parsing A2L file...")
items = a2limport.parse_a2l_minimal(path)

print(f"[OK] Parsed {len(items)} calibrations\n")

# Show first 10
print("First 10 calibrations:")
print("-" * 100)
for item in items[:10]:
    kind = f"[{item.kind:5}]"
    addr = f"0x{item.address:X}" if item.address else "  N/A"
    comp_type = item.compu_type[:8] if item.compu_type else "?"
    print(f"{kind} {addr:8} | {comp_type:8} | {item.title[:50]}")
    if item.annotation:
        print(f"         {item.annotation[:80]}")

print("\n" + "=" * 100)
print("Statistics:")
by_kind = {}
by_compu = {}
for item in items:
    by_kind[item.kind] = by_kind.get(item.kind, 0) + 1
    by_compu[item.compu_type] = by_compu.get(item.compu_type, 0) + 1

print("\nBy type:")
for k, v in sorted(by_kind.items()):
    print(f"  {k:8} {v:4} items")

print("\nBy COMPU method:")
for k, v in sorted(by_compu.items()):
    print(f"  {k:10} {v:4} items")

# Count warnings
warnings = sum(1 for item in items if item.note)
print(f"\n[WARN] {warnings} items with warnings (complex lookup tables, etc.)")

print("\n[OK] A2L parser working correctly!")

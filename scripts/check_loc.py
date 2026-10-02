#!/usr/bin/env python3
"""Enforces strict <= 200 LOC per file in src/nexus."""
import sys
from pathlib import Path

MAX_LOC = 200
TARGET_DIR = Path("src/nexus")
violations = []

for py_file in TARGET_DIR.rglob("*.py"):
    if "__pycache__" in py_file.parts:
        continue
    with py_file.open("r", encoding="utf-8", errors="ignore") as f:
        loc = sum(1 for _ in f)
    if loc > MAX_LOC:
        violations.append((loc, py_file))

if violations:
    print(f"❌ Found {len(violations)} files exceeding strict limit of {MAX_LOC} LOC:\n")
    for loc, path in sorted(violations, key=lambda x: x[0], reverse=True):
        print(f"  {loc:4d} LOC: {path}")
    sys.exit(1)

print(f"✅ All Python files in {TARGET_DIR} adhere to <= {MAX_LOC} LOC.")

#!/usr/bin/env python3
"""Verify every published firmware manifest entry using repository bytes."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "firmware"

def main():
    manifest = ROOT / "source-sha256.json"
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not entries:
        print("FAIL: firmware source manifest is empty")
        return 1
    bad = []
    for relative, expected in entries.items():
        target = (ROOT / relative).resolve()
        if ROOT.resolve() not in target.parents or target == manifest.resolve():
            bad.append(relative + ": invalid manifest path")
        elif not target.is_file():
            bad.append(relative + ": missing")
        elif hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            bad.append(relative + ": SHA256 mismatch")
    for message in bad:
        print("FAIL: " + message)
    print("SOURCE MANIFEST: %s (%d entries, %d failures)" %
          ("FAIL" if bad else "PASS", len(entries), len(bad)))
    return 1 if bad else 0

if __name__ == "__main__":
    sys.exit(main())

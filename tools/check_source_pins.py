#!/usr/bin/env python3
"""Verify all SDK component HEADs against the published exact source pins."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=os.environ.get("SDK_PATH"))
    parser.add_argument("--pins", type=Path, default=ROOT / "firmware/source-pins.json")
    args = parser.parse_args()
    if args.sdk is None:
        parser.error("--sdk or SDK_PATH is required")
    sdk = args.sdk.resolve()
    pins = json.loads(args.pins.read_text(encoding="utf-8"))
    failed = []
    for relative, expected in pins.items():
        target = (sdk / relative).resolve()
        if target != sdk and sdk not in target.parents:
            parser.error("source pin path escapes SDK: " + relative)
        if not target.is_dir():
            print("FAIL %s: missing component directory" % relative)
            failed.append(relative)
            continue
        result = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                                capture_output=True, text=True)
        actual = result.stdout.strip() if result.returncode == 0 else "missing repository"
        matches = actual == expected
        print("%s %s: %s" % ("PASS" if matches else "FAIL", relative, actual))
        if not matches:
            failed.append(relative)
    print("SOURCE PINS: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0

if __name__ == "__main__":
    sys.exit(main())

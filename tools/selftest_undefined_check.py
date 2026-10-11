#!/usr/bin/env python3
"""Self-test for tools/check_undefined_names.py: prove it actually detects
undefined names (a checker that never fails is worthless).
"""

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "tools" / "check_undefined_names.py"

BROKEN = '''\
import os
import time

SETTING = 1

def helper():
    return SETTING

def main():
    # oops: this name is never defined anywhere
    return oops_missing_name + 1

main()
'''
OK_SAMPLE = '''\
import os
import time

SETTING = 1

def helper():
    value = SETTING
    return value

def main():
    return helper() + SETTING

main()
'''


def run_with_src(text):
    """Run the checker against a temporary src/ tree containing one file."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        (tmp / "src").mkdir()
        # The checker only scans a fixed DEPLOYED list, so provide all of them.
        for name in ("main.py", "gba_ui.py", "gba_input.py", "gba_audio.py"):
            (tmp / "src" / name).write_text(text if name == "main.py" else "X = 1\n",
                                            encoding="utf-8")
        # Point the checker at the temp tree by patching its ROOT via a wrapper.
        wrapper = tmp / "run.py"
        wrapper.write_text(
            "import runpy, sys, pathlib\n"
            "src = pathlib.Path(%r)\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('chk', %r)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "mod.SRC = src / 'src'\n"
            "sys.exit(mod.main())\n" % (str(tmp), str(CHECKER)),
            encoding="utf-8")
        proc = subprocess.run([sys.executable, str(wrapper)],
                              capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr


def main():
    print("self-test: check_undefined_names.py")
    print("=" * 70)

    rc, out = run_with_src(BROKEN)
    detected = "oops_missing_name" in out
    print("  broken sample -> exit=%d, detected the bad name: %s" % (rc, detected))
    ok1 = rc != 0 and detected

    rc2, out2 = run_with_src(OK_SAMPLE)
    clean = rc2 == 0 and "PASS" in out2
    print("  clean sample  -> exit=%d, reported PASS: %s" % (rc2, clean))
    ok2 = clean

    print("=" * 70)
    if ok1 and ok2:
        print("RESULT: PASS - the checker detects undefined names and passes clean code")
        return 0
    print("RESULT: FAIL - the checker is not trustworthy")
    if not ok1:
        print("  (it failed to flag undefined name 'oops_missing_name')")
    if not ok2:
        print("  (it failed a clean file)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
